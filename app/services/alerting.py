"""Configurable alert policy for reviewable, non-legal change evidence."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Alert, Case


def create_alerts_for_case(
    db: Session, case: Case, *, min_change_area_m2: float = 0.0, confidence_threshold: float | None = None
) -> list[Alert]:
    """Create alerts only for material observations that require human verification."""

    threshold = settings.alert_confidence_threshold if confidence_threshold is None else confidence_threshold
    alerts: list[Alert] = []
    for finding in case.findings:
        if finding.confidence < threshold or finding.area_m2 < min_change_area_m2:
            continue
        activity = finding.activity.replace("_", " ")
        category = (finding.category or "OTHER_CHANGE").replace("_", " ").title()
        persistence = finding.persistence_score or 0.0
        # A review-priority heuristic, not a calibrated probability or legal score.
        priority_score = round(
            min(100.0, 100 * finding.confidence * (0.55 + 0.45 * persistence) * min(1.0, finding.area_m2 / 10_000)),
            1,
        )
        title = f"Human review required: potential {activity}"
        message = (
            f"A {finding.severity}-priority {category} pattern covers {finding.area_m2:,.0f} m² "
            f"with a {finding.confidence:.0%} change-confidence signal "
            f"and {persistence:.0%} temporal persistence. This is decision-support evidence only; "
            "verify the source imagery, clouds/shadows, seasonality, permits, and local context "
            "before any escalation."
        )
        alert = Alert(
            case_id=case.id,
            finding_id=finding.id,
            title=title,
            message=message,
            severity=finding.severity,
            priority_score=priority_score,
        )
        db.add(alert)
        alerts.append(alert)
    return alerts
