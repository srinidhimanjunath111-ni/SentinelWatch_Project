"""Continuous monitoring: a worker pool, per-AOI serialisation, recovery and backoff.

Runs queue per AOI, so one AOI never has two runs at once and a long replay on one AOI
cannot starve the others. Workers survive any exception. After a crash, runs left QUEUED or
RUNNING are requeued at startup. While every provider is failing, due checks back off
exponentially rather than hammering a service that is down.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import or_

from app.config import settings
from app.database import SessionLocal
from app.models import MonitoringArea, MonitoringRun
from app.services.audit import record_audit
from app.services.monitoring import (
    execute_monitoring_run,
    queue_monitoring_run,
    shutdown_requested,
)
from app.timeutil import iso_z, utcnow

logger = logging.getLogger(__name__)

ACTIVE_RUN_STATES = ("QUEUED", "RUNNING")


class MonitoringScheduler:
    def __init__(self, workers: int | None = None, execute: Callable[[str], bool | None] | None = None):
        self.worker_count = workers or settings.scheduler_workers
        self._execute = execute or execute_monitoring_run
        self._condition = threading.Condition()
        self._pending: dict[str, deque[str]] = {}
        self._ready: deque[str] = deque()
        self._busy: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._tick_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._running = False
        self._queue_lock = threading.Lock()
        self.last_tick_at: datetime | None = None
        self.last_error: str | None = None
        self.provider_failures = 0
        self.backoff_until: datetime | None = None

    # Lifecycle -------------------------------------------------------------------------

    def start(self, *, run_tick: bool | None = None) -> None:
        if self._running:
            return
        self._stop.clear()
        shutdown_requested.clear()
        self._running = True
        for index in range(self.worker_count):
            thread = threading.Thread(
                target=self._worker_loop, name=f"monitor-worker-{index + 1}", daemon=True
            )
            thread.start()
            self._threads.append(thread)
        self.recover_orphans()
        tick_enabled = settings.enable_monitoring_scheduler if run_tick is None else run_tick
        if tick_enabled:
            self._tick_thread = threading.Thread(target=self._tick_loop, name="monitor-tick", daemon=True)
            self._tick_thread.start()
        logger.info("Scheduler started: %d workers, tick %s.", self.worker_count, "on" if tick_enabled else "off")

    def stop(self, timeout: float = 20.0) -> None:
        """Ask workers to stop at the next stage boundary; runs they leave are requeued next start."""

        if not self._running:
            return
        self._stop.set()
        shutdown_requested.set()
        with self._condition:
            self._condition.notify_all()
        deadline = utcnow() + timedelta(seconds=timeout)
        for thread in [*self._threads, *([self._tick_thread] if self._tick_thread else [])]:
            remaining = (deadline - utcnow()).total_seconds()
            thread.join(max(0.0, remaining))
        self._threads = []
        self._tick_thread = None
        self._pending.clear()
        self._ready.clear()
        self._busy.clear()
        self._running = False
        logger.info("Scheduler stopped.")

    # Queueing --------------------------------------------------------------------------

    def submit(self, run_id: str, aoi_id: str) -> None:
        with self._condition:
            self._pending.setdefault(aoi_id, deque()).append(run_id)
            if aoi_id not in self._busy and aoi_id not in self._ready:
                self._ready.append(aoi_id)
            self._condition.notify()

    def queue_check(self, aoi_id: str, trigger: str = "manual") -> MonitoringRun | None:
        """Create and submit a run unless this AOI already has one. Serialised, so API
        requests and the scheduler tick cannot both pass the in-flight test."""

        with self._queue_lock:
            db = SessionLocal()
            try:
                area = db.get(MonitoringArea, aoi_id)
                if area is None:
                    return None
                inflight = (
                    db.query(MonitoringRun.id)
                    .filter(MonitoringRun.aoi_id == aoi_id, MonitoringRun.status.in_(ACTIVE_RUN_STATES))
                    .first()
                )
                if inflight:
                    return None
                run = queue_monitoring_run(db, area, trigger=trigger)
                db.expunge(run)
            finally:
                db.close()
        self.submit(run.id, aoi_id)
        return run

    def recover_orphans(self) -> int:
        """Requeue runs a previous process left unfinished, oldest replay step first."""

        db = SessionLocal()
        recovered: list[tuple[str, str]] = []
        try:
            orphans = (
                db.query(MonitoringRun)
                .filter(MonitoringRun.status.in_(ACTIVE_RUN_STATES))
                .order_by(MonitoringRun.as_of_date.asc(), MonitoringRun.started_at.asc())
                .all()
            )
            live_seen: set[str] = set()
            # The newest live run per AOI survives; older duplicates would only repeat it.
            for run in sorted(orphans, key=lambda item: item.started_at, reverse=True):
                if run.origin != "archive_replay" and run.aoi_id in live_seen:
                    run.status, run.stage = "FAILED", "FAILED"
                    run.progress_percent = 100
                    run.message = "Interrupted by a server restart; a newer run replaced it."
                    run.error_code = "INTERRUPTED"
                    run.completed_at = utcnow()
                    continue
                if run.origin != "archive_replay":
                    live_seen.add(run.aoi_id)
            for run in orphans:
                if run.status == "FAILED":
                    continue
                run.status, run.stage = "QUEUED", "QUEUED"
                run.progress_percent = 0
                run.message = "Requeued after a server restart."
                record_audit(db, "monitoring_run", run.id, "run_requeued_after_restart")
                recovered.append((run.id, run.aoi_id))
            db.commit()
        except Exception:
            logger.exception("Could not recover orphaned runs")
            db.rollback()
        finally:
            db.close()
        for run_id, aoi_id in recovered:
            self.submit(run_id, aoi_id)
        if recovered:
            logger.warning("Requeued %d orphaned run(s).", len(recovered))
        return len(recovered)

    # Workers ---------------------------------------------------------------------------

    def _next_work(self) -> tuple[str, str] | None:
        with self._condition:
            while not self._stop.is_set():
                if self._ready:
                    aoi_id = self._ready.popleft()
                    queue = self._pending.get(aoi_id)
                    if queue:
                        self._busy.add(aoi_id)
                        return aoi_id, queue.popleft()
                    continue
                self._condition.wait(timeout=1.0)
        return None

    def _finish(self, aoi_id: str) -> None:
        with self._condition:
            self._busy.discard(aoi_id)
            queue = self._pending.get(aoi_id)
            if queue:
                self._ready.append(aoi_id)
                self._condition.notify()
            else:
                self._pending.pop(aoi_id, None)

    def _worker_loop(self) -> None:
        while True:
            work = self._next_work()
            if work is None:
                return
            aoi_id, run_id = work
            try:
                more_pending = self._execute(run_id)
                self._record_outcome(run_id)
                if more_pending is True and not self._stop.is_set():
                    # Behind every other AOI already waiting, so catch-up never starves them.
                    self.queue_check(aoi_id, trigger="catch_up")
            except Exception as error:  # The worker must outlive anything a run can do.
                logger.exception("Worker caught an error from run %s", run_id)
                self.last_error = str(error)[:500]
            finally:
                self._finish(aoi_id)

    def _record_outcome(self, run_id: str) -> None:
        """Track provider-wide failure from run outcomes and adjust scheduler backoff."""

        db = SessionLocal()
        try:
            run = db.get(MonitoringRun, run_id)
            if run is None:
                return
            if run.error_code == "PROVIDER_ERROR":
                self.provider_failures += 1
                delay = min(
                    settings.scheduler_max_backoff_seconds,
                    max(30, settings.scheduler_poll_seconds) * 2 ** min(self.provider_failures, 10),
                )
                self.backoff_until = utcnow() + timedelta(seconds=delay)
                self.last_error = run.error_detail
                area = db.get(MonitoringArea, run.aoi_id)
                if area is not None and run.origin == "live":
                    # A due AOI waits out the backoff, or its own interval if that is sooner.
                    wait = min(delay, area.monitoring_interval_minutes * 60)
                    area.next_check_at = utcnow() + timedelta(seconds=wait)
                    db.commit()
            elif run.status not in ("FAILED", *ACTIVE_RUN_STATES):
                self.provider_failures = 0
                self.backoff_until = None
        finally:
            db.close()

    # Tick ------------------------------------------------------------------------------

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception as error:  # Keep ticking through a database blip.
                logger.exception("Scheduler tick failed")
                self.last_error = str(error)[:500]
            self._stop.wait(max(5, settings.scheduler_poll_seconds))

    def tick(self) -> list[str]:
        """Queue every due active AOI, unless all providers are currently backed off."""

        now = utcnow()
        self.last_tick_at = now
        if self.backoff_until is not None and now < self.backoff_until:
            return []
        db = SessionLocal()
        try:
            due = (
                db.query(MonitoringArea.id)
                .filter(
                    MonitoringArea.status == "ACTIVE",
                    or_(MonitoringArea.next_check_at.is_(None), MonitoringArea.next_check_at <= now),
                )
                .all()
            )
        finally:
            db.close()
        queued = []
        for (aoi_id,) in due:
            run = self.queue_check(aoi_id, trigger="scheduler")
            if run is not None:
                queued.append(run.id)
        return queued

    # Status ----------------------------------------------------------------------------

    def status(self) -> dict:
        db = SessionLocal()
        try:
            active = db.query(MonitoringRun).filter(MonitoringRun.status == "RUNNING").count()
            queued = db.query(MonitoringRun).filter(MonitoringRun.status == "QUEUED").count()
            next_due = (
                db.query(MonitoringArea.next_check_at)
                .filter(MonitoringArea.status == "ACTIVE", MonitoringArea.next_check_at.isnot(None))
                .order_by(MonitoringArea.next_check_at.asc())
                .first()
            )
        finally:
            db.close()
        return {
            "enabled": settings.enable_monitoring_scheduler,
            "running": self._running,
            "last_tick_at": iso_z(self.last_tick_at),
            "next_due_at": iso_z(next_due[0]) if next_due else None,
            "active_runs": active,
            "queued_runs": queued,
            "workers": self.worker_count,
            "backing_off_until": iso_z(self.backoff_until),
            "consecutive_provider_failures": self.provider_failures,
            "last_error": self.last_error,
        }

    def counters(self) -> dict:
        status = self.status()
        return {"active_runs": status["active_runs"], "queued_runs": status["queued_runs"]}


scheduler = MonitoringScheduler()
