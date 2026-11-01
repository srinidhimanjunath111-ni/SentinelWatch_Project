"""Immediate alert dispatch to the responsible department, with truthful delivery status.

Every dispatch records an in-app delivery. Email goes through real SMTP with the case PDF
attached when SMTP is configured; otherwise a real .eml is saved to storage/outbox/ with
status OUTBOX_SAVED, which says plainly that nothing was sent. Failures retry a bounded
number of times and never propagate into the monitoring run.
"""

from __future__ import annotations

import logging
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.database import SessionLocal
from app.models import Alert, AuditLog, Case, Department, NotificationAttempt
from app.services.alerting import alert_query, department_for
from app.services.audit import record_audit
from app.services.events import publish
from app.services.payloads import alert_payload, dispatch_payload
from app.services.reporting import (
    ReportDependencyUnavailable,
    build_case_investigation_report,
    report_filename,
)
from app.services.storage import public_path
from app.timeutil import iso_z, utcnow
from app.vocabulary import AUTHORISED_ACTIVITY, ORIGIN_REPLAY

logger = logging.getLogger(__name__)


def case_report_pdf(db: Session, case: Case) -> bytes | None:
    """The same report as GET /api/cases/{id}/report, or None if it cannot be rendered."""

    related = {
        value
        for value in (
            case.id,
            case.aoi_id,
            case.analysis_id,
            *(finding.id for finding in case.findings),
            *(alert.id for alert in case.alerts),
        )
        if value
    }
    events = (
        db.query(AuditLog)
        .filter(AuditLog.entity_id.in_(related))
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    try:
        return build_case_investigation_report(case, events)
    except ReportDependencyUnavailable as error:
        logger.warning("Dispatch without PDF: %s", error)
    except Exception:  # A report failure must not stop an alert from reaching its department.
        logger.exception("Could not render report for case %s", case.id)
    return None


def _build_email(alert: Alert, department: Department, recipient: str, pdf: bytes | None) -> EmailMessage:
    case = alert.case
    area = case.area
    replay = "[ARCHIVE REPLAY] " if alert.origin == ORIGIN_REPLAY else ""
    message = EmailMessage()
    message["Subject"] = f"{replay}SentinelWatch alert: {alert.title}"
    message["From"] = settings.smtp_from or "alerts@sentinelwatch.example"
    message["To"] = recipient
    lines = [
        f"To: {department.name}",
        "",
        alert.message,
        "",
        f"Site: {area.name if area else case.location}",
        f"Category: {alert.category}",
        f"Verdict: {alert.verdict}",
        f"Scene date: {alert.acquisition_date}",
        f"Changed area: {(alert.area_m2 or 0):,.0f} m2",
    ]
    if alert.origin == ORIGIN_REPLAY:
        lines += ["", "This alert comes from replaying archived imagery, not a new observation."]
    if recipient != department.recipient_email:
        lines += ["", f"Redirected by SMTP_REDIRECT_TO; addressed to {department.recipient_email}."]
    message.set_content("\n".join(lines))
    if pdf:
        message.add_attachment(
            pdf, maintype="application", subtype="pdf", filename=report_filename(case)
        )
    return message


def _send_smtp(message: EmailMessage) -> None:
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
        if settings.smtp_starttls:
            client.starttls()
        if settings.smtp_username:
            client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)


def _deliver_email(
    alert: Alert, department: Department, pdf: bytes | None
) -> tuple[str, str, int, Path | None]:
    """Returns status, detail, attempts used, and the saved file if any."""

    recipient = settings.smtp_redirect_to or department.recipient_email or ""
    message = _build_email(alert, department, recipient, pdf)
    if settings.smtp_ready and recipient:
        total = max(1, settings.dispatch_retries)
        last_error: Exception | None = None
        for attempt in range(1, total + 1):
            try:
                _send_smtp(message)
                return "SENT", f"The SMTP server accepted the message for {recipient}.", attempt, None
            except (OSError, smtplib.SMTPException) as error:
                last_error = error
                logger.warning("SMTP attempt %d/%d failed: %s", attempt, total, error)
                if attempt < total:
                    time.sleep(settings.dispatch_backoff_seconds * attempt)
        path = _save_eml(alert, message)
        return "FAILED", f"SMTP delivery failed after {total} attempts: {last_error}", total, path
    path = _save_eml(alert, message)
    return (
        "OUTBOX_SAVED",
        "Not sent: SMTP is not configured. The message and its attachment were saved to the outbox.",
        1,
        path,
    )


def _save_eml(alert: Alert, message: EmailMessage) -> Path:
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    path = settings.outbox_dir / f"{utcnow():%Y%m%dT%H%M%SZ}-{alert.id}.eml"
    path.write_bytes(bytes(message))
    return path


def dispatch_alert(
    alert_id: str,
    *,
    department_id: str | None = None,
    triggered_by: str = "auto",
    pdf: bytes | None = None,
) -> bool:
    """Dispatch one alert on its own session. Returns False on failure, and never raises."""

    db = SessionLocal()
    try:
        alert = alert_query(db).filter(Alert.id == alert_id).first()
        if alert is None:
            return False
        department = db.get(Department, department_id) if department_id else alert.department
        if department is None:
            area = alert.case.area if alert.case else None
            department = department_for(db, alert.category, area.state if area else None)
        alert.department_id = department.id
        records: list[NotificationAttempt] = [
            NotificationAttempt(
                alert_id=alert.id,
                department_id=department.id,
                channel="IN_APP",
                destination=department.name,
                status="RECORDED",
                detail="Routed to the department inbox in SentinelWatch.",
                triggered_by=triggered_by,
            )
        ]
        # An authorised activity is a logged low-priority notice, so it stays in the inbox.
        if alert.verdict != AUTHORISED_ACTIVITY or triggered_by == "manual":
            report = pdf if pdf is not None else case_report_pdf(db, alert.case)
            status, detail, attempts, path = _deliver_email(alert, department, report)
            records.append(
                NotificationAttempt(
                    alert_id=alert.id,
                    department_id=department.id,
                    channel="EMAIL",
                    destination=settings.smtp_redirect_to or department.recipient_email,
                    status=status,
                    detail=detail,
                    attempts=attempts,
                    file_path=public_path(path) if path else None,
                    triggered_by=triggered_by,
                )
            )
        now = utcnow()
        for record in records:
            db.add(record)
        previous = alert.status
        if alert.status == "open":
            alert.status = "dispatched"
        if alert.dispatched_at is None:
            alert.dispatched_at = now
            if alert.scene_processed_at is not None:
                alert.latency_seconds = round((now - alert.scene_processed_at).total_seconds(), 3)
        record_audit(
            db,
            "alert",
            alert.id,
            "alert_dispatched",
            {
                "department": department.name,
                "previous_status": previous,
                "channels": {record.channel: record.status for record in records},
                "triggered_by": triggered_by,
            },
        )
        db.commit()
        for record in records:
            db.refresh(record)
            publish(
                "dispatch",
                {
                    "alert_id": alert.id,
                    "department": department.name,
                    "channel": record.channel,
                    "status": record.status,
                    "destination": record.destination,
                    "at": iso_z(record.created_at),
                },
            )
        publish("alert", {**alert_payload(alert), "event_kind": "updated"})
        return True
    except Exception:
        logger.exception("Dispatch failed for alert %s", alert_id)
        db.rollback()
        return False
    finally:
        db.close()


def dispatch_many(alert_ids: list[str]) -> None:
    """Dispatch a case's new alerts, rendering its PDF once rather than per alert."""

    if not alert_ids:
        return
    pdf: bytes | None = None
    db = SessionLocal()
    try:
        first = (
            db.query(Alert)
            .options(joinedload(Alert.case))
            .filter(Alert.id == alert_ids[0])
            .first()
        )
        if first is not None:
            pdf = case_report_pdf(db, first.case)
    except Exception:
        logger.exception("Could not prepare a shared report for dispatch")
    finally:
        db.close()
    for alert_id in alert_ids:
        dispatch_alert(alert_id, pdf=pdf)


def smtp_status() -> dict:
    return {
        "configured": settings.smtp_ready,
        "host": settings.smtp_host or None,
        "redirect_to": settings.smtp_redirect_to or None,
        "mode": "smtp" if settings.smtp_ready else "outbox",
    }
