"""Priority discovery, transparent data status, and local alert routing APIs."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.database import engine, get_db
from app.models import (
    Alert,
    AnalysisRecord,
    CatalogueRefresh,
    Department,
    MonitoringArea,
    NotificationAttempt,
    PriorityCandidate,
    SatelliteAcquisition,
)
from app.satellite.geometry import geometry_bbox, geometry_center
from app.schemas import (
    AlertAssignment,
    CandidateApproval,
    DataStatusOut,
    DepartmentCreate,
    DepartmentOut,
    NotificationOut,
    PriorityCandidateOut,
    PriorityDiscoveryRequest,
)
from app.services.audit import record_audit
from app.services.notifications import record_notification
from app.services.priority_discovery import discover_priority_candidates

router = APIRouter(prefix="/api", tags=["operations"])


def _json(value: str, fallback):
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def candidate_payload(candidate: PriorityCandidate) -> dict:
    return {
        "id": candidate.id,
        "geometry": _json(candidate.geometry_json, {}),
        "bbox": _json(candidate.bbox_json, []),
        "region_name": candidate.region_name,
        "discovery_method": candidate.discovery_method,
        "score": candidate.score,
        "explanation": candidate.explanation,
        "evidence": _json(candidate.evidence_json, {}),
        "status": candidate.status,
        "monitoring_area_id": candidate.monitoring_area_id,
        "created_at": candidate.created_at,
        "updated_at": candidate.updated_at,
    }


@router.post("/priority-candidates/discover", response_model=list[PriorityCandidateOut])
def discover_candidates(payload: PriorityDiscoveryRequest, db: Session = Depends(get_db)):
    candidates = discover_priority_candidates(
        db,
        region_name=payload.region_name.strip(),
        geometry=payload.geometry,
        grid_size=payload.grid_size,
        max_cloud_cover=payload.max_cloud_cover,
        lookback_days=payload.lookback_days,
    )
    return [candidate_payload(candidate) for candidate in sorted(candidates, key=lambda item: item.score, reverse=True)]


@router.get("/priority-candidates", response_model=list[PriorityCandidateOut])
def list_candidates(limit: int = 100, db: Session = Depends(get_db)):
    candidates = (
        db.query(PriorityCandidate)
        .order_by(PriorityCandidate.score.desc(), PriorityCandidate.created_at.desc())
        .limit(min(max(limit, 1), 300))
        .all()
    )
    return [candidate_payload(candidate) for candidate in candidates]


@router.post("/priority-candidates/{candidate_id}/approve", response_model=PriorityCandidateOut)
def approve_candidate(
    candidate_id: str, payload: CandidateApproval, db: Session = Depends(get_db)
):
    candidate = db.get(PriorityCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Priority candidate not found.")
    if candidate.status == "DISMISSED":
        raise HTTPException(status_code=409, detail="Dismissed candidates cannot be approved.")
    if candidate.monitoring_area_id:
        return candidate_payload(candidate)
    geometry = _json(candidate.geometry_json, {})
    bbox = geometry_bbox(geometry)
    lat, lon = geometry_center(geometry)
    area = MonitoringArea(
        name=payload.name.strip(),
        geometry_json=json.dumps(geometry),
        bbox_json=json.dumps(bbox),
        center_lat=lat,
        center_lon=lon,
        categories_json=json.dumps(sorted({item.strip().upper() for item in payload.categories})),
        max_cloud_cover=payload.max_cloud_cover,
        min_change_area_m2=payload.min_change_area_m2,
        sensitivity=payload.sensitivity,
        status="ACTIVE",
    )
    db.add(area)
    db.flush()
    candidate.status, candidate.monitoring_area_id = "APPROVED", area.id
    record_audit(db, "priority_candidate", candidate.id, "candidate_approved", {"aoi_id": area.id})
    db.commit()
    db.refresh(candidate)
    return candidate_payload(candidate)


@router.post("/priority-candidates/{candidate_id}/dismiss", response_model=PriorityCandidateOut)
def dismiss_candidate(candidate_id: str, db: Session = Depends(get_db)):
    candidate = db.get(PriorityCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Priority candidate not found.")
    candidate.status = "DISMISSED"
    record_audit(db, "priority_candidate", candidate.id, "candidate_dismissed")
    db.commit()
    db.refresh(candidate)
    return candidate_payload(candidate)


@router.post("/departments", response_model=DepartmentOut, status_code=status.HTTP_201_CREATED)
def create_department(payload: DepartmentCreate, db: Session = Depends(get_db)):
    duplicate = db.query(Department).filter(func.lower(Department.name) == payload.name.strip().lower()).first()
    if duplicate:
        raise HTTPException(status_code=409, detail="A department with this name already exists.")
    department = Department(**payload.model_dump(exclude={"name"}), name=payload.name.strip())
    db.add(department)
    db.commit()
    db.refresh(department)
    return department


@router.get("/departments", response_model=list[DepartmentOut])
def list_departments(db: Session = Depends(get_db)):
    return db.query(Department).order_by(Department.name.asc()).all()


@router.post("/alerts/{alert_id}/assign", response_model=list[NotificationOut])
def assign_alert(alert_id: str, payload: AlertAssignment, db: Session = Depends(get_db)):
    alert = db.get(Alert, alert_id)
    department = db.get(Department, payload.department_id)
    if alert is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if department is None or not department.active:
        raise HTTPException(status_code=404, detail="Active department not found.")
    alert.department_id, alert.status = department.id, "assigned"
    record_notification(db, alert, department, request_email=payload.notify_email)
    record_audit(db, "alert", alert.id, "alert_assigned", {"department_id": department.id})
    db.commit()
    return db.query(NotificationAttempt).filter(NotificationAttempt.alert_id == alert.id).order_by(NotificationAttempt.created_at.desc()).all()


@router.get("/alerts/{alert_id}/notifications", response_model=list[NotificationOut])
def list_notifications(alert_id: str, db: Session = Depends(get_db)):
    if db.get(Alert, alert_id) is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    return db.query(NotificationAttempt).filter(NotificationAttempt.alert_id == alert_id).all()


@router.get("/data-status", response_model=DataStatusOut)
def data_status(db: Session = Depends(get_db)):
    latest = db.query(CatalogueRefresh).order_by(CatalogueRefresh.created_at.desc()).first()
    failure = db.query(CatalogueRefresh).filter(CatalogueRefresh.status == "FAILED").order_by(CatalogueRefresh.created_at.desc()).first()
    newest = db.query(func.max(SatelliteAcquisition.acquisition_datetime)).scalar()
    return DataStatusOut(
        database="connected",
        database_engine=engine.dialect.name,
        monitoring_areas=db.query(func.count(MonitoringArea.id)).scalar() or 0,
        observations=db.query(func.count(SatelliteAcquisition.id)).scalar() or 0,
        analyses=db.query(func.count(AnalysisRecord.id)).scalar() or 0,
        alerts=db.query(func.count(Alert.id)).scalar() or 0,
        latest_catalogue_query_at=latest.created_at if latest else None,
        latest_catalogue_query_status=latest.status if latest else None,
        newest_actual_acquisition_at=newest,
        latest_provider_error=failure.message if failure else None,
        imagery_credentials_configured=settings.has_copernicus_credentials,
    )
