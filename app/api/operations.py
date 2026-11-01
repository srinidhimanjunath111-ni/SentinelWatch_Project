"""Alerts, cases, departments, dispatch log, authorised zones and operational status."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy import case, func
from sqlalchemy.orm import Session, defer, joinedload, selectinload

from app.config import settings
from app.database import engine, get_db
from app.models import (
    Alert,
    AnalysisRecord,
    AuditLog,
    AuthorisedZone,
    Case,
    CaseReview,
    CatalogueRefresh,
    Department,
    Finding,
    MonitoringArea,
    NotificationAttempt,
    PriorityCandidate,
    SatelliteAcquisition,
)
from app.satellite.geometry import geometry_bbox, geometry_center
from app.schemas import (
    AlertAcknowledge,
    AlertAssignment,
    AlertDispatchRequest,
    AlertStatusUpdate,
    AuthorisedZoneCreate,
    CandidateApproval,
    DepartmentCreate,
    PriorityDiscoveryRequest,
    ReviewCreate,
    StatusUpdate,
)
from app.services.alerting import SEVERITY_RANK, alert_query
from app.services.audit import record_audit
from app.services.dispatch import case_report_pdf, dispatch_alert
from app.services.events import publish
from app.services.payloads import (
    alert_payload,
    case_detail_payload,
    case_summary_payload,
    department_payload,
    dispatch_payload,
    load_json,
    zone_payload,
)
from app.services.priority_discovery import discover_priority_candidates
from app.services.reporting import report_filename, reportlab_available
from app.timeutil import iso_z, utcnow
from app.vocabulary import ACTIVE_ALERT_STATUSES, ALERT_TRANSITIONS

router = APIRouter(prefix="/api", tags=["operations"])


# Alerts -------------------------------------------------------------------------------


def _alert_or_404(alert_id: str, db: Session) -> Alert:
    alert = alert_query(db).filter(Alert.id == alert_id).first()
    if alert is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found.")
    return alert


def _dispatches_for(db: Session, alert_id: str) -> list[dict]:
    attempts = (
        db.query(NotificationAttempt)
        .options(joinedload(NotificationAttempt.department))
        .filter(NotificationAttempt.alert_id == alert_id)
        .order_by(NotificationAttempt.created_at.desc())
        .all()
    )
    return [dispatch_payload(attempt) for attempt in attempts]


@router.get("/alerts")
def list_alerts(
    status_filter: str | None = Query(default=None, alias="status"),
    severity: str | None = None,
    category: str | None = None,
    department_id: str | None = None,
    area_id: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    """``status`` also accepts a comma-separated list, or ``active`` for all unresolved."""

    # Newest first; alerts raised together by one scene rank by severity.
    severity_rank = case(
        {name: rank for name, rank in SEVERITY_RANK.items()}, value=Alert.severity, else_=0
    )
    query = alert_query(db).order_by(
        Alert.created_at.desc(), severity_rank.desc(), Alert.priority_score.desc()
    )
    if status_filter == "active":
        query = query.filter(Alert.status.in_(ACTIVE_ALERT_STATUSES))
    elif status_filter:
        query = query.filter(Alert.status.in_(status_filter.split(",")))
    if severity:
        query = query.filter(Alert.severity == severity)
    if category:
        query = query.filter(Alert.category == category)
    if department_id:
        query = query.filter(Alert.department_id == department_id)
    if area_id:
        query = query.filter(Alert.aoi_id == area_id)
    return [alert_payload(alert) for alert in query.limit(min(max(limit, 1), 500)).all()]


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: str, db: Session = Depends(get_db)):
    alert = _alert_or_404(alert_id, db)
    payload = alert_payload(alert)
    payload["case"] = case_summary_payload(alert.case)
    payload["dispatches"] = _dispatches_for(db, alert.id)
    return payload


@router.patch("/alerts/{alert_id}/status")
def update_alert_status(alert_id: str, payload: AlertStatusUpdate, db: Session = Depends(get_db)):
    alert = _alert_or_404(alert_id, db)
    previous = alert.status
    if payload.status != previous and payload.status not in ALERT_TRANSITIONS.get(previous, set()):
        allowed = ", ".join(sorted(ALERT_TRANSITIONS.get(previous, set()))) or "none"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An alert cannot move from {previous} to {payload.status}. Allowed: {allowed}.",
        )
    now = utcnow()
    alert.status = payload.status
    if payload.note:
        alert.status_note = payload.note
    if payload.status == "acknowledged" and alert.acknowledged_at is None:
        alert.acknowledged_at = now
        alert.acknowledgement_note = payload.note
    if payload.status in ("resolved", "false_positive"):
        alert.closed_at = now
    record_audit(
        db,
        "alert",
        alert.id,
        "alert_status_changed",
        {"from": previous, "to": payload.status, "note": payload.note, "actor": payload.actor},
    )
    db.commit()
    db.refresh(alert)
    result = alert_payload(alert)
    publish("alert", {**result, "event_kind": "updated"})
    return result


@router.post("/alerts/{alert_id}/dispatch")
def redispatch_alert(
    alert_id: str, payload: AlertDispatchRequest | None = None, db: Session = Depends(get_db)
):
    alert = _alert_or_404(alert_id, db)
    department_id = payload.department_id if payload else None
    if department_id is not None:
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(status_code=404, detail="Active department not found.")
    if not dispatch_alert(alert.id, department_id=department_id, triggered_by="manual"):
        raise HTTPException(status_code=502, detail="Dispatch failed; see the server log.")
    db.expire_all()
    return get_alert(alert_id, db)


@router.get("/alerts/{alert_id}/notifications")
def list_notifications(alert_id: str, db: Session = Depends(get_db)):
    if db.get(Alert, alert_id) is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    return _dispatches_for(db, alert_id)


@router.patch("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str, payload: AlertAcknowledge, db: Session = Depends(get_db)):
    return update_alert_status(
        alert_id,
        AlertStatusUpdate(status="acknowledged", note=payload.acknowledgement_note),
        db,
    )


@router.post("/alerts/{alert_id}/assign")
def assign_alert(alert_id: str, payload: AlertAssignment, db: Session = Depends(get_db)):
    department = db.get(Department, payload.department_id)
    if db.get(Alert, alert_id) is None:
        raise HTTPException(status_code=404, detail="Alert not found.")
    if department is None or not department.active:
        raise HTTPException(status_code=404, detail="Active department not found.")
    if not dispatch_alert(alert_id, department_id=department.id, triggered_by="manual"):
        raise HTTPException(status_code=502, detail="Dispatch failed; see the server log.")
    db.expire_all()
    return _dispatches_for(db, alert_id)


@router.get("/dispatches")
def list_dispatches(limit: int = 100, db: Session = Depends(get_db)):
    attempts = (
        db.query(NotificationAttempt)
        .options(joinedload(NotificationAttempt.department))
        .order_by(NotificationAttempt.created_at.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    return [dispatch_payload(attempt) for attempt in attempts]


# Cases --------------------------------------------------------------------------------


def _case_detail_query(db: Session):
    return db.query(Case).options(
        selectinload(Case.findings),
        selectinload(Case.alerts).options(
            joinedload(Alert.department),
            joinedload(Alert.finding),
            joinedload(Alert.case),
            selectinload(Alert.regions),
        ),
        selectinload(Case.reviews),
        joinedload(Case.area),
        joinedload(Case.analysis),
    )


def _case_or_404(case_id: str, db: Session) -> Case:
    case = _case_detail_query(db).filter(Case.id == case_id).first()
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    return case


@router.get("/cases")
def list_cases(limit: int = 50, area_id: str | None = None, db: Session = Depends(get_db)):
    query = (
        db.query(Case)
        .options(
            selectinload(Case.findings).options(
                defer(Finding.geometry_json),
                defer(Finding.evidence_json),
                defer(Finding.verdict_reasons_json),
            ),
            selectinload(Case.alerts).load_only(Alert.id, Alert.case_id),
            joinedload(Case.area),
            defer(Case.context_json),
        )
        .order_by(Case.created_at.desc())
    )
    if area_id:
        query = query.filter(Case.aoi_id == area_id)
    return [case_summary_payload(case) for case in query.limit(min(max(limit, 1), 200)).all()]


@router.get("/cases/{case_id}")
def get_case(case_id: str, db: Session = Depends(get_db)):
    return case_detail_payload(_case_or_404(case_id, db))


@router.get(
    "/cases/{case_id}/report",
    responses={
        200: {"content": {"application/pdf": {}}},
        404: {"description": "Case not found."},
        503: {"description": "Optional PDF reporting dependency is not installed."},
    },
)
def download_case_report(case_id: str, db: Session = Depends(get_db)):
    """Download a truthful, scoped investigation report for an existing case."""

    case = _case_or_404(case_id, db)
    if not reportlab_available():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="PDF reporting requires ReportLab. Install requirements-operational.txt first.",
        )
    report = case_report_pdf(db, case)
    if report is None:
        raise HTTPException(status_code=500, detail="The report could not be rendered.")
    return Response(
        content=report,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report_filename(case)}"'},
    )


@router.patch("/cases/{case_id}/status")
def update_case_status(case_id: str, payload: StatusUpdate, db: Session = Depends(get_db)):
    case = _case_or_404(case_id, db)
    case.status = payload.status
    case.review_note = payload.review_note
    case.reviewed_at = utcnow() if payload.status != "new" else None
    if payload.status != "new":
        db.add(
            CaseReview(
                case_id=case.id,
                action=payload.status,
                note=payload.review_note,
                reviewer=payload.reviewer,
            )
        )
    record_audit(
        db,
        "case",
        case.id,
        "case_status_updated",
        {"status": payload.status, "reviewer": payload.reviewer},
    )
    db.commit()
    db.expire_all()
    return case_detail_payload(_case_or_404(case_id, db))


@router.post("/cases/{case_id}/reviews", status_code=status.HTTP_201_CREATED)
def create_case_review(case_id: str, payload: ReviewCreate, db: Session = Depends(get_db)):
    case = db.get(Case, case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    review = CaseReview(
        case_id=case.id, action=payload.action, note=payload.note, reviewer=payload.reviewer
    )
    case.status = payload.action
    case.review_note = payload.note
    case.reviewed_at = utcnow()
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
    return {
        "id": review.id,
        "case_id": review.case_id,
        "action": review.action,
        "note": review.note,
        "reviewer": review.reviewer,
        "created_at": iso_z(review.created_at),
    }


@router.get("/metrics")
def get_metrics(db: Session = Depends(get_db)):
    return {
        "total_cases": db.query(func.count(Case.id)).scalar() or 0,
        "open_alerts": db.query(func.count(Alert.id))
        .filter(Alert.status.in_(ACTIVE_ALERT_STATUSES))
        .scalar()
        or 0,
        "high_priority_alerts": db.query(func.count(Alert.id))
        .filter(Alert.status.in_(ACTIVE_ALERT_STATUSES), Alert.severity.in_(["high", "critical"]))
        .scalar()
        or 0,
        "changed_area_m2": float(db.query(func.coalesce(func.sum(Case.changed_area_m2), 0.0)).scalar() or 0.0),
        "activity_counts": dict(
            db.query(Finding.activity, func.count(Finding.id)).group_by(Finding.activity).all()
        ),
        "method_counts": dict(
            db.query(Case.analysis_method, func.count(Case.id)).group_by(Case.analysis_method).all()
        ),
        "monitored_areas": db.query(func.count(MonitoringArea.id)).scalar() or 0,
    }


# Departments --------------------------------------------------------------------------


@router.post("/departments", status_code=status.HTTP_201_CREATED)
def create_department(payload: DepartmentCreate, db: Session = Depends(get_db)):
    duplicate = (
        db.query(Department).filter(func.lower(Department.name) == payload.name.strip().lower()).first()
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="A department with this name already exists.")
    department = Department(**payload.model_dump(exclude={"name"}), name=payload.name.strip())
    db.add(department)
    db.commit()
    db.refresh(department)
    return department_payload(department)


@router.get("/departments")
def list_departments(db: Session = Depends(get_db)):
    departments = db.query(Department).order_by(Department.name.asc()).all()
    return [department_payload(department) for department in departments]


@router.get("/departments/{department_id}/inbox")
def department_inbox(department_id: str, limit: int = 100, db: Session = Depends(get_db)):
    department = db.get(Department, department_id)
    if department is None:
        raise HTTPException(status_code=404, detail="Department not found.")
    alerts = (
        alert_query(db)
        .filter(Alert.department_id == department_id)
        .order_by(Alert.created_at.desc())
        .limit(min(max(limit, 1), 500))
        .all()
    )
    attempts = (
        db.query(NotificationAttempt)
        .options(joinedload(NotificationAttempt.department))
        .filter(NotificationAttempt.alert_id.in_([alert.id for alert in alerts]))
        .order_by(NotificationAttempt.created_at.desc())
        .all()
        if alerts
        else []
    )
    by_alert: dict[str, list[dict]] = {}
    for attempt in attempts:
        by_alert.setdefault(attempt.alert_id, []).append(dispatch_payload(attempt))
    return {
        "department": department_payload(department),
        "alerts": [
            {**alert_payload(alert), "dispatches": by_alert.get(alert.id, [])} for alert in alerts
        ],
    }


# Authorised zones ---------------------------------------------------------------------


@router.get("/authorised-zones")
def list_zones(db: Session = Depends(get_db)):
    zones = db.query(AuthorisedZone).order_by(AuthorisedZone.created_at.desc()).all()
    return [zone_payload(zone) for zone in zones]


@router.post("/authorised-zones", status_code=status.HTTP_201_CREATED)
def create_zone(payload: AuthorisedZoneCreate, db: Session = Depends(get_db)):
    zone = AuthorisedZone(
        **payload.model_dump(exclude={"geometry"}), geometry_json=json.dumps(payload.geometry)
    )
    db.add(zone)
    db.flush()
    record_audit(
        db,
        "authorised_zone",
        zone.id,
        "zone_created",
        {"name": zone.name, "zone_type": zone.zone_type, "is_illustrative": zone.is_illustrative},
    )
    db.commit()
    db.refresh(zone)
    return zone_payload(zone)


@router.get("/authorised-zones/{zone_id}")
def get_zone(zone_id: str, db: Session = Depends(get_db)):
    zone = db.get(AuthorisedZone, zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Authorised zone not found.")
    return zone_payload(zone)


@router.delete("/authorised-zones/{zone_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_zone(zone_id: str, db: Session = Depends(get_db)) -> Response:
    zone = db.get(AuthorisedZone, zone_id)
    if zone is None:
        raise HTTPException(status_code=404, detail="Authorised zone not found.")
    record_audit(db, "authorised_zone", zone.id, "zone_deleted", {"name": zone.name})
    db.delete(zone)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# Priority discovery -------------------------------------------------------------------


def _candidate_payload(candidate: PriorityCandidate) -> dict:
    return {
        "id": candidate.id,
        "geometry": load_json(candidate.geometry_json, {}),
        "bbox": load_json(candidate.bbox_json, []),
        "region_name": candidate.region_name,
        "discovery_method": candidate.discovery_method,
        "score": candidate.score,
        "explanation": candidate.explanation,
        "evidence": load_json(candidate.evidence_json, {}),
        "status": candidate.status,
        "monitoring_area_id": candidate.monitoring_area_id,
        "created_at": iso_z(candidate.created_at),
        "updated_at": iso_z(candidate.updated_at),
    }


@router.post("/priority-candidates/discover")
def discover_candidates(payload: PriorityDiscoveryRequest, db: Session = Depends(get_db)):
    candidates = discover_priority_candidates(
        db,
        region_name=payload.region_name.strip(),
        geometry=payload.geometry,
        grid_size=payload.grid_size,
        lookback_days=payload.lookback_days,
    )
    ordered = sorted(candidates, key=lambda item: item.score, reverse=True)
    return [_candidate_payload(candidate) for candidate in ordered]


@router.get("/priority-candidates")
def list_candidates(limit: int = 100, db: Session = Depends(get_db)):
    candidates = (
        db.query(PriorityCandidate)
        .order_by(PriorityCandidate.score.desc(), PriorityCandidate.created_at.desc())
        .limit(min(max(limit, 1), 300))
        .all()
    )
    return [_candidate_payload(candidate) for candidate in candidates]


@router.post("/priority-candidates/{candidate_id}/approve")
def approve_candidate(candidate_id: str, payload: CandidateApproval, db: Session = Depends(get_db)):
    candidate = db.get(PriorityCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Priority candidate not found.")
    if candidate.status == "DISMISSED":
        raise HTTPException(status_code=409, detail="Dismissed candidates cannot be approved.")
    if candidate.monitoring_area_id:
        return _candidate_payload(candidate)
    geometry = load_json(candidate.geometry_json, {})
    bbox = geometry_bbox(geometry)
    lat, lon = geometry_center(geometry)
    area = MonitoringArea(
        name=payload.name.strip(),
        geometry_json=json.dumps(geometry),
        bbox_json=json.dumps(bbox),
        center_lat=lat,
        center_lon=lon,
        categories_json=json.dumps(payload.categories),
        min_change_area_m2=payload.min_change_area_m2,
        sensitivity=payload.sensitivity,
        state=payload.state,
        status="ACTIVE",
        next_check_at=utcnow(),
    )
    db.add(area)
    db.flush()
    candidate.status, candidate.monitoring_area_id = "APPROVED", area.id
    record_audit(db, "priority_candidate", candidate.id, "candidate_approved", {"aoi_id": area.id})
    db.commit()
    db.refresh(candidate)
    return _candidate_payload(candidate)


@router.post("/priority-candidates/{candidate_id}/dismiss")
def dismiss_candidate(candidate_id: str, db: Session = Depends(get_db)):
    candidate = db.get(PriorityCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="Priority candidate not found.")
    candidate.status = "DISMISSED"
    record_audit(db, "priority_candidate", candidate.id, "candidate_dismissed")
    db.commit()
    db.refresh(candidate)
    return _candidate_payload(candidate)


# Status -------------------------------------------------------------------------------


@router.get("/data-status")
def data_status(db: Session = Depends(get_db)):
    latest = db.query(CatalogueRefresh).order_by(CatalogueRefresh.created_at.desc()).first()
    failure = (
        db.query(CatalogueRefresh)
        .filter(CatalogueRefresh.status == "FAILED")
        .order_by(CatalogueRefresh.created_at.desc())
        .first()
    )
    newest = db.query(func.max(SatelliteAcquisition.acquisition_datetime)).scalar()
    return {
        "database": "connected",
        "database_engine": engine.dialect.name,
        "monitoring_areas": db.query(func.count(MonitoringArea.id)).scalar() or 0,
        "observations": db.query(func.count(SatelliteAcquisition.id)).scalar() or 0,
        "analyses": db.query(func.count(AnalysisRecord.id)).scalar() or 0,
        "alerts": db.query(func.count(Alert.id)).scalar() or 0,
        "audit_events": db.query(func.count(AuditLog.id)).scalar() or 0,
        "latest_catalogue_query_at": iso_z(latest.created_at) if latest else None,
        "latest_catalogue_query_status": latest.status if latest else None,
        "newest_actual_acquisition_at": iso_z(newest),
        "latest_provider_error": failure.message if failure else None,
        "imagery_credentials_configured": settings.has_copernicus_credentials,
    }
