"""JSON shapes of the HTTP API (contract section 5), built in one place.

Workers and routes share these builders, so an SSE ``alert`` event is the same object a
client gets from ``GET /api/alerts/{id}``. Every datetime passes through ``iso_z``.
"""

from __future__ import annotations

import json
from typing import Any

from app.models import (
    Alert,
    AuthorisedZone,
    Case,
    Department,
    Finding,
    MonitoringArea,
    MonitoringRun,
    NotificationAttempt,
)
from app.timeutil import iso_z


def load_json(value: str | None, fallback: Any) -> Any:
    try:
        return json.loads(value) if value else fallback
    except (TypeError, json.JSONDecodeError):
        return fallback


def run_payload(run: MonitoringRun | None) -> dict | None:
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
        "started_at": iso_z(run.started_at),
        "completed_at": iso_z(run.completed_at),
        "origin": run.origin or "live",
        "trigger": run.trigger,
        "as_of_date": run.as_of_date,
        "replay_id": run.replay_id,
        "provider": run.provider,
    }


def area_payload(
    area: MonitoringArea,
    *,
    latest_run: MonitoringRun | None = None,
    open_alerts: int = 0,
    last_scene: dict | None = None,
    newest_scene_status: str | None = None,
) -> dict:
    return {
        "id": area.id,
        "name": area.name,
        "geometry": load_json(area.geometry_json, {}),
        "bbox": load_json(area.bbox_json, []),
        "center_lat": area.center_lat,
        "center_lon": area.center_lon,
        "categories": load_json(area.categories_json, []),
        "max_cloud_cover": area.max_cloud_cover,
        "min_change_area_m2": area.min_change_area_m2,
        "sensitivity": area.sensitivity,
        "monitoring_interval_minutes": area.monitoring_interval_minutes,
        "status": area.status,
        "created_at": iso_z(area.created_at),
        "updated_at": iso_z(area.updated_at),
        "last_checked_at": iso_z(area.last_checked_at),
        "state": area.state,
        "district": area.district,
        "site_type": area.site_type,
        "description": area.description,
        "sources": load_json(area.sources_json, []),
        "next_check_at": iso_z(area.next_check_at),
        "open_alerts": open_alerts,
        "last_scene": last_scene,
        "health": area_health(area, latest_run, open_alerts, newest_scene_status),
        "latest_run": run_payload(latest_run),
    }


def area_health(
    area: MonitoringArea,
    latest_run: MonitoringRun | None,
    open_alerts: int,
    newest_scene_status: str | None,
) -> str:
    if area.status != "ACTIVE":
        return "PAUSED"
    if latest_run is not None and latest_run.status == "FAILED":
        return "ERROR"
    if open_alerts:
        return "ALERT"
    if newest_scene_status == "CLOUDED":
        return "CLOUDED"
    return "OK"


def department_payload(department: Department | None) -> dict | None:
    if department is None:
        return None
    return {
        "id": department.id,
        "name": department.name,
        "category": department.category,
        "state": department.state,
        "recipient_email": department.recipient_email,
        "active": department.active,
        "created_at": iso_z(department.created_at),
        "updated_at": iso_z(department.updated_at),
    }


def dispatch_payload(attempt: NotificationAttempt) -> dict:
    department = attempt.department
    return {
        "id": attempt.id,
        "alert_id": attempt.alert_id,
        "department_id": attempt.department_id,
        "department": department.name if department else None,
        "channel": attempt.channel,
        "destination": attempt.destination,
        "status": attempt.status,
        "detail": attempt.detail,
        "attempts": attempt.attempts or 1,
        "file": attempt.file_path,
        "triggered_by": attempt.triggered_by or "auto",
        "at": iso_z(attempt.created_at),
        "created_at": iso_z(attempt.created_at),
    }


def alert_payload(alert: Alert) -> dict:
    case = alert.case
    area = case.area if case is not None else None
    department = alert.department
    finding = alert.finding
    return {
        "id": alert.id,
        "case_id": alert.case_id,
        "finding_id": alert.finding_id,
        "title": alert.title,
        "message": alert.message,
        "severity": alert.severity,
        "status": alert.status,
        "status_note": alert.status_note,
        "acknowledgement_note": alert.acknowledgement_note,
        "created_at": iso_z(alert.created_at),
        "acknowledged_at": iso_z(alert.acknowledged_at),
        "closed_at": iso_z(alert.closed_at),
        "department_id": alert.department_id,
        "department": {"id": department.id, "name": department.name} if department else None,
        "category": alert.category,
        "verdict": alert.verdict,
        "origin": alert.origin or "live",
        "area_id": alert.aoi_id,
        "area_name": area.name if area else None,
        "state": area.state if area else None,
        "dispatched_at": iso_z(alert.dispatched_at),
        "detected_at": iso_z(alert.detected_at),
        "acquisition_date": alert.acquisition_date,
        "thumbnail": finding.thumbnail if finding else None,
        "confidence": alert.confidence,
        "area_m2": alert.area_m2,
        "priority_score": alert.priority_score,
        "latency_seconds": alert.latency_seconds,
        "persistence_count": alert.persistence_count or 1,
        "last_seen_at": iso_z(alert.last_seen_at),
        "verdict_reasons": load_json(finding.verdict_reasons_json, []) if finding else [],
    }


def finding_payload(finding: Finding) -> dict:
    return {
        "id": finding.id,
        "case_id": finding.case_id,
        "activity": finding.activity,
        "category": finding.category,
        "confidence": finding.confidence,
        "severity": finding.severity,
        "area_pixels": finding.area_pixels,
        "area_m2": finding.area_m2,
        "centroid_x": finding.centroid_x,
        "centroid_y": finding.centroid_y,
        "centroid_lat": finding.centroid_lat,
        "centroid_lon": finding.centroid_lon,
        "bbox": finding.bbox,
        "ndvi_before": finding.ndvi_before,
        "ndvi_after": finding.ndvi_after,
        "ndvi_delta": finding.ndvi_delta,
        "persistence_score": finding.persistence_score,
        "requires_human_verification": finding.requires_human_verification,
        "verdict": finding.verdict,
        "verdict_reasons": load_json(finding.verdict_reasons_json, []),
        "transition": {"from": finding.transition_from, "to": finding.transition_to},
        "indices": load_json(finding.indices_json, {}),
        "change_probability": finding.change_probability,
        "seasonal_persists": finding.seasonal_persists,
        "geometry": load_json(finding.geometry_json, None),
        "thumbnail": finding.thumbnail,
        "evidence": load_json(finding.evidence_json, {}),
    }


def _case_images(case: Case) -> dict:
    images = load_json(case.images_json, {})
    # Rows predating v3 only have the three original columns.
    images.setdefault("before", case.before_image)
    images.setdefault("after", case.after_image)
    images.setdefault("overlay", case.mask_image)
    for key in ("ndvi_before", "ndvi_after", "ndvi_change", "panel"):
        images.setdefault(key, None)
    return images


def case_summary_payload(case: Case) -> dict:
    """Needs ``case.findings``, ``case.alerts`` and ``case.area`` eagerly loaded for lists."""

    area = case.area
    findings = case.findings
    images = _case_images(case)
    return {
        "id": case.id,
        "title": case.title,
        "location": case.location,
        "before_date": case.before_date,
        "after_date": case.after_date,
        "pixel_resolution_m": case.pixel_resolution_m,
        "source": case.source,
        "status": case.status,
        "analysis_method": case.analysis_method,
        "overall_confidence": case.overall_confidence,
        "changed_area_px": case.changed_area_px,
        "changed_area_m2": case.changed_area_m2,
        "highest_severity": case.highest_severity,
        "before_image": case.before_image,
        "after_image": case.after_image,
        "mask_image": case.mask_image,
        "thumbnail": images.get("thumbnail") or images.get("overlay"),
        "aoi_id": case.aoi_id,
        "analysis_id": case.analysis_id,
        "before_acquisition_id": case.before_acquisition_id,
        "after_acquisition_id": case.after_acquisition_id,
        "data_origin": case.data_origin,
        "origin": case.origin or "live",
        "requires_human_verification": case.requires_human_verification,
        "model_version_label": case.model_version_label,
        "quality_note": case.quality_note,
        "created_at": iso_z(case.created_at),
        "area": (
            {
                "id": area.id,
                "name": area.name,
                "state": area.state,
                "bbox": load_json(area.bbox_json, []),
            }
            if area
            else None
        ),
        "finding_count": len(findings),
        "alert_count": len(case.alerts),
        "categories": sorted({f.category for f in findings if f.category}),
        "verdicts": sorted({f.verdict for f in findings if f.verdict}),
    }


def case_detail_payload(case: Case) -> dict:
    payload = case_summary_payload(case)
    context = load_json(case.context_json, {})
    for key, default in (
        ("rainfall", {}),
        ("disasters", []),
        ("protected_areas", []),
        ("osm_features", []),
        ("authorised_zones", []),
    ):
        context.setdefault(key, default)
    payload.update(
        {
            "review_note": case.review_note,
            "reviewed_at": iso_z(case.reviewed_at),
            "image_bounds": load_json(case.image_bounds_json, None),
            "images": _case_images(case),
            "seasonal_reference": load_json(case.seasonal_reference_json, None),
            "context": context,
            "findings": [finding_payload(finding) for finding in case.findings],
            "alerts": [alert_payload(alert) for alert in case.alerts],
            "reviews": [
                {
                    "id": review.id,
                    "case_id": review.case_id,
                    "action": review.action,
                    "note": review.note,
                    "reviewer": review.reviewer,
                    "created_at": iso_z(review.created_at),
                }
                for review in case.reviews
            ],
        }
    )
    return payload


def zone_payload(zone: AuthorisedZone) -> dict:
    return {
        "id": zone.id,
        "name": zone.name,
        "zone_type": zone.zone_type,
        "geometry": load_json(zone.geometry_json, None),
        "authority": zone.authority,
        "reference": zone.reference,
        "valid_from": zone.valid_from,
        "valid_to": zone.valid_to,
        "source": zone.source,
        "is_illustrative": zone.is_illustrative,
        "created_at": iso_z(zone.created_at),
    }
