"""Alert policy: which findings alert, who is responsible, and repeat-detection merging.

A satellite cannot establish legality, so alert wording stays at "suspected" and asks for
verification. Seeded department mailboxes use the reserved .example domain only.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from datetime import datetime
from typing import Any

import numpy as np
from sqlalchemy.orm import Session, joinedload

from app.models import Alert, Case, Department, Finding, MonitoringArea
from app.satellite.grid import AOIGrid, rasterize_geometry
from app.services.audit import record_audit
from app.vocabulary import (
    ACTIVE_ALERT_STATUSES,
    AUTHORISED_ACTIVITY,
    DEFAULT_ROUTE,
    DEPARTMENT_TEMPLATES,
    NATURAL_CAUSED,
    NATURAL_EVENT,
    NEEDS_REVIEW,
    ORIGIN_REPLAY,
    ROUTING,
    SEASONAL_BENIGN,
    SUSPECTED_UNAUTHORISED,
)

logger = logging.getLogger(__name__)

# Share of the smaller region overlapping before two detections count as the same change.
DUPLICATE_OVERLAP = 0.2
_VERDICT_WEIGHT = {
    SUSPECTED_UNAUTHORISED: 1.0,
    NATURAL_EVENT: 0.9,
    NEEDS_REVIEW: 0.6,
    AUTHORISED_ACTIVITY: 0.2,
}
_department_lock = threading.Lock()


def _mailbox_slug(state: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", state.lower()).strip("-") or "india"


def department_for(db: Session, category: str | None, state: str | None) -> Department:
    """Find or create the responsible department for a category in a state."""

    route = ROUTING.get(category or "", DEFAULT_ROUTE)
    template, prefix = DEPARTMENT_TEMPLATES[route]
    region = (state or "India").strip() or "India"
    name = template.format(state=region)
    with _department_lock:
        department = db.query(Department).filter(Department.name == name).first()
        if department is None:
            department = Department(
                name=name,
                category=route,
                state=region,
                recipient_email=f"{prefix}.{_mailbox_slug(region)}@sentinelwatch.example",
                active=True,
            )
            db.add(department)
            # Committed here so a second worker cannot try to create the same department.
            db.commit()
    return department


def priority_score(verdict: str | None, confidence: float, area_m2: float) -> float:
    """A review-ranking heuristic from 0 to 100, not a probability or a legal score."""

    weight = _VERDICT_WEIGHT.get(verdict or "", 0.5)
    size = min(1.0, max(0.0, math.log10(max(area_m2, 100.0) / 100.0) / 3.0))
    return round(100.0 * weight * (0.45 * confidence + 0.35 * size + 0.2), 1)


def _describe(finding: Finding, area_name: str, area_m2: float) -> tuple[str, str]:
    category = (finding.category or "OTHER_CHANGE").replace("_", " ").lower()
    hectares = area_m2 / 10_000
    if finding.verdict == SUSPECTED_UNAUTHORISED:
        title = f"Suspected unauthorised {category} at {area_name}"
        lead = f"Physical change consistent with {category} covering {hectares:.2f} ha, with no authorisation on record."
    elif finding.verdict == NATURAL_EVENT:
        title = f"Possible {category} at {area_name}"
        lead = f"A {category} pattern covering {hectares:.2f} ha."
    elif finding.verdict == AUTHORISED_ACTIVITY:
        title = f"Notice: {category} inside a registered zone at {area_name}"
        lead = f"Change consistent with {category} covering {hectares:.2f} ha, inside a registered zone."
    else:
        title = f"Change needing review at {area_name}"
        lead = f"Material change covering {hectares:.2f} ha with no determined cause."
    return (
        title[:180],
        f"{lead} Satellite evidence supports a review and does not establish legality; "
        "field verification is required.",
    )


def _overlaps(prior: Alert, region: np.ndarray, grid: AOIGrid) -> bool:
    geometry = prior.finding.geometry_json if prior.finding is not None else None
    if not geometry:
        return False
    try:
        prior_mask = rasterize_geometry(json.loads(geometry), grid)
    except Exception as error:  # A malformed legacy geometry cannot match anything.
        logger.info("Skipping overlap test against alert %s: %s", prior.id, error)
        return False
    smaller = min(int(prior_mask.sum()), int(region.sum()))
    return smaller > 0 and int((prior_mask & region).sum()) / smaller >= DUPLICATE_OVERLAP


def create_or_merge_alerts(
    db: Session,
    case: Case,
    area: MonitoringArea,
    regions: dict[str, np.ndarray],
    grid: AOIGrid,
    *,
    confidence_threshold: float,
    processed_at: datetime,
) -> tuple[list[Alert], list[Alert]]:
    """Create alerts for this case's findings, merging repeats into open alerts.

    Does not commit. Returns (new alerts, updated alerts). A live detection merges only with
    live alerts, and a replay only with alerts from the same replay run.
    """

    selected = set(json.loads(area.categories_json or "[]"))
    active = (
        db.query(Alert)
        .options(joinedload(Alert.finding))
        .filter(
            Alert.aoi_id == area.id,
            Alert.status.in_(ACTIVE_ALERT_STATUSES),
            Alert.origin == case.origin,
        )
    )
    if case.origin == ORIGIN_REPLAY:
        active = active.filter(Alert.replay_id == case.replay_id)
    open_alerts = active.all()
    created: list[Alert] = []
    updated: list[Alert] = []
    for finding in case.findings:
        if finding.verdict == SEASONAL_BENIGN:
            continue
        reasons: list[str] = json.loads(finding.verdict_reasons_json or "[]")
        if finding.confidence < confidence_threshold:
            reasons.append(
                f"Evidence score {finding.confidence:.2f} is below the alert threshold "
                f"{confidence_threshold:.2f}, so this is retained without an alert."
            )
            finding.verdict_reasons_json = json.dumps(reasons)
            continue
        # Natural hazards always alert. The AOI's list narrows only human-caused categories.
        if selected and finding.category not in selected and finding.category not in NATURAL_CAUSED:
            reasons.append(
                f"{finding.category} is not among the categories monitored on this AOI, so no alert was raised."
            )
            finding.verdict_reasons_json = json.dumps(reasons)
            continue
        region = regions.get(finding.id)
        duplicate = None
        if region is not None:
            duplicate = next(
                (
                    prior
                    for prior in open_alerts
                    if prior.category == finding.category and _overlaps(prior, region, grid)
                ),
                None,
            )
        if duplicate is not None:
            duplicate.persistence_count = (duplicate.persistence_count or 1) + 1
            duplicate.last_seen_at = processed_at
            duplicate.area_m2 = max(duplicate.area_m2 or 0.0, finding.area_m2)
            duplicate.confidence = max(duplicate.confidence or 0.0, finding.confidence)
            duplicate.priority_score = priority_score(
                duplicate.verdict, duplicate.confidence, duplicate.area_m2
            )
            finding.persistence_score = round(min(1.0, duplicate.persistence_count / 3.0), 4)
            reasons.append(
                f"This repeats an open alert first raised for {duplicate.acquisition_date}; "
                f"it has now been seen {duplicate.persistence_count} times, and no new alert was created."
            )
            finding.verdict_reasons_json = json.dumps(reasons)
            record_audit(
                db,
                "alert",
                duplicate.id,
                "alert_persistence_updated",
                {"finding_id": finding.id, "count": duplicate.persistence_count, "case_id": case.id},
            )
            updated.append(duplicate)
            continue
        title, message = _describe(finding, area.name, finding.area_m2)
        department = department_for(db, finding.category, area.state)
        alert = Alert(
            case_id=case.id,
            finding_id=finding.id,
            title=title,
            message=message,
            severity=finding.severity,
            status="open",
            department_id=department.id,
            priority_score=priority_score(finding.verdict, finding.confidence, finding.area_m2),
            category=finding.category,
            verdict=finding.verdict,
            origin=case.origin,
            replay_id=case.replay_id,
            aoi_id=area.id,
            confidence=finding.confidence,
            area_m2=finding.area_m2,
            acquisition_date=case.after_date,
            detected_at=processed_at,
            scene_processed_at=processed_at,
            last_seen_at=processed_at,
            persistence_count=1,
        )
        db.add(alert)
        db.flush()
        record_audit(
            db,
            "alert",
            alert.id,
            "alert_created",
            {"category": finding.category, "verdict": finding.verdict, "department": department.name},
        )
        created.append(alert)
    return created, updated


def alert_query(db: Session) -> Any:
    """Alerts with everything the payload reads, loaded in one query."""

    return db.query(Alert).options(
        joinedload(Alert.case).joinedload(Case.area),
        joinedload(Alert.department),
        joinedload(Alert.finding),
    )
