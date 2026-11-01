"""The monitoring pipeline: one AOI check, from catalogue search to dispatched alert.

A run selects the newest unanalysed clear scene, a clear baseline and last year's same
season, then detects, classifies and dispatches. Live checks and archive replay share every
line of this code; replay only limits scenes to ``as_of`` and sets the origin badge. Run
state is committed before it is published, so an event always describes a persisted fact.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.ml.inference import get_change_detector, get_landcover_classifier
from app.models import (
    AnalysisRecord,
    AOIAcquisition,
    AuthorisedZone,
    Case,
    CatalogueRefresh,
    Finding,
    MonitoringArea,
    MonitoringRun,
    SatelliteAcquisition,
)
from app.satellite.chain import get_chain
from app.satellite.grid import AOIGrid, AOITooLargeError, build_grid, mask_to_geojson, rasterize_geometry
from app.satellite.provider import AcquisitionCandidate, ProviderAuthRequired, ProviderError
from app.services.alerting import create_or_merge_alerts
from app.services.analysis import (
    AssessmentContext,
    ChangeMap,
    Rainfall,
    analysis_valid,
    assess_region,
    build_change_map,
    clear_fraction,
    compute_indices,
    scl_valid,
    spectral_landcover,
)
from app.services.audit import record_audit
from app.services.context import (
    fetch_case_context,
    public_context,
    rasterize_osm,
    read_slope_degrees,
)
from app.services.dispatch import dispatch_many
from app.services.events import publish
from app.services.evidence import write_evidence, write_scene_thumbnail
from app.services.payloads import alert_payload, load_json, zone_payload
from app.services.storage import public_path
from app.timeutil import parse_date, utcnow
from app.vocabulary import ORIGIN_LIVE, ORIGIN_REPLAY

logger = logging.getLogger(__name__)

FINAL_RUN_STATES = {
    "COMPLETED",
    "COMPLETED_NO_SIGNIFICANT_CHANGE",
    "NO_NEW_ACQUISITION",
    "NO_SUITABLE_ACQUISITION",
    "SKIPPED_PAUSED",
    "BLOCKED_CREDENTIALS",
    "FAILED",
}
ANALYSED_STATUSES = ("ANALYSED", "CHANGE")
_MAX_BASELINE_ATTEMPTS = 6
_MAX_REFERENCE_READS = 14
_MIN_COMPONENT_PIXELS = 4

# Set during graceful shutdown; a run checks it at each stage boundary.
shutdown_requested = threading.Event()
# Catalogue rows are shared between AOIs, so concurrent workers can discover the same product.
_catalogue_lock = threading.Lock()
# One inference at a time keeps peak memory bounded on an 8 GB machine.
_inference_lock = threading.Lock()


class RunInterrupted(Exception):
    """The server is shutting down; the run returns to the queue."""


@dataclass
class ClearScene:
    scene: AcquisitionCandidate
    scl: np.ndarray
    fraction: float


def queue_monitoring_run(
    db: Session,
    area: MonitoringArea,
    trigger: str = "manual",
    *,
    origin: str = ORIGIN_LIVE,
    as_of: str | None = None,
    replay_id: str | None = None,
    force: bool = False,
) -> MonitoringRun:
    run = MonitoringRun(
        aoi_id=area.id,
        status="QUEUED",
        stage="QUEUED",
        progress_percent=0,
        message=f"Queued by {trigger} trigger.",
        origin=origin,
        trigger=trigger,
        as_of_date=as_of,
        replay_id=replay_id,
        replay_force=force,
    )
    db.add(run)
    db.flush()
    record_audit(
        db,
        "monitoring_run",
        run.id,
        "run_queued",
        {"trigger": trigger, "aoi_id": area.id, "origin": origin, "as_of": as_of},
    )
    db.commit()
    db.refresh(run)
    _publish_run(run, area.name)
    return run


def _publish_run(run: MonitoringRun, area_name: str | None) -> None:
    publish(
        "run",
        {
            "run_id": run.id,
            "area_id": run.aoi_id,
            "area_name": area_name,
            "status": run.status,
            "stage": run.stage,
            "progress_percent": run.progress_percent,
            "message": run.message,
            "origin": run.origin,
            "replay_id": run.replay_id,
            "as_of_date": run.as_of_date,
        },
    )


def _update_run(
    db: Session,
    run: MonitoringRun,
    area_name: str | None,
    *,
    stage: str,
    progress: int,
    message: str,
    status: str = "RUNNING",
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    if shutdown_requested.is_set() and status == "RUNNING":
        raise RunInterrupted()
    run.status = status
    run.stage = stage
    run.progress_percent = max(0, min(100, int(progress)))
    run.message = message
    run.error_code = error_code
    run.error_detail = error_detail
    if status in FINAL_RUN_STATES:
        run.completed_at = utcnow()
    record_audit(
        db,
        "monitoring_run",
        run.id,
        "run_state_changed",
        {"stage": stage, "status": status, "progress_percent": run.progress_percent},
    )
    db.commit()
    _publish_run(run, area_name)


# Catalogue ----------------------------------------------------------------------------


def _upsert_scene(db: Session, area: MonitoringArea, scene: AcquisitionCandidate) -> AOIAcquisition:
    acquisition = (
        db.query(SatelliteAcquisition)
        .filter(SatelliteAcquisition.product_id == scene.product_id)
        .first()
    )
    if acquisition is None:
        acquisition = SatelliteAcquisition(
            provider=scene.provider,
            product_id=scene.product_id,
            scene_key=scene.scene_key,
            tile=scene.tile,
            collection=scene.collection,
            satellite=scene.satellite,
            sensor=scene.sensor,
            processing_level=scene.processing_level,
            acquisition_datetime=scene.acquisition_datetime,
            cloud_cover=scene.cloud_cover,
            bbox_json=json.dumps(scene.bbox) if scene.bbox else None,
            asset_metadata_json=json.dumps(
                {
                    "assets": scene.assets,
                    "properties": scene.raw_properties,
                    "baseline": scene.processing_baseline,
                    "sequence": scene.sequence,
                },
                default=str,
            ),
            source_url=scene.source_url,
            quality_note=f"Discovered in the {scene.provider} catalogue.",
        )
        db.add(acquisition)
        db.flush()
    link = (
        db.query(AOIAcquisition)
        .filter(AOIAcquisition.aoi_id == area.id, AOIAcquisition.acquisition_id == acquisition.id)
        .first()
    )
    if link is None:
        link = AOIAcquisition(aoi_id=area.id, acquisition_id=acquisition.id, scene_status="DISCOVERED")
        db.add(link)
        db.flush()
    return link


def _record_scenes(
    db: Session, area: MonitoringArea, scenes: list[AcquisitionCandidate]
) -> dict[str, AOIAcquisition]:
    with _catalogue_lock:
        links = {scene.product_id: _upsert_scene(db, area, scene) for scene in scenes}
        db.commit()
    return links


def _scene_from_row(row: SatelliteAcquisition) -> AcquisitionCandidate:
    metadata = load_json(row.asset_metadata_json, {})
    return AcquisitionCandidate(
        product_id=row.product_id,
        collection=row.collection,
        acquisition_datetime=row.acquisition_datetime,
        cloud_cover=row.cloud_cover,
        geometry=None,
        bbox=load_json(row.bbox_json, None),
        satellite=row.satellite,
        sensor=row.sensor,
        processing_level=row.processing_level,
        source_url=row.source_url,
        provider=row.provider,
        tile=row.tile,
        processing_baseline=metadata.get("baseline"),
        sequence=int(metadata.get("sequence") or 0),
        assets=metadata.get("assets") or {},
        raw_properties=metadata.get("properties") or {},
    )


def _known_scenes(
    db: Session, area: MonitoringArea, start: datetime, end: datetime
) -> list[AcquisitionCandidate]:
    """Scenes already catalogued for this AOI, so replay survives a catalogue outage."""

    rows = (
        db.query(SatelliteAcquisition)
        .join(AOIAcquisition, AOIAcquisition.acquisition_id == SatelliteAcquisition.id)
        .filter(
            AOIAcquisition.aoi_id == area.id,
            SatelliteAcquisition.acquisition_datetime >= start,
            SatelliteAcquisition.acquisition_datetime <= end,
        )
        .order_by(SatelliteAcquisition.acquisition_datetime.desc())
        .all()
    )
    return [_scene_from_row(row) for row in rows]


def _analysed_dates(db: Session, area: MonitoringArea, run: MonitoringRun) -> set[str]:
    """Scene dates already analysed under this run's origin, so results never mix."""

    query = (
        db.query(SatelliteAcquisition.acquisition_datetime)
        .join(AnalysisRecord, AnalysisRecord.after_acquisition_id == SatelliteAcquisition.id)
        .join(MonitoringRun, AnalysisRecord.run_id == MonitoringRun.id)
        .filter(
            AnalysisRecord.aoi_id == area.id,
            AnalysisRecord.status == "COMPLETED",
            MonitoringRun.origin == run.origin,
        )
    )
    if run.origin == ORIGIN_REPLAY and run.replay_force:
        # A forced replay repeats the walk, skipping only what this same replay has done.
        query = query.filter(MonitoringRun.replay_id == run.replay_id)
    return {row[0].strftime("%Y-%m-%d") for row in query.all()}


# Scene selection ----------------------------------------------------------------------


class _SceneInspector:
    """Reads SCL for candidate scenes, at most once each, recording the result per AOI."""

    def __init__(
        self,
        db: Session,
        area: MonitoringArea,
        grid: AOIGrid,
        aoi_mask: np.ndarray,
        links: dict[str, AOIAcquisition],
    ):
        self.db = db
        self.area = area
        self.grid = grid
        self.aoi_mask = aoi_mask
        self.links = links
        self.reads = 0
        self.chain = get_chain()

    def inspect(self, scene: AcquisitionCandidate) -> ClearScene:
        scl = self.chain.read_scl(scene, self.grid)
        self.reads += 1
        fraction = clear_fraction(scl, self.aoi_mask)
        link = self.links.get(scene.product_id)
        if link is not None:
            is_new = link.aoi_clear_fraction is None
            link.aoi_clear_fraction = round(fraction, 4)
            if link.scene_status in ("DISCOVERED", "CLOUDED", "CLEAR"):
                link.scene_status = (
                    "CLEAR" if fraction >= settings.min_aoi_clear_fraction else "CLOUDED"
                )
            self.db.commit()
            if is_new:
                publish(
                    "scene",
                    {
                        "area_id": self.area.id,
                        "area_name": self.area.name,
                        "date": scene.date_key,
                        "product_id": scene.product_id,
                        "aoi_clear_fraction": round(fraction, 4),
                    },
                )
        return ClearScene(scene, scl, fraction)

    def best_of_date(self, scenes: list[AcquisitionCandidate]) -> ClearScene:
        """An AOI can straddle two tiles on one date; the better-covered record wins."""

        results = [self.inspect(scene) for scene in scenes]
        return max(results, key=lambda item: item.fraction)


def _group_by_date(scenes: list[AcquisitionCandidate]) -> list[list[AcquisitionCandidate]]:
    """Scenes grouped per date, newest date first."""

    groups: dict[str, list[AcquisitionCandidate]] = {}
    for scene in sorted(scenes, key=lambda item: item.acquisition_datetime, reverse=True):
        groups.setdefault(scene.date_key, []).append(scene)
    return list(groups.values())


def _select_after(
    inspector: _SceneInspector, groups: list[list[AcquisitionCandidate]], analysed: set[str]
) -> tuple[ClearScene | None, list[ClearScene]]:
    """Walk new scenes newest-first until one is clear. Returns (scene, cloudy ones seen)."""

    newest_analysed = max(analysed) if analysed else ""
    cloudy: list[ClearScene] = []
    for group in groups:
        date_key = group[0].date_key
        if date_key <= newest_analysed or inspector.reads >= settings.max_scl_reads_per_run:
            break
        best = inspector.best_of_date(group)
        if best.fraction >= settings.min_aoi_clear_fraction:
            return best, cloudy
        cloudy.append(best)
    return None, cloudy


def _select_baseline(
    inspector: _SceneInspector,
    groups: list[list[AcquisitionCandidate]],
    after: ClearScene,
) -> ClearScene | None:
    """The newest clear scene at least the minimum gap earlier that overlaps usefully."""

    latest = after.scene.acquisition_datetime - timedelta(days=settings.monitoring_min_pair_gap_days)
    attempts = 0
    for group in groups:
        if group[0].acquisition_datetime > latest:
            continue
        if inspector.reads >= settings.max_scl_reads_per_run:
            return None
        best = inspector.best_of_date(group)
        if best.fraction < settings.min_aoi_clear_fraction:
            continue
        shared = (scl_valid(best.scl) & scl_valid(after.scl) & inspector.aoi_mask).sum()
        if shared / max(1, inspector.aoi_mask.sum()) >= settings.min_pair_valid_fraction:
            return best
        attempts += 1
        if attempts >= _MAX_BASELINE_ATTEMPTS:
            return None
    return None


def _select_reference(
    inspector: _SceneInspector,
    geometry: dict[str, Any],
    after_day: datetime,
) -> tuple[ClearScene | None, str | None]:
    """The clear scene nearest the same date last year. Failure means an untested season."""

    target = after_day - timedelta(days=365)
    window = timedelta(days=settings.seasonal_reference_window_days)
    try:
        scenes = get_chain().search_scenes(geometry, target - window, target + window, limit=120)
    except ProviderError as error:
        return None, f"The catalogue could not be reached for last year's scenes: {error}"
    if not scenes:
        return None, None
    inspector.links.update(_record_scenes(inspector.db, inspector.area, scenes))
    groups = _group_by_date(scenes)
    groups.sort(key=lambda group: abs((group[0].acquisition_datetime - target).total_seconds()))
    for reads, group in enumerate(groups):
        if reads >= _MAX_REFERENCE_READS:
            break
        try:
            best = inspector.best_of_date(group)
        except ProviderError as error:
            return None, f"Last year's scene could not be read: {error}"
        if best.fraction >= settings.min_aoi_clear_fraction:
            return best, None
    return None, None


# Models -------------------------------------------------------------------------------


def _detect(
    before: np.ndarray, after: np.ndarray, valid: np.ndarray
) -> tuple[np.ndarray | None, float, str, str | None]:
    """Returns (probability, threshold, method label, version). None means spectral only."""

    try:
        detector = get_change_detector()
        if not detector.available:
            return None, 0.5, "spectral-baseline", None
        with _inference_lock:
            probability = detector.predict(before, after, valid)
        version = getattr(detector, "version", None)
        return probability, float(detector.threshold), "siamese-unet+spectral", version
    except Exception:  # A model failure degrades to the labelled spectral baseline.
        logger.exception("Change model failed; using the spectral baseline for this run.")
        return None, 0.5, "spectral-baseline (model error)", None


def _classify_landcover(
    before: np.ndarray,
    after: np.ndarray,
    before_indices: dict[str, np.ndarray],
    after_indices: dict[str, np.ndarray],
    valid: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, str]:
    try:
        classifier = get_landcover_classifier()
        if classifier.available:
            with _inference_lock:
                before_classes, _ = classifier.predict(before, valid)
                after_classes, _ = classifier.predict(after, valid)
            return before_classes, after_classes, "landcover-unet"
    except Exception:
        logger.exception("Land-cover model failed; using spectral rules for this run.")
    return (
        spectral_landcover(before, before_indices, valid),
        spectral_landcover(after, after_indices, valid),
        "spectral-landcover",
    )


# Context ------------------------------------------------------------------------------


def _active_zones(
    db: Session, grid: AOIGrid, after_day: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Zones in force on the scene date that touch this AOI: (with masks, for the case record)."""

    matched: list[dict[str, Any]] = []
    public: list[dict[str, Any]] = []
    for zone in db.query(AuthorisedZone).all():
        valid_from, valid_to = parse_date(zone.valid_from), parse_date(zone.valid_to)
        if (valid_from and after_day < valid_from) or (valid_to and after_day > valid_to):
            continue
        geometry = load_json(zone.geometry_json, None)
        if not geometry:
            continue
        try:
            mask = rasterize_geometry(geometry, grid)
        except Exception as error:  # One bad zone geometry should not fail the run.
            logger.warning("Authorised zone %s has unusable geometry: %s", zone.id, error)
            continue
        if mask.any():
            payload = zone_payload(zone)
            matched.append({"zone": payload, "mask": mask})
            public.append({key: value for key, value in payload.items() if key != "geometry"})
    return matched, public


def _assessment_context(
    raw: dict[str, Any],
    grid: AOIGrid,
    zones: list[dict[str, Any]],
    after_date: str,
    reference_date: str | None,
) -> AssessmentContext:
    rainfall = raw.get("rainfall") or {}
    disasters = raw.get("disasters") or []
    features = raw.get("osm_features") or []
    quarry_mask, quarry_names = rasterize_osm(features, grid, "quarry")
    protected_mask, protected_names = rasterize_osm(features, grid, "protected_area")

    def named(kind: str) -> list[str]:
        return [event["title"] for event in disasters if event.get("type") == kind]

    slope_cache: list[np.ndarray | None] = []

    def slope() -> np.ndarray | None:
        if not slope_cache:
            slope_cache.append(read_slope_degrees(grid))
        return slope_cache[0]

    return AssessmentContext(
        after_date=after_date,
        reference_date=reference_date,
        rainfall=Rainfall(
            available=bool(rainfall.get("available")),
            total_mm=rainfall.get("total_mm"),
            max_daily_mm=rainfall.get("max_daily_mm"),
            normal_mm=rainfall.get("normal_mm"),
            anomaly_percent=rainfall.get("anomaly_percent"),
        ),
        flood_events=named("flood"),
        fire_events=named("fire"),
        landslide_events=named("landslide"),
        quarry_mask=quarry_mask,
        quarry_names=quarry_names,
        protected_mask=protected_mask,
        protected_names=protected_names,
        zones=zones,
        slope_degrees=slope,
        context_unavailable=[item["source"] for item in raw.get("unavailable") or []],
    )


# The pipeline -------------------------------------------------------------------------


def _thumbnail_for(
    area: MonitoringArea, scene: AcquisitionCandidate, bands: np.ndarray, valid: np.ndarray, grid: AOIGrid
) -> str | None:
    path = settings.output_dir / "scenes" / area.id / f"{scene.scene_key}.jpg"
    try:
        write_scene_thumbnail(path, bands, valid, grid)
        return public_path(path)
    except Exception:  # A timeline thumbnail is a convenience, never worth a run.
        logger.exception("Could not write a thumbnail for %s", scene.scene_key)
        return None


def _read_or_none(chain, scene: AcquisitionCandidate, grid: AOIGrid) -> np.ndarray | None:
    try:
        return chain.read_bands(scene, grid)
    except ProviderError as error:
        logger.warning("Seasonal reference %s unreadable: %s", scene.scene_key, error)
        return None


def _run_pipeline(db: Session, run: MonitoringRun, area: MonitoringArea) -> None:
    chain = get_chain()
    geometry = json.loads(area.geometry_json)
    grid = build_grid(geometry)
    aoi_mask = rasterize_geometry(geometry, grid)
    if not aoi_mask.any():
        aoi_mask = np.ones(grid.shape, dtype=bool)
    as_of = parse_date(run.as_of_date)
    end = (
        datetime(as_of.year, as_of.month, as_of.day, 23, 59, 59) if as_of else utcnow()
    )
    start = end - timedelta(days=settings.monitoring_lookback_days)
    origin_note = f" as of {run.as_of_date}" if as_of else ""

    run.started_at = utcnow()
    _update_run(
        db,
        run,
        area.name,
        stage="SEARCHING_CATALOGUE",
        progress=8,
        message=f"Searching the Sentinel-2 L2A catalogue for this AOI{origin_note}.",
    )
    run.catalogue_query_json = json.dumps(
        {"start": start.isoformat() + "Z", "end": end.isoformat() + "Z", "geometry": geometry}
    )
    db.commit()
    try:
        scenes = chain.search_scenes(geometry, start, end)
    except ProviderError as error:
        db.add(
            CatalogueRefresh(
                provider="chain",
                aoi_id=area.id,
                query_json=run.catalogue_query_json or "{}",
                status="FAILED",
                result_count=0,
                message=str(error)[:1000],
            )
        )
        db.commit()
        # Replay walks the past, so scenes catalogued earlier remain good evidence. A live
        # check has to reach the catalogue, or it would report "nothing new" untruthfully.
        scenes = _known_scenes(db, area, start, end) if run.origin == ORIGIN_REPLAY else []
        if not scenes:
            raise
    run.provider = scenes[0].provider if scenes else None
    db.add(
        CatalogueRefresh(
            provider=run.provider or "chain",
            aoi_id=area.id,
            query_json=run.catalogue_query_json or "{}",
            status="SUCCESS",
            result_count=len(scenes),
            newest_acquisition_at=scenes[0].acquisition_datetime if scenes else None,
            message=f"The catalogue returned {len(scenes)} scene record(s) after de-duplication.",
        )
    )
    links = _record_scenes(db, area, scenes)
    if not scenes:
        _finish_without_case(
            db, run, area, "NO_SUITABLE_ACQUISITION", "The catalogue has no scenes for this AOI in the lookback window."
        )
        return

    _update_run(
        db,
        run,
        area.name,
        stage="CHECKING_CLOUD",
        progress=18,
        message=f"Measuring usable pixels inside the AOI for {len(scenes)} catalogued scene(s), newest first.",
    )
    inspector = _SceneInspector(db, area, grid, aoi_mask, links)
    groups = _group_by_date(scenes)
    after, cloudy = _select_after(inspector, groups, _analysed_dates(db, area, run))
    if after is None:
        if cloudy:
            newest = cloudy[0]
            _finish_without_case(
                db,
                run,
                area,
                "NO_SUITABLE_ACQUISITION",
                f"The newest scene ({newest.scene.date_key}) is only {newest.fraction:.0%} clear inside "
                f"this AOI, below the {settings.min_aoi_clear_fraction:.0%} required. "
                f"{len(cloudy)} new scene(s) were too cloudy.",
            )
        else:
            _finish_without_case(
                db,
                run,
                area,
                "NO_NEW_ACQUISITION",
                "No scene newer than the last analysed one has been published for this AOI yet.",
            )
        return

    baseline = _select_baseline(inspector, groups, after)
    if baseline is None:
        _finish_without_case(
            db,
            run,
            area,
            "NO_SUITABLE_ACQUISITION",
            f"Scene {after.scene.date_key} is clear, but no clear baseline at least "
            f"{settings.monitoring_min_pair_gap_days} days earlier exists in the lookback window.",
        )
        return

    after_day = after.scene.acquisition_datetime
    reference, reference_problem = _select_reference(inspector, geometry, after_day)
    after_link, before_link = links[after.scene.product_id], links[baseline.scene.product_id]
    run.after_acquisition_id = after_link.acquisition_id
    run.before_acquisition_id = before_link.acquisition_id
    _update_run(
        db,
        run,
        area.name,
        stage="RETRIEVING_IMAGERY",
        progress=35,
        message=(
            f"Reading {grid.width} x {grid.height} px windows: scene {after.scene.date_key} "
            f"({after.fraction:.0%} clear), baseline {baseline.scene.date_key} ({baseline.fraction:.0%} clear), "
            + (f"last year's reference {reference.scene.date_key}." if reference else "no same-season reference.")
        ),
    )
    after_bands = chain.read_bands(after.scene, grid)
    before_bands = chain.read_bands(baseline.scene, grid)
    reference_bands = _read_or_none(chain, reference.scene, grid) if reference else None
    if reference is not None and reference_bands is None:
        reference_problem = "Last year's scene could not be read."
        reference = None

    valid = analysis_valid(after.scl, aoi_mask) & analysis_valid(baseline.scl, aoi_mask)
    before_indices = compute_indices(before_bands)
    after_indices = compute_indices(after_bands)

    _update_run(
        db,
        run,
        area.name,
        stage="DETECTING_CHANGE",
        progress=50,
        message=f"Comparing the pair on {float(valid.sum()) / max(1, aoi_mask.sum()):.0%} of the AOI usable in both scenes.",
    )
    probability, model_threshold, method, model_version = _detect(before_bands, after_bands, valid)
    min_pixels = max(
        _MIN_COMPONENT_PIXELS, int(np.ceil(area.min_change_area_m2 / grid.pixel_area_m2))
    )
    change_map: ChangeMap = build_change_map(
        before_bands,
        after_bands,
        before_indices,
        after_indices,
        valid,
        sensitivity=area.sensitivity,
        min_pixels=min_pixels,
        max_components=settings.max_findings_per_case,
        probability=probability,
        model_threshold=model_threshold,
        method=method,
    )
    del probability
    quality = {
        "after_clear_fraction": round(after.fraction, 4),
        "baseline_clear_fraction": round(baseline.fraction, 4),
        "pair_valid_fraction": round(float(valid.sum()) / max(1, aoi_mask.sum()), 4),
        "grid": {"epsg": grid.epsg, "width": grid.width, "height": grid.height},
        "after_product_id": after.scene.product_id,
        "before_product_id": baseline.scene.product_id,
        "reference_product_id": reference.scene.product_id if reference else None,
        "reference_problem": reference_problem,
        "min_component_pixels": min_pixels,
    }

    if not change_map.component_ids:
        analysis = _analysis_record(
            db, run, area, after_link, before_link, method, grid, quality, {"regions": 0}
        )
        after_link.scene_status = "ANALYSED"
        after_link.thumbnail = _thumbnail_for(area, after.scene, after_bands, valid, grid)
        record_audit(db, "analysis", analysis.id, "analysis_completed", {"method": method, "regions": 0})
        _finish_without_case(
            db,
            run,
            area,
            "COMPLETED_NO_SIGNIFICANT_CHANGE",
            f"Analysed {after.scene.date_key} against {baseline.scene.date_key}: "
            f"no region reached {area.min_change_area_m2:,.0f} m2.",
        )
        return

    _update_run(
        db,
        run,
        area.name,
        stage="GATHERING_CONTEXT",
        progress=62,
        message=f"{len(change_map.component_ids)} change region(s) found. Gathering rainfall, disaster and map context.",
    )
    raw_context = fetch_case_context(
        area.center_lat, area.center_lon, load_json(area.bbox_json, []), after_day.date()
    )
    zones, zones_public = _active_zones(db, grid, after_day.date())
    raw_context["authorised_zones"] = zones_public
    assessment_context = _assessment_context(
        raw_context,
        grid,
        zones,
        after.scene.date_key,
        reference.scene.date_key if reference else None,
    )

    _update_run(
        db,
        run,
        area.name,
        stage="CLASSIFYING",
        progress=72,
        message="Classifying land cover and testing each region for season and authorisation.",
    )
    before_classes, after_classes, landcover_method = _classify_landcover(
        before_bands, after_bands, before_indices, after_indices, valid
    )
    reference_indices = compute_indices(reference_bands) if reference_bands is not None else None
    reference_valid = (
        analysis_valid(reference.scl, aoi_mask) if reference is not None else None
    )
    assessments = []
    region_masks = []
    for identifier in change_map.component_ids:
        region = change_map.labels == identifier
        region_masks.append(region)
        assessments.append(
            assess_region(
                region,
                change_map=change_map,
                before_indices=before_indices,
                after_indices=after_indices,
                after=after_bands,
                before_classes=before_classes,
                after_classes=after_classes,
                reference_indices=reference_indices,
                reference_valid=reference_valid,
                context=assessment_context,
                pixel_area_m2=grid.pixel_area_m2,
                heavy_rain_mm=settings.heavy_rain_mm_14d,
                extreme_rain_mm=settings.extreme_rain_mm_14d,
            )
        )
    del before_classes, after_classes, reference_indices

    _update_run(
        db,
        run,
        area.name,
        stage="WRITING_EVIDENCE",
        progress=82,
        message="Writing evidence imagery.",
    )
    case_id_folder = settings.output_dir / "cases" / run.id
    evidence = write_evidence(
        case_id_folder,
        grid=grid,
        before=before_bands,
        after=after_bands,
        valid=valid,
        ndvi_before=before_indices["ndvi"],
        ndvi_after=after_indices["ndvi"],
        labels=change_map.labels,
        verdicts=[item.verdict for item in assessments],
        reference=reference_bands,
    )
    after_link.thumbnail = _thumbnail_for(area, after.scene, after_bands, valid, grid)

    analysis_method = f"{method} | {landcover_method}"
    processed_at = utcnow()
    analysis = _analysis_record(
        db,
        run,
        area,
        after_link,
        before_link,
        analysis_method,
        grid,
        quality,
        {
            "regions": len(assessments),
            "changed_area_m2": sum(item.area_m2 for item in assessments),
            "categories": sorted({item.category for item in assessments}),
            "verdicts": sorted({item.verdict for item in assessments}),
        },
        evidence_path=public_path(evidence.images["panel"]),
    )
    order = {"low": 1, "medium": 2, "high": 3, "critical": 4}
    material = [item for item in assessments if item.verdict != "SEASONAL_BENIGN"]
    ranked = material or assessments
    headline = max(ranked, key=lambda item: item.area_m2)
    images = {name: public_path(path) for name, path in evidence.images.items()}
    case = Case(
        title=f"{area.name}: {headline.category.replace('_', ' ').title()} on {after.scene.date_key}"[:160],
        location=f"{area.center_lat:.5f}, {area.center_lon:.5f}",
        before_date=baseline.scene.date_key,
        after_date=after.scene.date_key,
        source=f"Sentinel-2 L2A via {after.scene.provider}"[:120],
        pixel_resolution_m=grid.pixel_size,
        analysis_method=analysis_method[:64],
        overall_confidence=max(item.confidence for item in ranked),
        changed_area_px=sum(item.area_pixels for item in assessments),
        changed_area_m2=sum(item.area_m2 for item in assessments),
        highest_severity=max((item.severity for item in ranked), key=lambda s: order.get(s, 0)),
        before_image=images["before"],
        after_image=images["after"],
        mask_image=images["overlay"],
        aoi_id=area.id,
        analysis_id=analysis.id,
        before_acquisition_id=before_link.acquisition_id,
        after_acquisition_id=after_link.acquisition_id,
        data_origin="sentinel2_l2a",
        origin=run.origin,
        replay_id=run.replay_id,
        requires_human_verification=True,
        model_version_label=model_version,
        quality_note=(
            f"{quality['pair_valid_fraction']:.0%} of the AOI was usable in both scenes "
            f"(scene {after.fraction:.0%} clear, baseline {baseline.fraction:.0%} clear)."
        ),
        image_bounds_json=json.dumps(evidence.bounds),
        images_json=json.dumps(images),
        seasonal_reference_json=(
            json.dumps(
                {
                    "date": reference.scene.date_key,
                    "product_id": reference.scene.product_id,
                    "image": public_path(evidence.reference_image) if evidence.reference_image else None,
                }
            )
            if reference is not None
            else None
        ),
        context_json=json.dumps(public_context(raw_context)),
    )
    db.add(case)
    db.flush()
    regions_by_finding: dict[str, np.ndarray] = {}
    for assessment, region, thumbnail in zip(
        assessments, region_masks, evidence.finding_thumbnails, strict=True
    ):
        lon, lat = grid.pixel_to_lonlat(assessment.centroid_x, assessment.centroid_y)
        finding = Finding(
            case_id=case.id,
            activity=assessment.category.lower(),
            category=assessment.category,
            confidence=assessment.confidence,
            severity=assessment.severity,
            area_pixels=assessment.area_pixels,
            area_m2=assessment.area_m2,
            centroid_x=assessment.centroid_x,
            centroid_y=assessment.centroid_y,
            centroid_lat=round(lat, 6),
            centroid_lon=round(lon, 6),
            bbox=",".join(map(str, assessment.bbox)),
            geometry_json=json.dumps(mask_to_geojson(region, grid)),
            ndvi_before=assessment.ndvi_before,
            ndvi_after=assessment.ndvi_after,
            ndvi_delta=assessment.ndvi_after - assessment.ndvi_before,
            requires_human_verification=True,
            evidence_json=json.dumps(
                {
                    **assessment.evidence,
                    "before_product_id": baseline.scene.product_id,
                    "after_product_id": after.scene.product_id,
                    "landcover_source": landcover_method,
                }
            ),
            verdict=assessment.verdict,
            verdict_reasons_json=json.dumps(assessment.reasons),
            transition_from=assessment.transition_from,
            transition_to=assessment.transition_to,
            indices_json=json.dumps(assessment.indices),
            change_probability=assessment.change_probability,
            seasonal_persists=assessment.seasonal_persists,
            thumbnail=public_path(thumbnail),
        )
        db.add(finding)
        case.findings.append(finding)
        db.flush()
        regions_by_finding[finding.id] = region
    after_link.scene_status = "CHANGE"
    after_link.case_id = case.id
    created, updated = create_or_merge_alerts(
        db,
        case,
        area,
        regions_by_finding,
        grid,
        confidence_threshold=settings.alert_confidence_threshold,
        processed_at=processed_at,
    )
    record_audit(
        db,
        "case",
        case.id,
        "case_created",
        {
            "aoi_id": area.id,
            "analysis_id": analysis.id,
            "origin": run.origin,
            "new_alerts": len(created),
            "repeat_detections": len(updated),
        },
    )
    if run.origin == ORIGIN_LIVE:
        area.last_checked_at = utcnow()
    db.commit()
    del after_bands, before_bands, reference_bands, change_map, region_masks, regions_by_finding

    # Only now are the rows visible to other sessions, so only now may clients hear about them.
    for alert in created:
        publish("alert", {**alert_payload(alert), "event_kind": "created"})
    for alert in updated:
        publish("alert", {**alert_payload(alert), "event_kind": "updated"})

    alert_ids = [alert.id for alert in created]
    if alert_ids:
        _update_run(
            db,
            run,
            area.name,
            stage="DISPATCHING",
            progress=92,
            message=f"Dispatching {len(alert_ids)} alert(s) to the responsible departments.",
        )
        dispatch_many(alert_ids)
    benign = len(assessments) - len(material)
    _update_run(
        db,
        run,
        area.name,
        stage="COMPLETED",
        progress=100,
        status="COMPLETED",
        message=(
            f"Analysed {after.scene.date_key} against {baseline.scene.date_key}: "
            f"{len(assessments)} region(s), {benign} seasonal or crop-cycle, {len(created)} new alert(s), "
            f"{len(updated)} repeat detection(s)."
        ),
    )


def _analysis_record(
    db: Session,
    run: MonitoringRun,
    area: MonitoringArea,
    after_link: AOIAcquisition,
    before_link: AOIAcquisition,
    method: str,
    grid: AOIGrid,
    quality: dict[str, Any],
    result: dict[str, Any],
    evidence_path: str | None = None,
) -> AnalysisRecord:
    analysis = AnalysisRecord(
        run_id=run.id,
        aoi_id=area.id,
        before_acquisition_id=before_link.acquisition_id,
        after_acquisition_id=after_link.acquisition_id,
        status="COMPLETED",
        analysis_method=method[:64],
        pixel_area_m2=grid.pixel_area_m2,
        quality_json=json.dumps(quality),
        result_json=json.dumps(result, default=str),
        evidence_path=evidence_path,
        completed_at=utcnow(),
    )
    db.add(analysis)
    db.flush()
    return analysis


def _finish_without_case(
    db: Session, run: MonitoringRun, area: MonitoringArea, status: str, message: str
) -> None:
    if run.origin == ORIGIN_LIVE:
        area.last_checked_at = utcnow()
    _update_run(db, run, area.name, stage=status, progress=100, status=status, message=message)


def _schedule_next(db: Session, run: MonitoringRun, area: MonitoringArea | None) -> None:
    if area is None or run.origin != ORIGIN_LIVE:
        return
    area.next_check_at = utcnow() + timedelta(minutes=area.monitoring_interval_minutes)
    db.commit()


def execute_monitoring_run(run_id: str) -> None:
    """Run every stage for one queued run. All failures end in an auditable run state."""

    db = SessionLocal()
    run: MonitoringRun | None = None
    area: MonitoringArea | None = None
    name: str | None = None
    try:
        run = db.get(MonitoringRun, run_id)
        if run is None:
            return
        area = db.get(MonitoringArea, run.aoi_id)
        if area is None:
            _update_run(
                db,
                run,
                None,
                stage="FAILED",
                progress=100,
                status="FAILED",
                message="Monitoring area no longer exists.",
                error_code="AOI_NOT_FOUND",
            )
            return
        name = area.name
        if area.status != "ACTIVE" and run.origin == ORIGIN_LIVE:
            _update_run(
                db,
                run,
                name,
                stage="SKIPPED_PAUSED",
                progress=100,
                status="SKIPPED_PAUSED",
                message="The monitoring area is paused; no external data request was made.",
            )
            return
        _run_pipeline(db, run, area)
        _schedule_next(db, run, area)
    except RunInterrupted:
        db.rollback()
        if run is not None:
            run.status, run.stage = "QUEUED", "QUEUED"
            run.progress_percent = 0
            run.message = "Returned to the queue during server shutdown."
            db.commit()
    except AOITooLargeError as error:
        _fail(db, run, name, "AOI_TOO_LARGE", "This AOI exceeds the maximum analysis window.", error)
    except ProviderAuthRequired as error:
        db.rollback()
        if run is not None:
            _update_run(
                db,
                run,
                name,
                stage="BLOCKED_CREDENTIALS",
                progress=100,
                status="BLOCKED_CREDENTIALS",
                message="An imagery provider rejected its credentials.",
                error_code="PROVIDER_AUTH_FAILED",
                error_detail=str(error),
            )
    except ProviderError as error:
        _fail(db, run, name, "PROVIDER_ERROR", "Every imagery provider failed for this check.", error)
        logger.warning("Run %s: provider failure: %s", run_id, error)
    except Exception as error:  # Keep a failed run auditable without crashing the worker.
        logger.exception("Run %s failed unexpectedly", run_id)
        _fail(db, run, name, "UNEXPECTED_ERROR", "Monitoring stopped on an unexpected processing error.", error)
    finally:
        db.close()


def _fail(
    db: Session,
    run: MonitoringRun | None,
    area_name: str | None,
    code: str,
    message: str,
    error: Exception,
) -> None:
    db.rollback()
    if run is None:
        return
    try:
        if run.origin == ORIGIN_LIVE:
            area = db.get(MonitoringArea, run.aoi_id)
            if area is not None:
                area.last_checked_at = utcnow()
                area.next_check_at = utcnow() + timedelta(minutes=area.monitoring_interval_minutes)
        _update_run(
            db,
            run,
            area_name,
            stage="FAILED",
            progress=100,
            status="FAILED",
            message=message,
            error_code=code,
            error_detail=str(error)[:2000],
        )
    except Exception:
        logger.exception("Could not record the failure of run %s", run.id)
        db.rollback()
