"""AOI configuration, genuine monitoring progress, and system-status endpoints."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import (
    AnalysisRecord,
    AOIAcquisition,
    Case,
    CaseReview,
    ModelVersion,
    MonitoringArea,
    MonitoringRun,
    SatelliteAcquisition,
)
from app.satellite.geometry import geometry_bbox, geometry_center
from app.schemas import (
    AcquisitionOut,
    AnalysisOut,
    ModelVersionOut,
    MonitoringAreaCreate,
    MonitoringAreaOut,
    MonitoringAreaUpdate,
    MonitoringRunOut,
    ReviewCreate,
    ReviewOut,
    SystemStatusOut,
)
from app.services.audit import record_audit
from app.services.monitoring import execute_monitoring_run, queue_monitoring_run
from app.services.system import ensure_model_version, model_version_payload, system_status_payload

router = APIRouter(prefix="/api", tags=["monitoring"])


def _json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value or "")
    except (TypeError, json.JSONDecodeError):
        return fallback


def _run_payload(run: MonitoringRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": run.id,
        "aoi_id": run.aoi_id,
        "status": run.status,
        "stage": run.stage,
        "progress_percent": run.progress_percent,
        "message": run.message,
        "error_code": run.error_code,
        "error_detail": run.error_detail,
        "before_acquisition_id": run.before_acquisition_id,
        "after_acquisition_id": run.after_acquisition_id,
        "started_at": run.started_at,
        "completed_at": run.completed_at,
    }


def area_payload(area: MonitoringArea, latest_run: MonitoringRun | None = None) -> dict:
    return {
        "id": area.id,
        "name": area.name,
        "geometry": _json(area.geometry_json, {}),
        "bbox": _json(area.bbox_json, []),
        "center_lat": area.center_lat,
        "center_lon": area.center_lon,
        "categories": _json(area.categories_json, []),
        "max_cloud_cover": area.max_cloud_cover,
        "min_change_area_m2": area.min_change_area_m2,
        "sensitivity": area.sensitivity,
        "monitoring_interval_minutes": area.monitoring_interval_minutes,
        "status": area.status,
        "created_at": area.created_at,
        "updated_at": area.updated_at,
        "last_checked_at": area.last_checked_at,
        "latest_run": _run_payload(latest_run),
    }


def acquisition_payload(link: AOIAcquisition) -> dict:
    item = link.acquisition
    return {
        "id": item.id,
        "provider": item.provider,
        "product_id": item.product_id,
        "collection": item.collection,
        "satellite": item.satellite,
        "sensor": item.sensor,
        "processing_level": item.processing_level,
        "acquisition_datetime": item.acquisition_datetime,
        "cloud_cover": item.cloud_cover,
        "geometry": _json(item.geometry_json, None),
        "bbox": _json(item.bbox_json, None),
        "source_url": item.source_url,
        "quality_status": item.quality_status,
        "quality_note": item.quality_note,
        "cached_raster": bool(item.cached_raster_path),
        "first_seen_at": link.first_seen_at,
        "selection_role": link.selection_role,
        "last_selected_at": link.last_selected_at,
    }


def analysis_payload(item: AnalysisRecord) -> dict:
    return {
        "id": item.id,
        "aoi_id": item.aoi_id,
        "run_id": item.run_id,
        "status": item.status,
        "analysis_method": item.analysis_method,
        "pixel_area_m2": item.pixel_area_m2,
        "quality": _json(item.quality_json, {}),
        "result": _json(item.result_json, {}),
        "evidence_path": item.evidence_path,
        "created_at": item.created_at,
        "completed_at": item.completed_at,
    }


def _get_area_or_404(aoi_id: str, db: Session) -> MonitoringArea:
    area = db.get(MonitoringArea, aoi_id)
    if area is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring area not found.")
    return area


@router.post("/areas", response_model=MonitoringAreaOut, status_code=status.HTTP_201_CREATED)
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
    return area_payload(area)


@router.get("/areas", response_model=list[MonitoringAreaOut])
def list_areas(db: Session = Depends(get_db)):
    areas = db.query(MonitoringArea).order_by(MonitoringArea.updated_at.desc()).all()
    results: list[dict] = []
    for area in areas:
        latest = (
            db.query(MonitoringRun)
            .filter(MonitoringRun.aoi_id == area.id)
            .order_by(MonitoringRun.started_at.desc())
            .first()
        )
        results.append(area_payload(area, latest))
    return results


@router.get("/areas/{aoi_id}", response_model=MonitoringAreaOut)
def get_area(aoi_id: str, db: Session = Depends(get_db)):
    area = _get_area_or_404(aoi_id, db)
    latest = (
        db.query(MonitoringRun)
        .filter(MonitoringRun.aoi_id == area.id)
        .order_by(MonitoringRun.started_at.desc())
        .first()
    )
    return area_payload(area, latest)


@router.patch("/areas/{aoi_id}", response_model=MonitoringAreaOut)
def update_area(aoi_id: str, payload: MonitoringAreaUpdate, db: Session = Depends(get_db)):
    area = _get_area_or_404(aoi_id, db)
    changes = payload.model_dump(exclude_unset=True)
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
    area.updated_at = datetime.utcnow()
    record_audit(db, "monitoring_area", area.id, "aoi_updated", {"fields": sorted(changes)})
    db.commit()
    db.refresh(area)
    return area_payload(area)


@router.post("/areas/{aoi_id}/pause", response_model=MonitoringAreaOut)
def pause_area(aoi_id: str, db: Session = Depends(get_db)):
    area = _get_area_or_404(aoi_id, db)
    area.status = "PAUSED"
    record_audit(db, "monitoring_area", area.id, "aoi_paused")
    db.commit()
    db.refresh(area)
    return area_payload(area)


@router.post("/areas/{aoi_id}/resume", response_model=MonitoringAreaOut)
def resume_area(aoi_id: str, db: Session = Depends(get_db)):
    area = _get_area_or_404(aoi_id, db)
    area.status = "ACTIVE"
    record_audit(db, "monitoring_area", area.id, "aoi_resumed")
    db.commit()
    db.refresh(area)
    return area_payload(area)


@router.delete("/areas/{aoi_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_area(aoi_id: str, db: Session = Depends(get_db)) -> Response:
    """Delete an unused AOI; preserved analyses/cases deliberately block deletion."""

    area = _get_area_or_404(aoi_id, db)
    has_history = (
        db.query(AnalysisRecord).filter(AnalysisRecord.aoi_id == area.id).first()
        or db.query(Case).filter(Case.aoi_id == area.id).first()
    )
    if has_history:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This AOI has preserved monitoring evidence. Pause it instead of deleting history.",
        )
    record_audit(db, "monitoring_area", area.id, "aoi_deleted")
    db.delete(area)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/areas/{aoi_id}/check-now", response_model=MonitoringRunOut, status_code=status.HTTP_202_ACCEPTED)
def check_area_now(aoi_id: str, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    area = _get_area_or_404(aoi_id, db)
    if area.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Resume this monitoring area before requesting a check.",
        )
    inflight = (
        db.query(MonitoringRun)
        .filter(MonitoringRun.aoi_id == area.id, MonitoringRun.status.in_(["QUEUED", "RUNNING"]))
        .first()
    )
    if inflight:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Monitoring run {inflight.id} is already active for this area.",
        )
    run = queue_monitoring_run(db, area)
    background_tasks.add_task(execute_monitoring_run, run.id)
    return _run_payload(run)


@router.get("/monitoring-runs/{run_id}", response_model=MonitoringRunOut)
def get_monitoring_run(run_id: str, db: Session = Depends(get_db)):
    run = db.get(MonitoringRun, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Monitoring run not found.")
    return _run_payload(run)


@router.get("/areas/{aoi_id}/acquisitions", response_model=list[AcquisitionOut])
def list_area_acquisitions(aoi_id: str, limit: int = 100, db: Session = Depends(get_db)):
    _get_area_or_404(aoi_id, db)
    links = (
        db.query(AOIAcquisition)
        .join(SatelliteAcquisition)
        .filter(AOIAcquisition.aoi_id == aoi_id)
        .order_by(SatelliteAcquisition.acquisition_datetime.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [acquisition_payload(link) for link in links]


@router.get("/areas/{aoi_id}/analyses", response_model=list[AnalysisOut])
def list_area_analyses(aoi_id: str, limit: int = 50, db: Session = Depends(get_db)):
    _get_area_or_404(aoi_id, db)
    analyses = (
        db.query(AnalysisRecord)
        .filter(AnalysisRecord.aoi_id == aoi_id)
        .order_by(AnalysisRecord.created_at.desc())
        .limit(min(max(limit, 1), 200))
        .all()
    )
    return [analysis_payload(item) for item in analyses]


@router.get("/models", response_model=list[ModelVersionOut])
def list_models(db: Session = Depends(get_db)):
    ensure_model_version(db)
    versions = db.query(ModelVersion).order_by(ModelVersion.updated_at.desc()).all()
    return [model_version_payload(version) for version in versions]


@router.get("/system/status", response_model=SystemStatusOut)
def system_status(db: Session = Depends(get_db)):
    return system_status_payload(db)


@router.post("/cases/{case_id}/reviews", response_model=ReviewOut, status_code=status.HTTP_201_CREATED)
def create_case_review(case_id: str, payload: ReviewCreate, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    review = CaseReview(
        case_id=case.id,
        action=payload.action,
        note=payload.note,
        reviewer=payload.reviewer,
    )
    case.status = payload.action
    case.review_note = payload.note
    case.reviewed_at = datetime.utcnow()
    db.add(review)
    db.flush()
    record_audit(
        db,
        "case",
        case.id,
        "human_review_recorded",
        {"action": payload.action, "reviewer": payload.reviewer},
    )
    db.commit()
    db.refresh(review)
    return review
