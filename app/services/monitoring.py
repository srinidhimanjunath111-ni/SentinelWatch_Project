"""Automatic AOI monitoring service with truthful, persisted progress states."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import (
    AnalysisRecord,
    AOIAcquisition,
    Case,
    CatalogueRefresh,
    Finding,
    MonitoringArea,
    MonitoringRun,
    SatelliteAcquisition,
)
from app.satellite.copernicus import CopernicusDataSpaceProvider
from app.satellite.provider import AcquisitionCandidate, ProviderAuthRequired, ProviderError
from app.services.alerting import create_alerts_for_case
from app.services.analysis import (
    AnalysisResult,
    Detection,
    analyse_arrays,
    write_visual_evidence_arrays,
)
from app.services.audit import record_audit
from app.services.geospatial import (
    GeospatialDependencyError,
    SceneQualityError,
    component_geojson,
    prepare_pair,
    write_rgb_preview,
)
from app.services.storage import public_path
from app.services.system import ensure_model_version

FINAL_RUN_STATES = {
    "COMPLETED",
    "COMPLETED_NO_SIGNIFICANT_CHANGE",
    "NO_NEW_ACQUISITION",
    "NO_SUITABLE_ACQUISITION",
    "SKIPPED_PAUSED",
    "BLOCKED_CREDENTIALS",
    "BLOCKED_DEPENDENCY",
    "UNSUITABLE_IMAGERY",
    "FAILED",
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _safe_product_fragment(product_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", product_id).strip("-")[:180] or "product"


def queue_monitoring_run(db: Session, area: MonitoringArea, trigger: str = "manual") -> MonitoringRun:
    run = MonitoringRun(
        aoi_id=area.id,
        status="QUEUED",
        stage="QUEUED",
        progress_percent=0,
        message=f"Queued by {trigger} trigger.",
    )
    db.add(run)
    db.flush()
    record_audit(db, "monitoring_run", run.id, "run_queued", {"trigger": trigger, "aoi_id": area.id})
    db.commit()
    db.refresh(run)
    return run


def _update_run(
    db: Session,
    run: MonitoringRun,
    *,
    stage: str,
    progress: int,
    message: str,
    status: str = "RUNNING",
    error_code: str | None = None,
    error_detail: str | None = None,
) -> None:
    run.status = status
    run.stage = stage
    run.progress_percent = max(0, min(100, int(progress)))
    run.message = message
    run.error_code = error_code
    run.error_detail = error_detail
    if status in FINAL_RUN_STATES:
        run.completed_at = _utcnow()
    record_audit(
        db,
        "monitoring_run",
        run.id,
        "run_state_changed",
        {"stage": stage, "status": status, "progress_percent": run.progress_percent},
    )
    db.commit()


def _candidate_json(candidate: AcquisitionCandidate) -> str:
    return json.dumps(
        {
            "assets": candidate.assets,
            "properties": candidate.raw_properties,
        },
        default=str,
        sort_keys=True,
    )


def _upsert_acquisition(
    db: Session, area: MonitoringArea, candidate: AcquisitionCandidate
) -> SatelliteAcquisition:
    acquisition = (
        db.query(SatelliteAcquisition)
        .filter(SatelliteAcquisition.product_id == candidate.product_id)
        .first()
    )
    values = {
        "provider": "copernicus-data-space",
        "collection": candidate.collection,
        "satellite": candidate.satellite,
        "sensor": candidate.sensor,
        "processing_level": candidate.processing_level,
        "acquisition_datetime": candidate.acquisition_datetime,
        "cloud_cover": candidate.cloud_cover,
        "geometry_json": json.dumps(candidate.geometry) if candidate.geometry else None,
        "bbox_json": json.dumps(candidate.bbox) if candidate.bbox else None,
        "asset_metadata_json": _candidate_json(candidate),
        "source_url": candidate.source_url,
        "quality_status": "DISCOVERED",
        "quality_note": "Discovered through AOI-intersects CDSE STAC query.",
    }
    if acquisition is None:
        acquisition = SatelliteAcquisition(product_id=candidate.product_id, **values)
        db.add(acquisition)
        db.flush()
        record_audit(
            db,
            "satellite_acquisition",
            acquisition.id,
            "catalogue_item_discovered",
            {"product_id": candidate.product_id, "aoi_id": area.id},
        )
    else:
        for key, value in values.items():
            setattr(acquisition, key, value)
    link = (
        db.query(AOIAcquisition)
        .filter(AOIAcquisition.aoi_id == area.id, AOIAcquisition.acquisition_id == acquisition.id)
        .first()
    )
    if link is None:
        db.add(AOIAcquisition(aoi_id=area.id, acquisition_id=acquisition.id))
    return acquisition


def _mark_pair_selection(
    db: Session, area: MonitoringArea, before_id: str, after_id: str
) -> None:
    now = _utcnow()
    links = db.query(AOIAcquisition).filter(AOIAcquisition.aoi_id == area.id).all()
    for link in links:
        if link.acquisition_id == before_id:
            link.selection_role, link.last_selected_at = "BASELINE", now
        elif link.acquisition_id == after_id:
            link.selection_role, link.last_selected_at = "CURRENT", now


def _existing_after_product_ids(db: Session, aoi_id: str) -> set[str]:
    rows = (
        db.query(SatelliteAcquisition.product_id)
        .join(AnalysisRecord, AnalysisRecord.after_acquisition_id == SatelliteAcquisition.id)
        .filter(AnalysisRecord.aoi_id == aoi_id, AnalysisRecord.status == "COMPLETED")
        .all()
    )
    return {row[0] for row in rows}


def _persistence_score(
    db: Session, area_id: str, detection: Detection, image_shape: tuple[int, int]
) -> float:
    """Measure recurrence of same-category regions in prior real analyses of this AOI."""

    height, width = image_shape
    historical = (
        db.query(Finding)
        .join(Case)
        .filter(
            Case.aoi_id == area_id,
            Case.data_origin == "copernicus_operational",
            Finding.category == detection.category,
        )
        .all()
    )
    matches = 0
    for prior in historical:
        normalized_distance = (
            ((prior.centroid_x - detection.centroid_x) / max(width, 1)) ** 2
            + ((prior.centroid_y - detection.centroid_y) / max(height, 1)) ** 2
        ) ** 0.5
        area_ratio = min(prior.area_m2, detection.area_m2) / max(prior.area_m2, detection.area_m2)
        if normalized_distance <= 0.12 and area_ratio >= 0.20:
            matches += 1
    return round(min(1.0, matches / 3.0), 4)


def _highest_severity(detections: list[Detection]) -> str:
    order = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    return max((item.severity for item in detections), key=order.get, default="none")


def _result_payload(result: AnalysisResult, significant_count: int) -> dict[str, Any]:
    return {
        "method": result.method,
        "changed_area_pixels": result.changed_area_pixels,
        "changed_area_m2": round(result.changed_area_m2, 3),
        "highest_confidence": round(result.confidence, 4),
        "component_count": len(result.detections),
        "significant_component_count": significant_count,
        "categories": sorted({item.category for item in result.detections}),
        "requires_human_verification": True,
    }


def _cache_path(area_id: str, candidate: AcquisitionCandidate) -> Path:
    return settings.satellite_cache_dir / area_id / f"{_safe_product_fragment(candidate.product_id)}.tiff"


def _retrieve_scene(
    provider: CopernicusDataSpaceProvider,
    area: MonitoringArea,
    candidate: AcquisitionCandidate,
    acquisition: SatelliteAcquisition,
) -> Path:
    destination = _cache_path(area.id, candidate)
    if not destination.is_file() or destination.stat().st_size == 0:
        download = provider.download_multiband(
            candidate,
            json.loads(area.geometry_json),
            area.max_cloud_cover,
            destination,
        )
        acquisition.band_manifest_json = json.dumps(
            {"bands": download.bands, "request": download.request_metadata}, default=str
        )
    acquisition.cached_raster_path = str(destination)
    acquisition.quality_status = "RETRIEVED"
    acquisition.quality_note = "AOI-cropped multi-band GeoTIFF retrieved through CDSE Process API."
    return destination


def execute_monitoring_run(run_id: str) -> None:
    """Run every stage synchronously inside a worker/background task for one queued run."""

    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        if run is None:
            return
        area = db.get(MonitoringArea, run.aoi_id)
        if area is None:
            _update_run(
                db,
                run,
                stage="FAILED",
                progress=100,
                status="FAILED",
                message="Monitoring area no longer exists.",
                error_code="AOI_NOT_FOUND",
            )
            return
        if area.status != "ACTIVE":
            _update_run(
                db,
                run,
                stage="SKIPPED_PAUSED",
                progress=100,
                status="SKIPPED_PAUSED",
                message="The monitoring area is paused; no external data request was made.",
            )
            return

        provider = CopernicusDataSpaceProvider()
        end = _utcnow()
        start = end - timedelta(days=settings.monitoring_lookback_days)
        interval = f"{start.isoformat()}Z/{end.isoformat()}Z"
        _update_run(
            db,
            run,
            stage="SEARCHING_CATALOGUE",
            progress=10,
            message="Searching the official Copernicus Sentinel-2 L2A catalogue for this AOI.",
        )
        geometry = json.loads(area.geometry_json)
        run.catalogue_query_json = json.dumps(
            {
                "endpoint": provider.stac_url,
                "collection": provider.collection,
                "datetime": interval,
                "max_cloud_cover": area.max_cloud_cover,
                "geometry": geometry,
            }
        )
        db.commit()
        candidates = provider.search_acquisitions(geometry, interval, area.max_cloud_cover)
        db.add(
            CatalogueRefresh(
                provider=getattr(provider, "name", "copernicus-data-space"),
                aoi_id=area.id,
                query_json=run.catalogue_query_json or "{}",
                status="SUCCESS",
                result_count=len(candidates),
                newest_acquisition_at=(candidates[0].acquisition_datetime if candidates else None),
                message=f"CDSE STAC returned {len(candidates)} cloud-filtered item(s).",
            )
        )
        db.commit()
        _update_run(
            db,
            run,
            stage="RECORDING_ACQUISITIONS",
            progress=22,
            message=f"Recording {len(candidates)} real catalogue acquisition(s) and de-duplicating them.",
        )
        acquisitions = {
            candidate.product_id: _upsert_acquisition(db, area, candidate) for candidate in candidates
        }
        db.commit()
        if len(candidates) < 2:
            area.last_checked_at = _utcnow()
            db.commit()
            _update_run(
                db,
                run,
                stage="NO_SUITABLE_ACQUISITION",
                progress=100,
                status="NO_SUITABLE_ACQUISITION",
                message="Fewer than two cloud-filtered Sentinel-2 L2A acquisitions were found in the lookback window.",
            )
            return

        _update_run(
            db,
            run,
            stage="SELECTING_PAIR",
            progress=30,
            message="Applying temporal separation, cloud quality, collection, and processing-level checks.",
        )
        pair = provider.select_pair(candidates, settings.monitoring_min_pair_gap_days)
        if pair is None:
            area.last_checked_at = _utcnow()
            db.commit()
            _update_run(
                db,
                run,
                stage="NO_SUITABLE_ACQUISITION",
                progress=100,
                status="NO_SUITABLE_ACQUISITION",
                message="No comparable acquisition pair satisfied the configured quality and temporal rules.",
            )
            return
        before_acquisition, after_acquisition = (
            acquisitions[pair.before.product_id],
            acquisitions[pair.after.product_id],
        )
        run.before_acquisition_id, run.after_acquisition_id = before_acquisition.id, after_acquisition.id
        _mark_pair_selection(db, area, before_acquisition.id, after_acquisition.id)
        db.commit()
        if pair.after.product_id in _existing_after_product_ids(db, area.id):
            area.last_checked_at = _utcnow()
            db.commit()
            _update_run(
                db,
                run,
                stage="NO_NEW_ACQUISITION",
                progress=100,
                status="NO_NEW_ACQUISITION",
                message="The best current acquisition was already analysed for this AOI; no duplicate case was created.",
            )
            return

        if not provider.has_credentials:
            area.last_checked_at = _utcnow()
            db.commit()
            _update_run(
                db,
                run,
                stage="BLOCKED_CREDENTIALS",
                progress=38,
                status="BLOCKED_CREDENTIALS",
                message="Real catalogue metadata was recorded, but imagery retrieval is blocked until CDSE OAuth credentials are configured.",
                error_code="COPERNICUS_CREDENTIALS_MISSING",
                error_detail="Set COPERNICUS_CLIENT_ID and COPERNICUS_CLIENT_SECRET in .env, then run Check Now again.",
            )
            return

        _update_run(
            db,
            run,
            stage="RETRIEVING_IMAGERY",
            progress=42,
            message="Requesting AOI-cropped B02/B03/B04/B08/SCL/CLM GeoTIFFs from CDSE Process API.",
        )
        before_path = _retrieve_scene(provider, area, pair.before, before_acquisition)
        after_path = _retrieve_scene(provider, area, pair.after, after_acquisition)
        db.commit()

        _update_run(
            db,
            run,
            stage="PREPROCESSING",
            progress=57,
            message="Validating CRS/grid, aligning rasters, normalising reflectance, and masking cloud/shadow/no-data pixels.",
        )
        prepared = prepare_pair(before_path, after_path)
        preview_dir = settings.output_dir / "monitoring" / area.id
        before_preview = preview_dir / f"{run.id}-before.jpg"
        after_preview = preview_dir / f"{run.id}-after.jpg"
        write_rgb_preview(prepared.before_rgb, prepared.valid_mask, before_preview)
        write_rgb_preview(prepared.after_rgb, prepared.valid_mask, after_preview)

        model_version = ensure_model_version(db)
        if model_version.status == "READY":
            _update_run(
                db,
                run,
                stage="RUNNING_SIAMESE_MODEL",
                progress=67,
                message=f"Running installed model version {model_version.name} on the aligned pair.",
            )
        elif settings.allow_baseline_when_model_unavailable:
            _update_run(
                db,
                run,
                stage="RUNNING_BASELINE",
                progress=67,
                message="No validated Siamese checkpoint is available; running the explicitly labelled spectral/NDVI baseline.",
            )
        else:
            _update_run(
                db,
                run,
                stage="MODEL_UNAVAILABLE",
                progress=100,
                status="FAILED",
                message="No validated model is installed and baseline processing is disabled by configuration.",
                error_code="MODEL_NOT_TRAINED",
            )
            return
        result = analyse_arrays(
            prepared.before_rgb,
            prepared.after_rgb,
            prepared.pixel_area_m2,
            before_ndvi=prepared.before_ndvi,
            after_ndvi=prepared.after_ndvi,
            valid_mask=prepared.valid_mask,
            sensitivity=area.sensitivity,
        )

        _update_run(
            db,
            run,
            stage="EXTRACTING_REGIONS",
            progress=78,
            message="Converting change components to areas, categories, bounding boxes, and review evidence.",
        )
        selected_categories = set(json.loads(area.categories_json or "[]"))
        significant = [
            (detection, component)
            for detection, component in zip(result.detections, result.component_masks, strict=True)
            if detection.area_m2 >= area.min_change_area_m2
            and (not selected_categories or detection.category in selected_categories)
        ]
        evidence_path = preview_dir / f"{run.id}-change-evidence.jpg"
        write_visual_evidence_arrays(prepared.before_rgb, prepared.after_rgb, result, evidence_path)
        analysis = AnalysisRecord(
            run_id=run.id,
            aoi_id=area.id,
            before_acquisition_id=before_acquisition.id,
            after_acquisition_id=after_acquisition.id,
            model_version_id=model_version.id,
            status="COMPLETED",
            analysis_method=result.method,
            pixel_area_m2=prepared.pixel_area_m2,
            quality_json=json.dumps(prepared.quality),
            result_json=json.dumps(_result_payload(result, len(significant))),
            evidence_path=public_path(evidence_path),
            completed_at=_utcnow(),
        )
        db.add(analysis)
        db.flush()
        record_audit(
            db,
            "analysis",
            analysis.id,
            "analysis_completed",
            {"method": result.method, "significant_regions": len(significant)},
        )

        if not significant:
            area.last_checked_at = _utcnow()
            db.commit()
            _update_run(
                db,
                run,
                stage="COMPLETED_NO_SIGNIFICANT_CHANGE",
                progress=100,
                status="COMPLETED_NO_SIGNIFICANT_CHANGE",
                message="The real pair was processed; no region met this AOI's configured category and minimum-area thresholds.",
            )
            return

        _update_run(
            db,
            run,
            stage="CREATING_CASE_AND_ALERTS",
            progress=90,
            message="Saving preserved evidence, an investigator case, and review alerts for material regions.",
        )
        highest = _highest_severity([item[0] for item in significant])
        case = Case(
            title=f"{area.name} — potential change — {pair.after.acquisition_datetime.date().isoformat()}",
            location=f"{area.center_lat:.5f}, {area.center_lon:.5f}",
            before_date=pair.before.acquisition_datetime.date().isoformat(),
            after_date=pair.after.acquisition_datetime.date().isoformat(),
            source="Copernicus Data Space / Sentinel-2 L2A",
            pixel_resolution_m=prepared.pixel_resolution_m,
            analysis_method=result.method,
            overall_confidence=max(item[0].confidence for item in significant),
            changed_area_px=sum(item[0].area_pixels for item in significant),
            changed_area_m2=sum(item[0].area_m2 for item in significant),
            highest_severity=highest,
            before_image=public_path(before_preview),
            after_image=public_path(after_preview),
            mask_image=public_path(evidence_path),
            aoi_id=area.id,
            analysis_id=analysis.id,
            before_acquisition_id=before_acquisition.id,
            after_acquisition_id=after_acquisition.id,
            data_origin="copernicus_operational",
            requires_human_verification=True,
            model_version_label=model_version.name,
            quality_note=(
                f"Usable comparison pixels: {prepared.quality['valid_fraction']:.1%}; "
                f"pair selection: {pair.rationale}"
            ),
        )
        db.add(case)
        db.flush()
        for detection, component in significant:
            persistence = _persistence_score(db, area.id, detection, prepared.valid_mask.shape)
            geometry = component_geojson(component, prepared.transform, prepared.crs)
            evidence = dict(detection.evidence)
            evidence.update(
                {
                    "category": detection.category,
                    "persistence_score": persistence,
                    "before_product_id": pair.before.product_id,
                    "after_product_id": pair.after.product_id,
                    "pair_selection": pair.rationale,
                }
            )
            db.add(
                Finding(
                    case_id=case.id,
                    activity=detection.activity,
                    category=detection.category,
                    confidence=detection.confidence,
                    severity=detection.severity,
                    area_pixels=detection.area_pixels,
                    area_m2=detection.area_m2,
                    centroid_x=detection.centroid_x,
                    centroid_y=detection.centroid_y,
                    bbox=",".join(map(str, detection.bbox)),
                    geometry_json=json.dumps(geometry) if geometry else None,
                    ndvi_before=detection.ndvi_before,
                    ndvi_after=detection.ndvi_after,
                    ndvi_delta=detection.ndvi_delta,
                    persistence_score=persistence,
                    requires_human_verification=True,
                    evidence_json=json.dumps(evidence),
                )
            )
        db.flush()
        create_alerts_for_case(db, case, min_change_area_m2=area.min_change_area_m2)
        area.last_checked_at = _utcnow()
        record_audit(
            db,
            "case",
            case.id,
            "case_created_from_real_monitoring",
            {"aoi_id": area.id, "analysis_id": analysis.id, "alert_policy": "human-review-required"},
        )
        db.commit()
        _update_run(
            db,
            run,
            stage="COMPLETED",
            progress=100,
            status="COMPLETED",
            message="Real AOI analysis completed and reviewable evidence/cases were persisted.",
        )
    except ProviderAuthRequired as error:
        if "run" in locals() and run is not None:
            _update_run(
                db,
                run,
                stage="BLOCKED_CREDENTIALS",
                progress=38,
                status="BLOCKED_CREDENTIALS",
                message="Imagery retrieval requires valid CDSE OAuth credentials.",
                error_code="COPERNICUS_AUTH_FAILED",
                error_detail=str(error),
            )
    except GeospatialDependencyError as error:
        if "run" in locals() and run is not None:
            _update_run(
                db,
                run,
                stage="BLOCKED_DEPENDENCY",
                progress=57,
                status="BLOCKED_DEPENDENCY",
                message="Real imagery was retrieved but cannot be preprocessed in this environment.",
                error_code="RASTERIO_NOT_INSTALLED",
                error_detail=str(error),
            )
    except SceneQualityError as error:
        if "run" in locals() and run is not None:
            _update_run(
                db,
                run,
                stage="UNSUITABLE_IMAGERY",
                progress=100,
                status="UNSUITABLE_IMAGERY",
                message="The selected real imagery could not pass geospatial/cloud/no-data quality control.",
                error_code="SCENE_QUALITY_REJECTED",
                error_detail=str(error),
            )
    except ProviderError as error:
        if "run" in locals() and run is not None:
            db.add(
                CatalogueRefresh(
                    provider="copernicus-data-space",
                    aoi_id=run.aoi_id,
                    query_json=run.catalogue_query_json or "{}",
                    status="FAILED",
                    result_count=0,
                    message=str(error),
                )
            )
            db.commit()
            _update_run(
                db,
                run,
                stage="FAILED",
                progress=100,
                status="FAILED",
                message="The official satellite provider request could not complete.",
                error_code="PROVIDER_ERROR",
                error_detail=str(error),
            )
    except Exception as error:  # Keep a failed run auditable without crashing the API worker.
        if "run" in locals() and run is not None:
            _update_run(
                db,
                run,
                stage="FAILED",
                progress=100,
                status="FAILED",
                message="Monitoring stopped because an unexpected processing error occurred.",
                error_code="UNEXPECTED_ERROR",
                error_detail=str(error),
            )
    finally:
        db.close()


def queue_due_monitoring_runs() -> list[str]:
    """Queue due active AOIs for the optional built-in scheduler without duplicate runs."""

    db = SessionLocal()
    try:
        now = _utcnow()
        queued: list[str] = []
        active_areas = db.query(MonitoringArea).filter(MonitoringArea.status == "ACTIVE").all()
        for area in active_areas:
            due_at = area.last_checked_at + timedelta(minutes=area.monitoring_interval_minutes) if area.last_checked_at else now
            has_inflight = (
                db.query(MonitoringRun)
                .filter(MonitoringRun.aoi_id == area.id, MonitoringRun.status.in_(["QUEUED", "RUNNING"]))
                .first()
            )
            if due_at <= now and not has_inflight:
                queued.append(queue_monitoring_run(db, area, trigger="scheduler").id)
        return queued
    finally:
        db.close()
