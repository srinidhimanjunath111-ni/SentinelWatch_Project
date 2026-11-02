"""Structured logging: readable on the console, JSON lines in logs/sentinelwatch.log."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

from app.config import settings
from app.timeutil import iso_z, utcnow

_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


class JsonLinesFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "at": iso_z(utcnow()),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _RESERVED})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Idempotent, so reloads and test clients cannot stack duplicate handlers."""

    root = logging.getLogger()
    if any(getattr(handler, "_sentinelwatch", False) for handler in root.handlers):
        return
    root.setLevel(settings.log_level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console._sentinelwatch = True  # type: ignore[attr-defined]
    root.addHandler(console)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        settings.log_dir / "sentinelwatch.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(JsonLinesFormatter())
    file_handler._sentinelwatch = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)
    # GDAL reports every absent overview or sidecar as a warning.
    logging.getLogger("rasterio").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
