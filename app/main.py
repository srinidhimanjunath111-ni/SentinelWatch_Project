"""FastAPI application and investigator-facing web dashboard."""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, joinedload

from app.api.monitoring import router as monitoring_router
from app.api.operations import router as operations_router
from app.config import settings
from app.database import SessionLocal, get_db
from app.migrations import migrate_schema
from app.models import Alert, AuditLog, Case, CaseReview, Finding, MonitoringArea, MonitoringRun
from app.schemas import (
    AlertAcknowledge,
    AlertOut,
    CaseDetailOut,
    CaseSummaryOut,
    MetricsOut,
    StatusUpdate,
)
from app.services.alerting import create_alerts_for_case
from app.services.analysis import analyse_pair, write_visual_evidence
from app.services.audit import record_audit
from app.services.monitoring import execute_monitoring_run, queue_due_monitoring_runs
from app.services.reporting import (
    ReportDependencyUnavailable,
    build_case_investigation_report,
    report_filename,
)
from app.services.storage import public_path, save_upload
from app.services.system import ensure_model_version

settings.ensure_directories()


async def _scheduler_loop() -> None:
    """Optional local scheduler. It only queues actual due AOIs and runs the same worker."""

    while True:
        try:
            run_ids = await asyncio.to_thread(queue_due_monitoring_runs)
            for run_id in run_ids:
                asyncio.create_task(asyncio.to_thread(execute_monitoring_run, run_id))
        except Exception:
            # Individual runs contain their own auditable failure state. Scheduler
            # loop errors must not terminate the API server.
            pass
        await asyncio.sleep(max(15, settings.scheduler_poll_seconds))


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    migrate_schema()
    db = SessionLocal()
    try:
        ensure_model_version(db)
    finally:
        db.close()
    scheduler_task = (
        asyncio.create_task(_scheduler_loop()) if settings.enable_monitoring_scheduler else None
    )
    try:
        yield
    finally:
        if scheduler_task:
            scheduler_task.cancel()
            with suppress(asyncio.CancelledError):
                await scheduler_task


app = FastAPI(
    title="SentinelWatch API",
    version="2.0.0",
    description=(
        "Evidence-oriented bitemporal change detection. Results identify suspicious "
        "changes for human review and are not proof of illegal activity."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
app.include_router(monitoring_router)
app.include_router(operations_router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request):
    # Keyword arguments support both current Starlette and older FastAPI template APIs.
    return templates.TemplateResponse(
        request=request, name="index.html", context={"app_name": settings.app_name}
    )


@app.get("/files/{file_path:path}", include_in_schema=False)
def uploaded_file(file_path: str):
    # The route is constrained to the configured storage tree; untrusted URL paths
    # cannot escape it even when they contain ../ segments.
    candidate = Path(file_path).resolve()
    storage_root = settings.storage_dir.resolve()
    if storage_root not in candidate.parents or not candidate.is_file():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Evidence file not found."
        )
    return FileResponse(candidate)


def _case_query(db: Session):
    return db.query(Case).options(
        joinedload(Case.findings), joinedload(Case.alerts), joinedload(Case.reviews)
    )


def _not_synthetic_demo_filter():
    return or_(Case.source.is_(None), ~Case.source.like("Synthetic%"))


def _get_case_or_404(case_id: str, db: Session) -> Case:
    case = _case_query(db).filter(Case.id == case_id).first()
    if not case:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Case not found.")
    return case


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name}


@app.get("/api/cases", response_model=list[CaseSummaryOut])
def list_cases(limit: int = 50, include_demo: bool | None = None, db: Session = Depends(get_db)):
    limit = min(max(limit, 1), 200)
    include_demo = settings.demo_mode if include_demo is None else include_demo
    query = _case_query(db)
    if not include_demo:
        query = query.filter(_not_synthetic_demo_filter())
    return query.order_by(Case.created_at.desc()).limit(limit).all()


@app.get("/api/cases/{case_id}", response_model=CaseDetailOut)
def get_case(case_id: str, db: Session = Depends(get_db)):
    return _get_case_or_404(case_id, db)


@app.get(
    "/api/cases/{case_id}/report",
    responses={
        200: {"content": {"application/pdf": {}}},
        404: {"description": "Case not found."},
        503: {"description": "Optional PDF reporting dependency is not installed."},
    },
)
def download_case_report(case_id: str, db: Session = Depends(get_db)):
    """Download a truthful, scoped investigation report for an existing case."""

    case = _get_case_or_404(case_id, db)
    related_entity_ids = {
        value
        for value in (
            case.id,
            case.aoi_id,
            case.analysis_id,
            case.before_acquisition_id,
            case.after_acquisition_id,
            *(finding.id for finding in case.findings),
            *(alert.id for alert in case.alerts),
        )
        if value
    }
    audit_events = (
        db.query(AuditLog)
        .filter(AuditLog.entity_id.in_(related_entity_ids))
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    try:
        report = build_case_investigation_report(case, audit_events)
    except ReportDependencyUnavailable as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error

    return Response(
        content=report,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{report_filename(case)}"'},
    )


@app.post(
    "/api/analyze",
    response_model=CaseDetailOut,
    status_code=status.HTTP_201_CREATED,
    deprecated=True,
    tags=["developer/testing"],
)
@app.post(
    "/api/developer/analyze-image-pair",
    response_model=CaseDetailOut,
    status_code=status.HTTP_201_CREATED,
    tags=["developer/testing"],
)
async def analyze_images(
    before_image: UploadFile = File(..., description="Earlier acquisition image"),
    after_image: UploadFile = File(..., description="Later acquisition image"),
    title: str = Form("Untitled investigation"),
    location: str | None = Form(None),
    before_date: str | None = Form(None),
    after_date: str | None = Form(None),
    source: str | None = Form(None),
    pixel_resolution_m: float = Form(settings.pixel_resolution_m, gt=0.01, le=1000),
    db: Session = Depends(get_db),
):
    case_id = str(uuid.uuid4())
    before_path = await save_upload(before_image, case_id, "before")
    try:
        after_path = await save_upload(after_image, case_id, "after")
        result = analyse_pair(before_path, after_path, pixel_resolution_m)
    except Exception as error:
        before_path.unlink(missing_ok=True)
        if "after_path" in locals():
            after_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Could not analyse image pair: {error}",
        ) from error

    mask_path = settings.output_dir / f"{case_id}-change-evidence.jpg"
    write_visual_evidence(before_path, after_path, result, mask_path)
    severity_order = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
    highest_severity = max(
        (item.severity for item in result.detections), key=severity_order.get, default="none"
    )
    case = Case(
        id=case_id,
        title=title.strip()[:160] or "Untitled investigation",
        location=(location or "").strip() or None,
        before_date=before_date,
        after_date=after_date,
        source=(source or "").strip() or None,
        pixel_resolution_m=pixel_resolution_m,
        analysis_method=result.method,
        overall_confidence=result.confidence,
        changed_area_px=result.changed_area_pixels,
        changed_area_m2=result.changed_area_m2,
        highest_severity=highest_severity,
        before_image=public_path(before_path),
        after_image=public_path(after_path),
        mask_image=public_path(mask_path),
        data_origin="developer_upload",
        requires_human_verification=True,
        quality_note="Developer/testing image-pair upload; not automatic Copernicus monitoring.",
    )
    db.add(case)
    db.flush()
    for observation in result.detections:
        finding = Finding(
            case_id=case.id,
            activity=observation.activity,
            category=observation.category,
            confidence=observation.confidence,
            severity=observation.severity,
            area_pixels=observation.area_pixels,
            area_m2=observation.area_m2,
            centroid_x=observation.centroid_x,
            centroid_y=observation.centroid_y,
            bbox=",".join(map(str, observation.bbox)),
            ndvi_before=observation.ndvi_before,
            ndvi_after=observation.ndvi_after,
            ndvi_delta=observation.ndvi_delta,
            requires_human_verification=True,
            evidence_json=json.dumps(observation.evidence),
        )
        db.add(finding)
    db.flush()
    create_alerts_for_case(db, case)
    record_audit(db, "case", case.id, "developer_image_pair_analysed")
    db.commit()
    db.refresh(case)
    return _get_case_or_404(case.id, db)


@app.patch("/api/cases/{case_id}/status", response_model=CaseDetailOut)
def update_case_status(case_id: str, payload: StatusUpdate, db: Session = Depends(get_db)):
    case = _get_case_or_404(case_id, db)
    case.status = payload.status
    case.review_note = payload.review_note
    case.reviewed_at = datetime.utcnow() if payload.status != "new" else None
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
    return _get_case_or_404(case_id, db)


@app.get("/api/alerts", response_model=list[AlertOut])
def list_alerts(open_only: bool = False, include_demo: bool | None = None, db: Session = Depends(get_db)):
    include_demo = settings.demo_mode if include_demo is None else include_demo
    query = db.query(Alert).join(Case).order_by(Alert.created_at.desc())
    if not include_demo:
        query = query.filter(_not_synthetic_demo_filter())
    if open_only:
        query = query.filter(Alert.status == "open")
    return query.limit(200).all()


@app.patch("/api/alerts/{alert_id}/acknowledge", response_model=AlertOut)
def acknowledge_alert(alert_id: str, payload: AlertAcknowledge, db: Session = Depends(get_db)):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found.")
    alert.status = "acknowledged"
    alert.acknowledgement_note = payload.acknowledgement_note
    alert.acknowledged_at = datetime.utcnow()
    record_audit(db, "alert", alert.id, "alert_acknowledged")
    db.commit()
    db.refresh(alert)
    return alert


@app.get("/api/metrics", response_model=MetricsOut)
def get_metrics(include_demo: bool | None = None, db: Session = Depends(get_db)):
    include_demo = settings.demo_mode if include_demo is None else include_demo
    case_query = db.query(Case)
    if not include_demo:
        case_query = case_query.filter(_not_synthetic_demo_filter())
    total_cases = case_query.with_entities(func.count(Case.id)).scalar() or 0
    alert_query = db.query(Alert).join(Case)
    if not include_demo:
        alert_query = alert_query.filter(_not_synthetic_demo_filter())
    open_alerts = alert_query.filter(Alert.status == "open").with_entities(func.count(Alert.id)).scalar() or 0
    high_priority = (
        alert_query.filter(Alert.status == "open", Alert.severity.in_(["high", "critical"]))
        .with_entities(func.count(Alert.id))
        .scalar()
        or 0
    )
    changed_area = case_query.with_entities(func.coalesce(func.sum(Case.changed_area_m2), 0.0)).scalar() or 0.0
    activity_counts = dict(
        db.query(Finding.activity, func.count(Finding.id))
        .join(Case)
        .filter(_not_synthetic_demo_filter() if not include_demo else True)
        .group_by(Finding.activity)
        .all()
    )
    method_counts = dict(
        case_query.with_entities(Case.analysis_method, func.count(Case.id))
        .group_by(Case.analysis_method)
        .all()
    )
    return MetricsOut(
        total_cases=total_cases,
        open_alerts=open_alerts,
        high_priority_alerts=high_priority,
        changed_area_m2=float(changed_area),
        activity_counts=activity_counts,
        method_counts=method_counts,
        monitored_areas=db.query(func.count(MonitoringArea.id)).scalar() or 0,
        active_areas=(
            db.query(func.count(MonitoringArea.id)).filter(MonitoringArea.status == "ACTIVE").scalar()
            or 0
        ),
        completed_monitoring_runs=(
            db.query(func.count(MonitoringRun.id))
            .filter(MonitoringRun.status.in_(["COMPLETED", "COMPLETED_NO_SIGNIFICANT_CHANGE"]))
            .scalar()
            or 0
        ),
    )
