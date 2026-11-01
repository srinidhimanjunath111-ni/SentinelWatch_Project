"""Truthful notification recording with optional SMTP delivery."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, Department, NotificationAttempt


def record_notification(
    db: Session, alert: Alert, department: Department, *, request_email: bool
) -> NotificationAttempt:
    """Record in-app routing; send email only when all SMTP settings are configured."""

    attempt = NotificationAttempt(
        alert_id=alert.id,
        department_id=department.id,
        channel="IN_APP",
        destination=department.name,
        status="RECORDED",
        detail="Alert routed to configured department in the SentinelWatch inbox.",
    )
    db.add(attempt)
    if not request_email:
        return attempt
    if not department.recipient_email or not settings.smtp_ready:
        db.add(
            NotificationAttempt(
                alert_id=alert.id,
                department_id=department.id,
                channel="EMAIL",
                destination=department.recipient_email,
                status="NOT_CONFIGURED",
                detail="Email was requested but SMTP settings or the department recipient are missing.",
            )
        )
        return attempt
    email = EmailMessage()
    email["Subject"] = f"SentinelWatch review alert: {alert.title}"
    email["From"] = settings.smtp_from
    email["To"] = department.recipient_email
    email.set_content(alert.message)
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
            if settings.smtp_starttls:
                client.starttls()
            if settings.smtp_username:
                client.login(settings.smtp_username, settings.smtp_password)
            client.send_message(email)
        state, detail = "SENT", "SMTP server accepted the email message."
    except (OSError, smtplib.SMTPException) as error:
        state, detail = "FAILED", f"SMTP delivery failed: {error}"
    db.add(
        NotificationAttempt(
            alert_id=alert.id,
            department_id=department.id,
            channel="EMAIL",
            destination=department.recipient_email,
            status=state,
            detail=detail,
        )
    )
    return attempt
