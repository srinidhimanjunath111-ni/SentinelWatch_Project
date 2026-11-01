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
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import Alert, Case, Department, Finding, MonitoringArea
from app.satellite.grid import AOIGrid, rasterize_geometry
from app.services.analysis import WEAK_CAUSE_STRENGTH, severity_for
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
# Below this, a change with no specific cause does not alert on its own.
TINY_OTHER_CHANGE_M2 = 10_000.0
SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3, "critical": 4}
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


def _describe(
    category_name: str | None, verdict: str | None, area_name: str, area_m2: float, regions: int
) -> tuple[str, str]:
    category = (category_name or "OTHER_CHANGE").replace("_", " ").lower()
    size = f"{area_m2 / 10_000:.2f} ha"
    if regions > 1:
        size += f" across {regions} regions"
    if verdict == SUSPECTED_UNAUTHORISED:
        title = f"Suspected unauthorised {category} at {area_name}"
        lead = f"Physical change consistent with {category} covering {size}, with no authorisation on record."
    elif verdict == NATURAL_EVENT:
        title = f"Possible {category} at {area_name}"
        lead = f"A {category} pattern covering {size}."
    elif verdict == AUTHORISED_ACTIVITY:
        title = f"Notice: {category} inside a registered zone at {area_name}"
        lead = f"Change consistent with {category} covering {size}, inside a registered zone."
    else:
        title = f"Change needing review at {area_name}"
        lead = f"Material change covering {size} with no determined cause."
    return (
        title[:180],
        f"{lead} Satellite evidence supports a review and does not establish legality; "
        "field verification is required.",
    )


def _alert_mask(prior: Alert, grid: AOIGrid) -> np.ndarray | None:
    """The union of every region an open alert covers, on this AOI's grid."""

    combined: np.ndarray | None = None
    findings = list(prior.regions) or ([prior.finding] if prior.finding is not None else [])
    for finding in findings:
        if not finding.geometry_json:
            continue
        try:
            mask = rasterize_geometry(json.loads(finding.geometry_json), grid)
        except Exception as error:  # A malformed legacy geometry cannot match anything.
            logger.info("Skipping overlap test against alert %s: %s", prior.id, error)
            continue
        combined = mask if combined is None else combined | mask
    return combined


def _overlaps(prior: Alert, region: np.ndarray, grid: AOIGrid) -> bool:
    prior_mask = _alert_mask(prior, grid)
    if prior_mask is None:
        return False
    smaller = min(int(prior_mask.sum()), int(region.sum()))
    return smaller > 0 and int((prior_mask & region).sum()) / smaller >= DUPLICATE_OVERLAP


def _note(finding: Finding, sentence: str) -> None:
    reasons: list[str] = json.loads(finding.verdict_reasons_json or "[]")
    reasons.append(sentence)
    finding.verdict_reasons_json = json.dumps(reasons)


def _weak_cause(finding: Finding) -> bool:
    """Regrowth or a change within one surface type: evidence, never an alert on its own."""

    evidence = json.loads(finding.evidence_json or "{}")
    return evidence.get("rule_strength", 1.0) <= WEAK_CAUSE_STRENGTH


def _alertable_findings(
    case: Case, area: MonitoringArea, confidence_threshold: float
) -> list[Finding]:
    """The findings worth an alert. The rest stay as evidence, each saying why.

    The evidence bar applies per category and verdict: one strong region makes the kind of
    change credible, and the alert then reports all of it, not only the strongest piece.
    """

    selected = set(json.loads(area.categories_json or "[]"))
    strongest: dict[tuple[str | None, str | None], float] = {}
    candidates = [
        finding
        for finding in case.findings
        if finding.verdict != SEASONAL_BENIGN and not _weak_cause(finding)
    ]
    for finding in candidates:
        key = (finding.category, finding.verdict)
        strongest[key] = max(strongest.get(key, 0.0), finding.confidence)
    eligible: list[Finding] = []
    for finding in candidates:
        if strongest[(finding.category, finding.verdict)] < confidence_threshold:
            _note(
                finding,
                f"Evidence score {finding.confidence:.2f} is below the alert threshold "
                f"{confidence_threshold:.2f}, so this is retained without an alert.",
            )
            continue
        # Natural hazards always alert. The AOI's list narrows only human-caused categories.
        if selected and finding.category not in selected and finding.category not in NATURAL_CAUSED:
            _note(
                finding,
                f"{finding.category} is not among the categories monitored on this AOI, so no alert was raised.",
            )
            continue
        eligible.append(finding)

    def tiny(finding: Finding) -> bool:
        return finding.category == "OTHER_CHANGE" and finding.area_m2 < TINY_OTHER_CHANGE_M2

    if any(not tiny(finding) for finding in eligible):
        for finding in eligible:
            if tiny(finding):
                _note(
                    finding,
                    "Under 1 ha with no specific cause, so it is kept as evidence and the larger "
                    "changes in this scene carry the alert.",
                )
        eligible = [finding for finding in eligible if not tiny(finding)]
    return eligible


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
    """One alert per category and verdict in this case, merging repeats into open alerts.

    Findings stay separate underneath, so a scene with eight similar pits is one alert to
    read, listing eight regions. Does not commit. Returns (new alerts, updated alerts), most
    severe first. A live detection merges only with live alerts, and a replay only with
    alerts from the same replay.
    """

    active = (
        db.query(Alert)
        .options(joinedload(Alert.finding), selectinload(Alert.regions))
        .filter(
            Alert.aoi_id == area.id,
            Alert.status.in_(ACTIVE_ALERT_STATUSES),
            Alert.origin == case.origin,
        )
    )
    if case.origin == ORIGIN_REPLAY:
        active = active.filter(Alert.replay_id == case.replay_id)
    open_alerts = active.all()

    groups: dict[tuple[str | None, str | None], list[Finding]] = {}
    for finding in _alertable_findings(case, area, confidence_threshold):
        groups.setdefault((finding.category, finding.verdict), []).append(finding)

    created: list[Alert] = []
    updated: list[Alert] = []
    for (category, verdict), members in groups.items():
        members.sort(key=lambda item: item.area_m2, reverse=True)
        lead = members[0]
        total_area = sum(item.area_m2 for item in members)
        confidence = max(item.confidence for item in members)
        group_mask = np.zeros(grid.shape, dtype=bool)
        for member in members:
            mask = regions.get(member.id)
            if mask is not None:
                group_mask |= mask
        # A changed verdict is an escalation and needs its own dispatch, so only the same
        # category under the same verdict counts as a repeat.
        duplicate = next(
            (
                prior
                for prior in open_alerts
                if prior.category == category
                and prior.verdict == verdict
                and _overlaps(prior, group_mask, grid)
            ),
            None,
        )
        if duplicate is not None:
            duplicate.persistence_count = (duplicate.persistence_count or 1) + 1
            duplicate.last_seen_at = processed_at
            duplicate.area_m2 = max(duplicate.area_m2 or 0.0, total_area)
            duplicate.confidence = max(duplicate.confidence or 0.0, confidence)
            duplicate.severity = severity_for(duplicate.verdict or "", duplicate.area_m2)
            duplicate.priority_score = priority_score(
                duplicate.verdict, duplicate.confidence, duplicate.area_m2
            )
            for member in members:
                member.persistence_score = round(min(1.0, duplicate.persistence_count / 3.0), 4)
                _note(
                    member,
                    f"This repeats an open alert first raised for {duplicate.acquisition_date}; "
                    f"it has now been seen {duplicate.persistence_count} times, and no new alert was created.",
                )
            record_audit(
                db,
                "alert",
                duplicate.id,
                "alert_persistence_updated",
                {"count": duplicate.persistence_count, "case_id": case.id, "regions": len(members)},
            )
            updated.append(duplicate)
            continue
        title, message = _describe(category, verdict, area.name, total_area, len(members))
        department = department_for(db, category, area.state)
        alert = Alert(
            case_id=case.id,
            finding_id=lead.id,
            title=title,
            message=message,
            severity=severity_for(verdict or "", total_area),
            status="open",
            department_id=department.id,
            priority_score=priority_score(verdict, confidence, total_area),
            category=category,
            verdict=verdict,
            origin=case.origin,
            replay_id=case.replay_id,
            aoi_id=area.id,
            confidence=confidence,
            area_m2=total_area,
            acquisition_date=case.after_date,
            created_at=processed_at,
            detected_at=processed_at,
            scene_processed_at=processed_at,
            last_seen_at=processed_at,
            persistence_count=1,
        )
        db.add(alert)
        db.flush()
        for member in members:
            member.alert_id = alert.id
        record_audit(
            db,
            "alert",
            alert.id,
            "alert_created",
            {
                "category": category,
                "verdict": verdict,
                "department": department.name,
                "regions": len(members),
            },
        )
        created.append(alert)

    def rank(alert: Alert) -> tuple[int, float]:
        return (-SEVERITY_RANK.get(alert.severity, 0), -(alert.priority_score or 0.0))

    created.sort(key=rank)
    updated.sort(key=rank)
    return created, updated


def alert_query(db: Session) -> Any:
    """Alerts with everything the payload reads, loaded without a query per alert."""

    return db.query(Alert).options(
        joinedload(Alert.case).joinedload(Case.area),
        joinedload(Alert.department),
        joinedload(Alert.finding),
        selectinload(Alert.regions).defer(Finding.evidence_json),
    )
