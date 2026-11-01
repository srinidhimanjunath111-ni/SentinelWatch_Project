"""Truthful runtime/model status surfaced to the dashboard and API."""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.ml.inference import SiameseInference
from app.models import ModelVersion


def _rasterio_available() -> bool:
    try:
        import rasterio  # noqa: F401
    except ImportError:
        return False
    return True


def current_model_state() -> tuple[str, str, str]:
    """Return status, device, and explanatory message without pretending readiness."""

    if not settings.model_path.exists():
        return (
            "NOT_TRAINED",
            "cpu",
            "No validated Siamese U-Net checkpoint is installed; operational runs use the labelled baseline only.",
        )
    predictor = SiameseInference(settings.model_path)
    if not predictor.available:
        return (
            "CHECKPOINT_UNAVAILABLE",
            "cpu",
            "A checkpoint file exists but cannot be loaded in this environment. Verify PyTorch and model compatibility.",
        )
    return ("READY", predictor.device, "Validated checkpoint loaded for Siamese U-Net inference.")


def ensure_model_version(db: Session) -> ModelVersion:
    """Persist actual installed model state; no synthetic metrics are added."""

    state, _, message = current_model_state()
    version = db.query(ModelVersion).filter(ModelVersion.name == settings.model_version_name).first()
    if version is None:
        version = ModelVersion(
            name=settings.model_version_name,
            architecture="Siamese U-Net shared encoder",
            checkpoint_path=str(settings.model_path),
            status=state,
            metrics_json="{}",
            notes=message,
        )
        db.add(version)
        db.commit()
        db.refresh(version)
        return version
    if version.status != state or version.checkpoint_path != str(settings.model_path):
        version.status = state
        version.checkpoint_path = str(settings.model_path)
        version.notes = message
        db.commit()
        db.refresh(version)
    return version


def model_version_payload(version: ModelVersion) -> dict:
    try:
        metrics = json.loads(version.metrics_json or "{}")
    except json.JSONDecodeError:
        metrics = {}
    return {
        "id": version.id,
        "name": version.name,
        "architecture": version.architecture,
        "checkpoint_path": version.checkpoint_path,
        "status": version.status,
        "training_dataset": version.training_dataset,
        "metrics": metrics,
        "notes": version.notes,
        "created_at": version.created_at,
        "updated_at": version.updated_at,
    }


def system_status_payload(db: Session) -> dict:
    version = ensure_model_version(db)
    model_state, device, model_message = current_model_state()
    rasterio_ready = _rasterio_available()
    prerequisites: list[str] = []
    if not settings.has_copernicus_credentials:
        prerequisites.append("Copernicus OAuth credentials are not configured for imagery retrieval.")
    if not rasterio_ready:
        prerequisites.append("Rasterio is not installed for GeoTIFF preprocessing.")
    if model_state != "READY":
        prerequisites.append(model_message)
    message = " ".join(prerequisites) or "Operational prerequisites are available."
    return {
        "service": settings.app_name,
        "database": "connected",
        "provider": "Copernicus Data Space Ecosystem",
        "catalogue_endpoint": settings.copernicus_stac_url,
        "imagery_credentials_configured": settings.has_copernicus_credentials,
        "scheduler_enabled": settings.enable_monitoring_scheduler,
        "model_status": version.status,
        "model_version": version.name,
        "device": device,
        "rasterio_available": rasterio_ready,
        "demo_mode": settings.demo_mode,
        "message": message,
    }


def checkpoint_exists() -> bool:
    return Path(settings.model_path).is_file()
