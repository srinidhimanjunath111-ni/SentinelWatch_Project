"""Central settings, deliberately dependency-free for simple local deployment."""

from __future__ import annotations

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # The application still works with environment variables alone.
    load_dotenv = None

if load_dotenv is not None:
    load_dotenv()


class Settings:
    app_name = os.getenv("APP_NAME", "SentinelWatch")
    database_url = os.getenv("DATABASE_URL", "sqlite:///./instance/sentinelwatch.db")
    storage_dir = Path(os.getenv("STORAGE_DIR", "storage"))
    max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "30"))
    pixel_resolution_m = float(os.getenv("PIXEL_RESOLUTION_M", "10"))
    alert_confidence_threshold = float(os.getenv("ALERT_CONFIDENCE_THRESHOLD", "0.60"))
    min_component_pixels = int(os.getenv("MIN_COMPONENT_PIXELS", "80"))
    model_path = Path(os.getenv("MODEL_PATH", "models/checkpoints/siamese_unet_best.pt"))
    model_version_name = os.getenv("MODEL_VERSION_NAME", "siamese-unet-v1")

    # Copernicus Data Space Ecosystem (CDSE). Catalogue discovery is public, while
    # Process API imagery retrieval needs an OAuth client created in the CDSE account.
    copernicus_stac_url = os.getenv(
        "COPERNICUS_STAC_URL", "https://stac.dataspace.copernicus.eu/v1"
    )
    copernicus_process_url = os.getenv(
        "COPERNICUS_PROCESS_URL", "https://sh.dataspace.copernicus.eu/api/v1/process"
    )
    copernicus_token_url = os.getenv(
        "COPERNICUS_TOKEN_URL",
        "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token",
    )
    copernicus_client_id = os.getenv("COPERNICUS_CLIENT_ID", "").strip()
    copernicus_client_secret = os.getenv("COPERNICUS_CLIENT_SECRET", "").strip()
    copernicus_request_timeout_seconds = float(
        os.getenv("COPERNICUS_REQUEST_TIMEOUT_SECONDS", "120")
    )

    monitoring_lookback_days = int(os.getenv("MONITORING_LOOKBACK_DAYS", "180"))
    monitoring_min_pair_gap_days = int(os.getenv("MONITORING_MIN_PAIR_GAP_DAYS", "20"))
    default_monitoring_interval_minutes = int(
        os.getenv("DEFAULT_MONITORING_INTERVAL_MINUTES", "1440")
    )
    max_aoi_request_pixels = int(os.getenv("MAX_AOI_REQUEST_PIXELS", "1024"))
    enable_monitoring_scheduler = os.getenv("ENABLE_MONITORING_SCHEDULER", "false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    scheduler_poll_seconds = int(os.getenv("SCHEDULER_POLL_SECONDS", "60"))
    allow_baseline_when_model_unavailable = os.getenv(
        "ALLOW_BASELINE_WHEN_MODEL_UNAVAILABLE", "true"
    ).lower() in {"1", "true", "yes", "on"}
    demo_mode = os.getenv("DEMO_MODE", "false").lower() in {"1", "true", "yes", "on"}
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    smtp_starttls = os.getenv("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes", "on"}
    cors_origins = tuple(
        value.strip()
        for value in os.getenv("CORS_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000").split(",")
        if value.strip()
    )

    @property
    def upload_dir(self) -> Path:
        return self.storage_dir / "uploads"

    @property
    def output_dir(self) -> Path:
        return self.storage_dir / "outputs"

    @property
    def satellite_cache_dir(self) -> Path:
        return self.storage_dir / "satellite-cache"

    @property
    def has_copernicus_credentials(self) -> bool:
        return bool(self.copernicus_client_id and self.copernicus_client_secret)

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    def ensure_directories(self) -> None:
        Path("instance").mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.satellite_cache_dir.mkdir(parents=True, exist_ok=True)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
