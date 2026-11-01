"""Truthful runtime status for /api/system/status, /api/overview and /api/models."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
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
from app.vocabulary import ACTIVE_ALERT_STATUSES, SEASONAL_BENIGN

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


def models_payload(base_url: str) -> list[dict[str, Any]]:
    """Registry entries plus truthful runtime status and absolute artifact URLs."""

    registry = {str(model.get("key")): model for model in read_registry()}
    output = []
    for key in _MODEL_KEYS:
        model = dict(registry.get(key) or {"key": key, "metrics": {}, "artifacts": {}, "datasets": []})
        if key not in registry:
            model["status"] = "NOT_TRAINED"
        elif not _checkpoint_present(model):
            model["status"] = "CHECKPOINT_MISSING"
        artifacts = model.get("artifacts") or {}
        model["artifacts"] = {
            name: f"{base_url.rstrip('/')}/{str(path).lstrip('/')}" for name, path in artifacts.items()
        }
        output.append(model)
    return output


def _model_runtime() -> dict[str, Any]:
    """Which device the cached model singletons chose. Never triggers a slow first load."""

    from app.ml import inference

    change = getattr(inference, "_change_detector", None)
    landcover = getattr(inference, "_landcover_classifier", None)
    return {
        "change_available": bool(getattr(change, "available", False)),
        "landcover_available": bool(getattr(landcover, "available", False)),
        "device": getattr(change, "device", None) or getattr(landcover, "device", None) or "cpu",
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
        "open_alerts": db.query(func.count(Alert.id))
        .filter(Alert.status.in_(ACTIVE_ALERT_STATUSES))
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
        "alerts_24h": db.query(func.count(Alert.id))
        .filter(Alert.created_at >= now - timedelta(hours=24))
        .scalar()
        or 0,
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
            {"at": run.completed_at, "type": "run", "text": f"{name}: {run.message}", "area_id": run.aoi_id}
        )
    alerts = db.query(Alert).order_by(Alert.created_at.desc()).limit(_ACTIVITY_LIMIT).all()
    for alert in alerts:
        items.append({"at": alert.created_at, "type": "alert", "text": alert.title, "area_id": alert.aoi_id})
    dispatches = (
        db.query(NotificationAttempt, Alert.aoi_id)
        .join(Alert, NotificationAttempt.alert_id == Alert.id)
        .filter(NotificationAttempt.channel != "IN_APP")
        .order_by(NotificationAttempt.created_at.desc())
        .limit(_ACTIVITY_LIMIT)
        .all()
    )
    for attempt, area_id in dispatches:
        items.append(
            {
                "at": attempt.created_at,
                "type": "dispatch",
                "text": f"{attempt.channel} to {attempt.destination}: {attempt.status}",
                "area_id": area_id,
            }
        )
    items.sort(key=lambda item: item["at"], reverse=True)
    return [{**item, "at": iso_z(item["at"])} for item in items[:_ACTIVITY_LIMIT]]
