"""AOI configuration, runs, timeline, replay, and system status."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    Alert,
    AnalysisRecord,
    AOIAcquisition,
    Case,
    CatalogueRefresh,
    Finding,
    MonitoringArea,
    MonitoringRun,
    PriorityCandidate,
    SatelliteAcquisition,
)
from app.satellite.chain import get_chain
from app.satellite.geometry import geometry_bbox, geometry_center
from app.satellite.provider import ProviderError
from app.schemas import (
    MonitoringAreaCreate,
    MonitoringAreaUpdate,
    ReplayRequest,
)
from app.services.audit import record_audit
from app.services.monitoring import queue_monitoring_run
from app.services.payloads import area_payload, load_json, run_payload
from app.services.scheduler import ACTIVE_RUN_STATES, scheduler
from app.services.system import models_payload, overview_payload, system_status_payload
from app.timeutil import iso_z, parse_date, utcnow
from app.vocabulary import ACTIVE_ALERT_STATUSES, ORIGIN_REPLAY

router = APIRouter(prefix="/api", tags=["monitoring"])

_REPLAY_MIN_RADIUS_DAYS = 15



def _area_or_404(aoi_id: str, db: Session) -> MonitoringArea:
    area = db.get(MonitoringArea, aoi_id)
    if area is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring area not found.")
    return area


def _area_payloads(db: Session, areas: list[MonitoringArea]) -> list[dict]:
    """Build area payloads with constant-count queries, however many areas there are."""

    if not areas:
        return []
    ids = [area.id for area in areas]
    latest_rank = (
        db.query(
            MonitoringRun.id.label("id"),
            func.row_number()
            .over(partition_by=MonitoringRun.aoi_id, order_by=MonitoringRun.started_at.desc())
            .label("rank"),
        )
        .filter(MonitoringRun.aoi_id.in_(ids))
        .subquery()
    )
    latest_runs = {
        run.aoi_id: run
        for run in db.query(MonitoringRun)
        .join(latest_rank, MonitoringRun.id == latest_rank.c.id)
        .filter(latest_rank.c.rank == 1)
        .all()
    }
    open_counts = dict(
        db.query(Alert.aoi_id, func.count(Alert.id))
        .filter(Alert.aoi_id.in_(ids), Alert.status.in_(ACTIVE_ALERT_STATUSES))
        .group_by(Alert.aoi_id)
        .all()
    )
    scene_rank = (
        db.query(
            AOIAcquisition.id.label("id"),
            func.row_number()
            .over(
                partition_by=AOIAcquisition.aoi_id,
                order_by=SatelliteAcquisition.acquisition_datetime.desc(),
            )
            .label("rank"),
        )
        .join(SatelliteAcquisition, AOIAcquisition.acquisition_id == SatelliteAcquisition.id)
        .filter(AOIAcquisition.aoi_id.in_(ids), AOIAcquisition.aoi_clear_fraction.isnot(None))
        .subquery()
    )
    newest_scenes = {
        link.aoi_id: (link, acquisition)
        for link, acquisition in db.query(AOIAcquisition, SatelliteAcquisition)
        .join(SatelliteAcquisition, AOIAcquisition.acquisition_id == SatelliteAcquisition.id)
        .join(scene_rank, AOIAcquisition.id == scene_rank.c.id)
        .filter(scene_rank.c.rank == 1)
        .all()
    }
    payloads = []
    for area in areas:
        newest = newest_scenes.get(area.id)
        last_scene = None
        if newest is not None:
            link, acquisition = newest
            last_scene = {
                "date": acquisition.acquisition_datetime.strftime("%Y-%m-%d"),
                "product_id": acquisition.product_id,
                "thumbnail": link.thumbnail,
                "aoi_clear_fraction": link.aoi_clear_fraction,
            }
        payloads.append(
            area_payload(
                area,
                latest_run=latest_runs.get(area.id),
                open_alerts=open_counts.get(area.id, 0),
                last_scene=last_scene,
                newest_scene_status=newest[0].scene_status if newest else None,
            )
        )
    return payloads


def _area_detail(db: Session, area: MonitoringArea) -> dict:
    return _area_payloads(db, [area])[0]


@router.post("/areas", status_code=status.HTTP_201_CREATED)
def create_area(payload: MonitoringAreaCreate, db: Session = Depends(get_db)):
    bbox = geometry_bbox(payload.geometry)
    center_lat, center_lon = geometry_center(payload.geometry)
    area = MonitoringArea(
        name=payload.name.strip(),
        geometry_json=json.dumps(payload.geometry),
        bbox_json=json.dumps(bbox),
        center_lat=center_lat,
        center_lon=center_lon,
        categories_json=json.dumps(payload.categories),
        max_cloud_cover=payload.max_cloud_cover,
        min_change_area_m2=payload.min_change_area_m2,
        sensitivity=payload.sensitivity,
        monitoring_interval_minutes=payload.monitoring_interval_minutes,
        status="ACTIVE",
        state=payload.state,
        district=payload.district,
        site_type=payload.site_type,
        description=payload.description,
        sources_json=json.dumps(payload.sources) if payload.sources else None,
        next_check_at=utcnow(),
    )
    db.add(area)
    db.flush()
    record_audit(
        db,
        "monitoring_area",
        area.id,
        "aoi_created",
        {"name": area.name, "bbox": bbox, "categories": payload.categories},
    )
    db.commit()
    db.refresh(area)
    return _area_detail(db, area)


@router.get("/areas")
def list_areas(db: Session = Depends(get_db)):
    areas = db.query(MonitoringArea).order_by(MonitoringArea.created_at.asc()).all()
    return _area_payloads(db, areas)


@router.get("/areas/{aoi_id}")
def get_area(aoi_id: str, db: Session = Depends(get_db)):
    return _area_detail(db, _area_or_404(aoi_id, db))


@router.patch("/areas/{aoi_id}")
def update_area(aoi_id: str, payload: MonitoringAreaUpdate, db: Session = Depends(get_db)):
    area = _area_or_404(aoi_id, db)
    changes = payload.model_dump(exclude_unset=True)
    fields = sorted(changes)
    if "geometry" in changes:
        geometry = changes.pop("geometry")
        area.geometry_json = json.dumps(geometry)
        area.bbox_json = json.dumps(geometry_bbox(geometry))
        area.center_lat, area.center_lon = geometry_center(geometry)
    if "categories" in changes:
        area.categories_json = json.dumps(changes.pop("categories"))
    for field, value in changes.items():
        if field == "name" and value is not None:
            value = value.strip()
        setattr(area, field, value)
    if "monitoring_interval_minutes" in fields and area.last_checked_at is not None:
        area.next_check_at = area.last_checked_at + timedelta(minutes=area.monitoring_interval_minutes)
    area.updated_at = utcnow()
    record_audit(db, "monitoring_area", area.id, "aoi_updated", {"fields": fields})
    db.commit()
    db.refresh(area)
    return _area_detail(db, area)


@router.post("/areas/{aoi_id}/pause")
def pause_area(aoi_id: str, db: Session = Depends(get_db)):
    area = _area_or_404(aoi_id, db)
    area.status = "PAUSED"
    record_audit(db, "monitoring_area", area.id, "aoi_paused")
    db.commit()
    db.refresh(area)
    return _area_detail(db, area)


@router.post("/areas/{aoi_id}/resume")
def resume_area(aoi_id: str, db: Session = Depends(get_db)):
    area = _area_or_404(aoi_id, db)
    area.status = "ACTIVE"
    area.next_check_at = utcnow()
    record_audit(db, "monitoring_area", area.id, "aoi_resumed")
    db.commit()
    db.refresh(area)
    return _area_detail(db, area)


@router.delete("/areas/{aoi_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_area(aoi_id: str, db: Session = Depends(get_db)) -> Response:
    """Delete an unused AOI; preserved analyses and cases deliberately block deletion."""

    area = _area_or_404(aoi_id, db)
    has_history = (
        db.query(AnalysisRecord.id).filter(AnalysisRecord.aoi_id == area.id).first()
        or db.query(Case.id).filter(Case.aoi_id == area.id).first()
    )
    if has_history:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This AOI has preserved monitoring evidence. Pause it instead of deleting history.",
        )
    if db.query(MonitoringRun.id).filter(
        MonitoringRun.aoi_id == area.id, MonitoringRun.status.in_(ACTIVE_RUN_STATES)
    ).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A monitoring run is active for this AOI. Wait for it to finish.",
        )
    record_audit(
        db, "monitoring_area", area.id, "aoi_deleted", {"name": area.name, "site_key": area.site_key}
    )
    db.query(CatalogueRefresh).filter(CatalogueRefresh.aoi_id == area.id).update(
        {CatalogueRefresh.aoi_id: None}
    )
    db.query(PriorityCandidate).filter(PriorityCandidate.monitoring_area_id == area.id).update(
        {PriorityCandidate.monitoring_area_id: None}
    )
    db.delete(area)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/areas/{aoi_id}/check-now", status_code=status.HTTP_202_ACCEPTED)
def check_area_now(aoi_id: str, db: Session = Depends(get_db)):
    area = _area_or_404(aoi_id, db)
    if area.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resume this monitoring area before requesting a check.",
        )
    run = scheduler.queue_check(area.id, trigger="manual")
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A monitoring run is already active for this area.",
        )
    return run_payload(run)


def _replay_dates(area: MonitoringArea, start: date, end: date, max_scenes: int) -> list[str]:
    """Real scene dates spread evenly from start to end, nearest each even step.

    Each step needs only a small catalogue search, so a multi-year window stays quick and
    its first and last dates are the scenes closest to what was asked for. Tile cloud only
    budgets the limited scene count here; each run still measures the AOI itself, and the
    AOI's field can widen it for a known scene whose AOI was clear under a cloudy tile.
    """

    geometry = json.loads(area.geometry_json)
    span = (end - start).days
    count = 1 if span == 0 else max_scenes
    targets = [
        start + timedelta(days=round(span * index / max(1, count - 1))) if count > 1 else end
        for index in range(count)
    ]
    radius = max(_REPLAY_MIN_RADIUS_DAYS, span // (2 * max(1, count - 1)))
    chain = get_chain()

    def nearest(target: date) -> str | None:
        low = max(start, target - timedelta(days=radius))
        high = min(end, target + timedelta(days=radius))
        scenes = chain.search_scenes(
            geometry,
            datetime(low.year, low.month, low.day),
            datetime(high.year, high.month, high.day, 23, 59, 59),
            limit=100,
        )
        usable = [
            scene
            for scene in scenes
            if scene.cloud_cover is None or scene.cloud_cover <= area.max_cloud_cover
        ]
        if not usable:
            return None
        moment = datetime(target.year, target.month, target.day)
        best = min(usable, key=lambda scene: abs(scene.acquisition_datetime - moment))
        return best.date_key

    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="replay-search") as pool:
        found = list(pool.map(nearest, targets))
    return sorted({value for value in found if value})


@router.post("/areas/{aoi_id}/replay", status_code=status.HTTP_202_ACCEPTED)
def replay_area(aoi_id: str, payload: ReplayRequest, db: Session = Depends(get_db)):
    """Walk real past scenes in date order through the same pipeline as a live check."""

    area = _area_or_404(aoi_id, db)
    start, end = parse_date(payload.start_date), parse_date(payload.end_date)
    if start is None or end is None or start > end:
        raise HTTPException(status_code=422, detail="start_date must not be after end_date.")
    if end > utcnow().date():
        raise HTTPException(status_code=422, detail="Replay only covers past imagery.")
    if db.query(MonitoringRun.id).filter(
        MonitoringRun.aoi_id == area.id, MonitoringRun.status.in_(ACTIVE_RUN_STATES)
    ).first():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A monitoring run is active for this area. Try again when it finishes.",
        )
    try:
        dates = _replay_dates(area, start, end, payload.max_scenes)
    except ProviderError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The imagery catalogue could not be reached: {error}",
        ) from error
    if not dates:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No catalogue scenes with usable cloud cover exist in that date range.",
        )
    replay_id = str(uuid.uuid4())
    run_ids = []
    for as_of in dates:
        run = queue_monitoring_run(
            db,
            area,
            trigger="replay",
            origin=ORIGIN_REPLAY,
            as_of=as_of,
            replay_id=replay_id,
            force=payload.force,
        )
        run_ids.append(run.id)
        scheduler.submit(run.id, area.id)
    record_audit(
        db,
        "monitoring_area",
        area.id,
        "replay_started",
        {"replay_id": replay_id, "dates": dates, "force": payload.force},
    )
    db.commit()
    return {"replay_id": replay_id, "run_ids": run_ids, "dates": dates}


@router.get("/areas/{aoi_id}/timeline")
def area_timeline(aoi_id: str, limit: int = 120, db: Session = Depends(get_db)):
    """One entry per observed scene date, newest first."""

    _area_or_404(aoi_id, db)
    rows = (
        db.query(AOIAcquisition, SatelliteAcquisition)
        .join(SatelliteAcquisition, AOIAcquisition.acquisition_id == SatelliteAcquisition.id)
        .filter(AOIAcquisition.aoi_id == aoi_id)
        .order_by(SatelliteAcquisition.acquisition_datetime.desc())
        .limit(min(max(limit, 1) * 3, 1500))
        .all()
    )
    case_ids = {link.case_id for link, _ in rows if link.case_id}
    categories: dict[str, set[str]] = {}
    if case_ids:
        for case_id, category in (
            db.query(Finding.case_id, Finding.category).filter(Finding.case_id.in_(case_ids)).all()
        ):
            categories.setdefault(case_id, set()).add(category)
    entries: dict[str, dict[str, Any]] = {}
    for link, acquisition in rows:
        date_key = acquisition.acquisition_datetime.strftime("%Y-%m-%d")
        entry = {
            "date": date_key,
            "product_id": acquisition.product_id,
            "cloud_cover": acquisition.cloud_cover,
            "aoi_clear_fraction": link.aoi_clear_fraction,
            "status": link.scene_status,
            "thumbnail": link.thumbnail,
            "case_id": link.case_id,
            "categories": sorted(categories.get(link.case_id, [])) if link.case_id else [],
            "satellite": acquisition.satellite,
        }
        current = entries.get(date_key)
        # Two tiles can cover one date; the record with more information wins.
        if current is None or (entry["aoi_clear_fraction"] or -1) > (current["aoi_clear_fraction"] or -1):
            entries[date_key] = entry
    return list(entries.values())[: max(1, limit)]


@router.get("/monitoring-runs/{run_id}")
def get_monitoring_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(MonitoringRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring run not found.")
    return run_payload(run)


@router.get("/areas/{aoi_id}/acquisitions")
def list_area_acquisitions(aoi_id: str, limit: int = 100, db: Session = Depends(get_db)):
    _area_or_404(aoi_id, db)
    rows = (
        db.query(AOIAcquisition, SatelliteAcquisition)
        .join(SatelliteAcquisition, AOIAcquisition.acquisition_id == SatelliteAcquisition.id)
        .filter(AOIAcquisition.aoi_id == aoi_id)
        .order_by(SatelliteAcquisition.acquisition_datetime.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [
        {
            "id": acquisition.id,
            "provider": acquisition.provider,
            "product_id": acquisition.product_id,
            "collection": acquisition.collection,
            "satellite": acquisition.satellite,
            "sensor": acquisition.sensor,
            "processing_level": acquisition.processing_level,
            "acquisition_datetime": iso_z(acquisition.acquisition_datetime),
            "cloud_cover": acquisition.cloud_cover,
            "aoi_clear_fraction": link.aoi_clear_fraction,
            "bbox": load_json(acquisition.bbox_json, None),
            "source_url": acquisition.source_url,
            "status": link.scene_status,
            "first_seen_at": iso_z(link.first_seen_at),
        }
        for link, acquisition in rows
    ]


@router.get("/areas/{aoi_id}/analyses")
def list_area_analyses(aoi_id: str, limit: int = 50, db: Session = Depends(get_db)):
    _area_or_404(aoi_id, db)
    analyses = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.aoi_id == aoi_id)
        .order_by(AnalysisRecord.created_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [
        {
            "id": item.id,
            "aoi_id": item.aoi_id,
            "run_id": item.run_id,
            "status": item.status,
            "analysis_method": item.analysis_method,
            "pixel_area_m2": item.pixel_area_m2,
            "quality": load_json(item.quality_json, {}),
            "result": load_json(item.result_json, {}),
            "evidence_path": item.evidence_path,
            "created_at": iso_z(item.created_at),
            "completed_at": iso_z(item.completed_at),
        }
        for item in analyses
    ]


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    return overview_payload(db)


@router.get("/models")
def list_models(request: Request):
    return models_payload(str(request.base_url))


@router.get("/system/status")
def system_status(db: Session = Depends(get_db)):
    return system_status_payload(db)
