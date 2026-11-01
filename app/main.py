"""FastAPI application: startup, shutdown, evidence files, SSE and the web dashboard."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.api.monitoring import router as monitoring_router
from app.api.operations import router as operations_router
from app.auth import LoginRequiredMiddleware
from app.auth import router as auth_router
from app.config import APP_VERSION, settings
from app.logging_setup import configure_logging
from app.migrations import migrate_schema
from app.seed_sites import apply_seed_files
from app.services.events import bus, install_shutdown_hook, stream_events
from app.services.scheduler import scheduler
from app.services.storage import resolve_public_path
from app.snapshot import restore_snapshot_if_empty

logger = logging.getLogger(__name__)
_APP_DIR = Path(__file__).resolve().parent
_started_monotonic = time.monotonic()
# Evidence files are written once under a unique run folder, so they can cache for a long time.
_EVIDENCE_CACHE = "public, max-age=2592000, immutable"


def _preload_models() -> None:
    """Load models once, off the startup path, so status is honest and first runs are quick."""

    try:
        from app.ml.inference import get_change_detector, get_landcover_classifier

        get_change_detector()
        get_landcover_classifier()
    except Exception:  # The pipeline degrades to spectral rules and reports that it did.
        logger.exception("Model preload failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.ensure_directories()
    configure_logging()
    bus.reopen()
    install_shutdown_hook()
    migrate_schema()
    restore_snapshot_if_empty()
    apply_seed_files()
    if settings.preload_models:
        threading.Thread(target=_preload_models, name="model-preload", daemon=True).start()
    scheduler.start()
    logger.info("%s %s ready.", settings.app_name, APP_VERSION)
    try:
        yield
    finally:
        bus.close()
        scheduler.stop()


app = FastAPI(
    title="SentinelWatch API",
    version=APP_VERSION,
    description=(
        "Continuous Sentinel-2 monitoring. Results identify physical change for human "
        "review and are not proof of illegal activity."
    ),
    lifespan=lifespan,
)
app.add_middleware(LoginRequiredMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Last-Event-ID"],
)
app.mount("/static", StaticFiles(directory=_APP_DIR / "static"), name="static")
app.mount("/reports/ml", StaticFiles(directory=settings.reports_ml_dir, check_dir=False), name="reports-ml")
templates = Jinja2Templates(directory=_APP_DIR / "templates")
app.include_router(auth_router)
app.include_router(monitoring_router)
app.include_router(operations_router)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request):
    # Keyword arguments support both current Starlette and older FastAPI template APIs.
    return templates.TemplateResponse(
        request=request, name="index.html", context={"app_name": settings.app_name}
    )


@app.get("/files/{file_path:path}", include_in_schema=False)
def evidence_file(file_path: str):
    # The route is constrained to the served parts of the storage tree; untrusted URL paths
    # cannot escape it even when they contain ../ segments.
    candidate = resolve_public_path(file_path)
    if candidate is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Evidence file not found."
        )
    return FileResponse(candidate, headers={"Cache-Control": _EVIDENCE_CACHE})


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": settings.app_name,
        "version": APP_VERSION,
        "uptime_seconds": round(time.monotonic() - _started_monotonic, 1),
    }


@app.get("/api/events/stream", include_in_schema=False)
async def events_stream(request: Request) -> StreamingResponse:
    last_event_id: int | None = None
    header = request.headers.get("last-event-id")
    if header and header.isdigit():
        last_event_id = int(header)
    return StreamingResponse(
        stream_events(request.is_disconnected, scheduler.counters, last_event_id=last_event_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
