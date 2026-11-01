"""Truthful runtime status for /api/system/status, /api/overview and /api/models."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app import ml as _ml_package
from app.config import settings
from app.models import (
    Alert,
    AnalysisRecord,
    Case,
    Finding,
    MonitoringArea,
    MonitoringRun,
    NotificationAttempt,
)
from app.satellite.chain import get_chain
from app.services.dispatch import smtp_status
from app.timeutil import iso_z, utcnow
from app.vocabulary import ACTIVE_ALERT_STATUSES, ORIGIN_LIVE, ORIGIN_REPLAY, SEASONAL_BENIGN

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(_ml_package.__file__).resolve().parents[2]
_MODEL_KEYS = ("change", "landcover")
_ACTIVITY_LIMIT = 25


def _rasterio_available() -> bool:
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return False
    return True


def read_registry() -> list[dict[str, Any]]:
    """Models listed in models/registry.json, or [] until the ML work has delivered one."""

    path = settings.model_registry_path
    try:
        return list(json.loads(path.read_text(encoding="utf-8")).get("models") or [])
    except (OSError, ValueError, AttributeError) as error:
        logger.debug("Model registry unavailable: %s", error)
        return []


def _checkpoint_present(model: dict[str, Any]) -> bool:
    checkpoint = model.get("checkpoint")
    return bool(checkpoint) and (_PROJECT_ROOT / str(checkpoint)).is_file()


def _runtime_state(key: str) -> dict[str, Any]:
    """What this process has actually loaded. Reading it never triggers a slow first load."""

    from app.ml import inference

    attribute = "_change_detector" if key == "change" else "_landcover_classifier"
    model = getattr(inference, attribute, None)
    if model is None:
        return {"runtime_status": "NOT_LOADED", "available": False, "device": None, "unavailable_reason": None}
    available = bool(getattr(model, "available", False))
    checkpoint = getattr(model, "checkpoint", None)
    missing = checkpoint is not None and not Path(checkpoint).is_file()
    return {
        "runtime_status": "LOADED" if available else ("NO_CHECKPOINT" if missing else "LOAD_FAILED"),
        "available": available,
        "device": getattr(model, "device", None) if available else None,
        "unavailable_reason": getattr(model, "unavailable_reason", None),
    }


def models_payload(base_url: str) -> list[dict[str, Any]]:
    """Registry facts plus what is really running, kept apart so neither overstates the other.

    ``registry_status`` is what training recorded. ``status`` is the state a client should
    display: it is READY only if the registry says so, the checkpoint exists, and this
    process has not failed to load it.
    """

    registry = {str(model.get("key")): model for model in read_registry()}
    output = []
    for key in _MODEL_KEYS:
        model = dict(registry.get(key) or {"key": key, "metrics": {}, "artifacts": {}, "datasets": []})
        model["registry_status"] = model.get("status")
        runtime = _runtime_state(key)
        model.update(runtime)
        if key not in registry:
            model["status"] = "NOT_TRAINED"
        elif not _checkpoint_present(model):
            model["status"] = "CHECKPOINT_MISSING"
        elif runtime["runtime_status"] == "LOAD_FAILED":
            model["status"] = "LOAD_FAILED"
        artifacts = model.get("artifacts") or {}
        model["artifacts"] = {
            name: f"{base_url.rstrip('/')}/{str(path).lstrip('/')}" for name, path in artifacts.items()
        }
        output.append(model)
    return output


def _model_runtime() -> dict[str, Any]:
    change, landcover = _runtime_state("change"), _runtime_state("landcover")
    return {
        "change_available": change["available"],
        "landcover_available": landcover["available"],
        "change_runtime": change["runtime_status"],
        "landcover_runtime": landcover["runtime_status"],
        "device": change["device"] or landcover["device"] or "cpu",
    }


def system_status_payload(db: Session) -> dict[str, Any]:
    from app.services.scheduler import scheduler

    chain = get_chain()
    models = models_payload("")
    statuses = {model["key"]: model.get("status") for model in models}
    runtime = _model_runtime()
    return {
        "service": settings.app_name,
        "database": "connected",
        "provider": chain.summary()["name"],
        "providers": chain.status(),
        "scheduler": scheduler.status(),
        "model_status": statuses,
        "device": runtime["device"],
        "models_loaded": runtime,
        "smtp": smtp_status(),
        "auth_required": settings.auth_required,
        "rasterio_available": _rasterio_available(),
        "imagery_credentials_configured": settings.has_copernicus_credentials,
        "message": (
            "Sentinel-2 scenes arrive every 2-5 days when the satellites pass and skies are "
            "clear; the schedule controls how often the catalogue is polled."
        ),
    }


def overview_payload(db: Session) -> dict[str, Any]:
    from app.services.scheduler import scheduler

    now = utcnow()
    counts = {
        "areas": db.query(func.count(MonitoringArea.id)).scalar() or 0,
        "active_areas": db.query(func.count(MonitoringArea.id))
        .filter(MonitoringArea.status == "ACTIVE")
        .scalar()
        or 0,
        "critical_alerts": db.query(func.count(Alert.id))
        .filter(Alert.status.in_(ACTIVE_ALERT_STATUSES), Alert.severity == "critical")
        .scalar()
        or 0,
        "cases": db.query(func.count(Case.id)).scalar() or 0,
        "scenes_analysed": db.query(func.count(AnalysisRecord.id))
        .filter(AnalysisRecord.status == "COMPLETED")
        .scalar()
        or 0,
        "changed_area_m2": float(
            db.query(func.coalesce(func.sum(Finding.area_m2), 0.0))
            .filter(Finding.verdict != SEASONAL_BENIGN)
            .scalar()
            or 0.0
        ),
        **_alert_counts_by_origin(db, now),
        # In-app deliveries plus emails an SMTP server accepted. A saved .eml was not sent.
        "dispatches_sent": db.query(func.count(NotificationAttempt.id))
        .filter(NotificationAttempt.status.in_(("RECORDED", "SENT")))
        .scalar()
        or 0,
        "emails_sent": db.query(func.count(NotificationAttempt.id))
        .filter(NotificationAttempt.status == "SENT")
        .scalar()
        or 0,
        "outbox_saved": db.query(func.count(NotificationAttempt.id))
        .filter(NotificationAttempt.status == "OUTBOX_SAVED")
        .scalar()
        or 0,
    }
    models = {
        model["key"]: {
            "status": model.get("status"),
            "runtime_status": model.get("runtime_status"),
            "f1": (model.get("metrics") or {}).get("f1"),
        }
        for model in models_payload("")
    }
    by_category = dict(
        db.query(Finding.category, func.count(Finding.id))
        .filter(Finding.category.isnot(None))
        .group_by(Finding.category)
        .all()
    )
    by_verdict = dict(
        db.query(Finding.verdict, func.count(Finding.id))
        .filter(Finding.verdict.isnot(None))
        .group_by(Finding.verdict)
        .all()
    )
    return {
        "generated_at": iso_z(now),
        "counts": counts,
        "scheduler": scheduler.status(),
        "provider": get_chain().summary(),
        "models": models,
        "by_category": by_category,
        "by_verdict": by_verdict,
        "activity": _activity(db),
    }


def _alert_counts_by_origin(db: Session, now: datetime) -> dict[str, int]:
    """Totals plus a live and replay split, so past imagery never reads as a live incident."""

    open_alerts = dict(
        db.query(Alert.origin, func.count(Alert.id))
        .filter(Alert.status.in_(ACTIVE_ALERT_STATUSES))
        .group_by(Alert.origin)
        .all()
    )
    recent = dict(
        db.query(Alert.origin, func.count(Alert.id))
        .filter(Alert.created_at >= now - timedelta(hours=24))
        .group_by(Alert.origin)
        .all()
    )
    return {
        "open_alerts": sum(open_alerts.values()),
        "open_alerts_live": open_alerts.get(ORIGIN_LIVE, 0),
        "open_alerts_replay": open_alerts.get(ORIGIN_REPLAY, 0),
        "alerts_24h": sum(recent.values()),
        "alerts_24h_live": recent.get(ORIGIN_LIVE, 0),
        "alerts_24h_replay": recent.get(ORIGIN_REPLAY, 0),
    }


def _activity(db: Session) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    runs = (
        db.query(MonitoringRun, MonitoringArea.name)
        .join(MonitoringArea, MonitoringRun.aoi_id == MonitoringArea.id)
        .filter(MonitoringRun.completed_at.isnot(None))
        .order_by(MonitoringRun.completed_at.desc())
        .limit(_ACTIVITY_LIMIT)
        .all()
    )
    for run, name in runs:
        items.append(
            {
                "at": run.completed_at,
                "type": "run",
                "text": f"{name}: {run.message}",
                "area_id": run.aoi_id,
                "origin": run.origin,
            }
        )
    alerts = db.query(Alert).order_by(Alert.created_at.desc()).limit(_ACTIVITY_LIMIT).all()
    for alert in alerts:
        items.append(
            {
                "at": alert.created_at,
                "type": "alert",
                "text": alert.title,
                "area_id": alert.aoi_id,
                "origin": alert.origin,
            }
        )
    dispatches = (
        db.query(NotificationAttempt, Alert.aoi_id, Alert.origin)
        .join(Alert, NotificationAttempt.alert_id == Alert.id)
        .filter(NotificationAttempt.channel != "IN_APP")
        .order_by(NotificationAttempt.created_at.desc())
        .limit(_ACTIVITY_LIMIT)
        .all()
    )
    for attempt, area_id, origin in dispatches:
        items.append(
            {
                "at": attempt.created_at,
                "type": "dispatch",
                "text": f"{attempt.channel} to {attempt.destination}: {attempt.status}",
                "area_id": area_id,
                "origin": origin,
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return [{**item, "at": iso_z(item["at"])} for item in items[:_ACTIVITY_LIMIT]]
