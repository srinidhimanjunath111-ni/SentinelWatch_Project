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

APP_VERSION = "3.0.0"


def _flag(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    app_name = os.getenv("APP_NAME", "SentinelWatch")
    database_url = os.getenv("DATABASE_URL", "sqlite:///./instance/sentinelwatch.db")
    storage_dir = Path(os.getenv("STORAGE_DIR", "storage"))
    log_dir = Path(os.getenv("LOG_DIR", "logs"))
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    pixel_resolution_m = float(os.getenv("PIXEL_RESOLUTION_M", "10"))
    alert_confidence_threshold = float(os.getenv("ALERT_CONFIDENCE_THRESHOLD", "0.62"))
    max_findings_per_case = int(os.getenv("MAX_FINDINGS_PER_CASE", "40"))
    change_checkpoint = os.getenv("CHANGE_CHECKPOINT", "").strip()
    landcover_checkpoint = os.getenv("LANDCOVER_CHECKPOINT", "").strip()
    preload_models = _flag("PRELOAD_MODELS", "true")
    model_registry_path = Path(os.getenv("MODEL_REGISTRY_PATH", "models/registry.json"))
    reports_ml_dir = Path(os.getenv("REPORTS_ML_DIR", "reports/ml"))

    # Imagery providers. Earth Search and Planetary Computer need no credentials;
    # Copernicus joins the failover chain only when OAuth credentials are supplied.
    earth_search_url = os.getenv("EARTH_SEARCH_URL", "https://earth-search.aws.element84.com/v1")
    planetary_computer_url = os.getenv(
        "PLANETARY_COMPUTER_URL", "https://planetarycomputer.microsoft.com/api/stac/v1"
    )
    planetary_computer_sas_url = os.getenv(
        "PLANETARY_COMPUTER_SAS_URL", "https://planetarycomputer.microsoft.com/api/sas/v1"
    )
    provider_timeout_seconds = float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "30"))
    provider_retries = int(os.getenv("PROVIDER_RETRIES", "3"))
    provider_backoff_seconds = float(os.getenv("PROVIDER_BACKOFF_SECONDS", "1.5"))
    provider_cooldown_seconds = float(os.getenv("PROVIDER_COOLDOWN_SECONDS", "120"))

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

    # Scene selection.
    monitoring_lookback_days = int(os.getenv("MONITORING_LOOKBACK_DAYS", "180"))
    monitoring_min_pair_gap_days = int(os.getenv("MONITORING_MIN_PAIR_GAP_DAYS", "20"))
    min_aoi_clear_fraction = float(os.getenv("MIN_AOI_CLEAR_FRACTION", "0.60"))
    min_pair_valid_fraction = float(os.getenv("MIN_PAIR_VALID_FRACTION", "0.30"))
    seasonal_reference_window_days = int(os.getenv("SEASONAL_REFERENCE_WINDOW_DAYS", "45"))
    # After downtime, scenes are analysed oldest first up to this many of the newest dates.
    max_catch_up_dates = int(os.getenv("MAX_CATCH_UP_DATES", "12"))
    max_scl_reads_per_run = int(os.getenv("MAX_SCL_READS_PER_RUN", "150"))
    # The longest AOI side in 10 m pixels; 1024 px is about 10 km.
    max_aoi_request_pixels = int(os.getenv("MAX_AOI_REQUEST_PIXELS", "1024"))

    # Scheduler.
    enable_monitoring_scheduler = _flag("ENABLE_MONITORING_SCHEDULER", "true")
    scheduler_poll_seconds = int(os.getenv("SCHEDULER_POLL_SECONDS", "30"))
    scheduler_workers = max(1, int(os.getenv("SCHEDULER_WORKERS", "3")))
    default_monitoring_interval_minutes = int(os.getenv("DEFAULT_MONITORING_INTERVAL_MINUTES", "1440"))
    scheduler_max_backoff_seconds = int(os.getenv("SCHEDULER_MAX_BACKOFF_SECONDS", "1800"))

    # External context (rainfall, disaster feeds, OpenStreetMap). Failures never fail a run.
    context_timeout_seconds = float(os.getenv("CONTEXT_TIMEOUT_SECONDS", "12"))
    context_budget_seconds = float(os.getenv("CONTEXT_BUDGET_SECONDS", "8"))
    context_user_agent = os.getenv(
        "CONTEXT_USER_AGENT", "SentinelWatch/3.0 (final-year engineering project; contact via repository)"
    )
    heavy_rain_mm_14d = float(os.getenv("HEAVY_RAIN_MM_14D", "100"))
    extreme_rain_mm_14d = float(os.getenv("EXTREME_RAIN_MM_14D", "200"))

    # Dispatch.
    smtp_host = os.getenv("SMTP_HOST", "").strip()
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_username = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    smtp_from = os.getenv("SMTP_FROM", "").strip()
    smtp_starttls = _flag("SMTP_STARTTLS", "true")
    # Seeded mailboxes use the reserved .example domain and cannot receive mail, so a live
    # SMTP demo can redirect every message here. The original addressee stays in the body.
    smtp_redirect_to = os.getenv("SMTP_REDIRECT_TO", "").strip()
    dispatch_retries = int(os.getenv("DISPATCH_RETRIES", "3"))
    dispatch_backoff_seconds = float(os.getenv("DISPATCH_BACKOFF_SECONDS", "2"))

    # First-run population.
    seed_sites_on_first_run = _flag("SENTINELWATCH_SEED_SITES", "true")
    restore_snapshot_on_first_run = _flag("SENTINELWATCH_RESTORE_SNAPSHOT", "true")
    snapshot_dir = Path(os.getenv("SENTINELWATCH_SNAPSHOT_DIR", "demo_snapshot"))

    # Optional deployment login, off unless a password is configured.
    admin_password = os.getenv("SENTINELWATCH_ADMIN_PASSWORD", "")
    auth_session_hours = int(os.getenv("SENTINELWATCH_SESSION_HOURS", "12"))

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
    def context_cache_dir(self) -> Path:
        return self.storage_dir / "context-cache"

    @property
    def outbox_dir(self) -> Path:
        return self.storage_dir / "outbox"

    @property
    def has_copernicus_credentials(self) -> bool:
        return bool(self.copernicus_client_id and self.copernicus_client_secret)

    @property
    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def auth_required(self) -> bool:
        return bool(self.admin_password)

    def ensure_directories(self) -> None:
        if self.database_url.startswith("sqlite:///"):
            Path(self.database_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        for folder in (
            self.upload_dir,
            self.output_dir,
            self.satellite_cache_dir,
            self.context_cache_dir,
            self.outbox_dir,
            self.log_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)


settings = Settings()
