"""Recovery, serialisation, backoff and survival of the continuous scheduler."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

from app.config import settings
from app.database import SessionLocal
from app.models import MonitoringArea, MonitoringRun
from app.services.scheduler import MonitoringScheduler
from app.timeutil import utcnow
from tests.fakes import make_area, queue


def _run(run_id):
    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        db.expunge(run)
        return run
    finally:
        db.close()


def _set_status(run_id, **values):
    db = SessionLocal()
    run = db.get(MonitoringRun, run_id)
    for key, value in values.items():
        setattr(run, key, value)
    db.commit()
    db.close()


def _wait_for(predicate, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_startup_requeues_orphaned_queued_and_running_runs():
    executed = []
    first, second = make_area(name="One"), make_area(name="Two")
    running, queued = queue(first), queue(second)
    _set_status(running, status="RUNNING", stage="DETECTING_CHANGE", progress_percent=50)
    scheduler = MonitoringScheduler(workers=2, execute=executed.append)
    scheduler.start(run_tick=False)
    try:
        assert _wait_for(lambda: len(executed) == 2)
    finally:
        scheduler.stop()
    assert set(executed) == {running, queued}
    assert _run(running).status == "QUEUED"
    assert "restart" in _run(running).message


def test_only_the_newest_orphaned_live_run_survives_per_aoi():
    executed = []
    area = make_area()
    older, newer = queue(area), queue(area)
    _set_status(older, started_at=utcnow() - timedelta(hours=1))
    scheduler = MonitoringScheduler(workers=1, execute=executed.append)
    scheduler.start(run_tick=False)
    try:
        assert _wait_for(lambda: executed == [newer])
    finally:
        scheduler.stop()
    assert _run(older).status == "FAILED" and _run(older).error_code == "INTERRUPTED"


def test_orphaned_replay_steps_are_all_kept_in_date_order():
    executed = []
    area = make_area()
    later = queue(area, origin="archive_replay", as_of="2024-06-01", replay_id="r")
    earlier = queue(area, origin="archive_replay", as_of="2024-01-01", replay_id="r")
    scheduler = MonitoringScheduler(workers=2, execute=executed.append)
    scheduler.start(run_tick=False)
    try:
        assert _wait_for(lambda: len(executed) == 2)
    finally:
        scheduler.stop()
    assert executed == [earlier, later]


def test_one_aoi_never_runs_two_at_once_while_others_proceed():
    area_a, area_b = make_area(name="A"), make_area(name="B")
    active: dict[str, int] = {}
    overlaps = []
    lock = threading.Lock()
    owners = {}

    def execute(run_id):
        aoi = owners[run_id]
        with lock:
            active[aoi] = active.get(aoi, 0) + 1
            if active[aoi] > 1:
                overlaps.append(aoi)
        time.sleep(0.03)
        with lock:
            active[aoi] -= 1

    scheduler = MonitoringScheduler(workers=3, execute=execute)
    scheduler.start(run_tick=False)
    try:
        for index in range(4):
            run_a = queue(area_a, origin="archive_replay", as_of=f"2024-01-0{index + 1}", replay_id="a")
            run_b = queue(area_b, origin="archive_replay", as_of=f"2024-01-0{index + 1}", replay_id="b")
            owners[run_a], owners[run_b] = area_a, area_b
            scheduler.submit(run_a, area_a)
            scheduler.submit(run_b, area_b)
        assert _wait_for(lambda: not scheduler._pending and not scheduler._busy)
    finally:
        scheduler.stop()
    assert overlaps == []


def test_check_refuses_a_second_run_while_one_is_in_flight():
    area = make_area()
    gate = threading.Event()
    scheduler = MonitoringScheduler(workers=1, execute=lambda _run_id: gate.wait(3))
    scheduler.start(run_tick=False)
    try:
        assert scheduler.queue_check(area) is not None
        assert scheduler.queue_check(area) is None
    finally:
        gate.set()
        scheduler.stop()


def test_a_worker_survives_an_exception_from_a_run():
    area_a, area_b = make_area(name="A"), make_area(name="B")
    executed = []

    def execute(run_id):
        executed.append(run_id)
        if len(executed) == 1:
            raise RuntimeError("boom")

    scheduler = MonitoringScheduler(workers=1, execute=execute)
    scheduler.start(run_tick=False)
    try:
        scheduler.queue_check(area_a)
        scheduler.queue_check(area_b)
        assert _wait_for(lambda: len(executed) == 2)
    finally:
        scheduler.stop()
    assert scheduler.last_error == "boom"


def test_a_run_reporting_more_scenes_queues_a_follow_up_behind_other_aois():
    area_a, area_b = make_area(name="A"), make_area(name="B")
    order = []
    release = threading.Event()

    def execute(run_id):
        release.wait(3)
        aoi = _run(run_id).aoi_id
        order.append(aoi)
        _set_status(run_id, status="COMPLETED")
        return aoi == area_a and order.count(area_a) == 1

    scheduler = MonitoringScheduler(workers=1, execute=execute)
    scheduler.start(run_tick=False)
    try:
        scheduler.queue_check(area_a, trigger="manual")
        scheduler.queue_check(area_b, trigger="manual")
        release.set()
        assert _wait_for(lambda: len(order) == 3)
    finally:
        scheduler.stop()
    assert order == [area_a, area_b, area_a]


def test_tick_queues_only_due_active_areas():
    due = make_area(name="Due", next_check_at=utcnow() - timedelta(minutes=1))
    make_area(name="Later", next_check_at=utcnow() + timedelta(hours=1))
    make_area(name="Paused", status="PAUSED", next_check_at=utcnow() - timedelta(minutes=1))
    scheduler = MonitoringScheduler(workers=1, execute=lambda _run_id: None)
    queued = scheduler.tick()
    assert len(queued) == 1
    assert _run(queued[0]).aoi_id == due
    assert scheduler.last_tick_at is not None


def test_provider_failure_backs_off_ticks_and_status_reports_it():
    area = make_area(name="Due", next_check_at=utcnow() - timedelta(minutes=1))
    run_id = queue(area)
    _set_status(run_id, status="FAILED", error_code="PROVIDER_ERROR", error_detail="catalogue down")
    scheduler = MonitoringScheduler(workers=1, execute=lambda _run_id: None)
    scheduler._record_outcome(run_id)
    assert scheduler.backoff_until is not None
    assert scheduler.provider_failures == 1
    assert scheduler.tick() == []
    status = scheduler.status()
    assert status["backing_off_until"].endswith("Z")
    assert status["last_error"] == "catalogue down"
    db = SessionLocal()
    area_row = db.get(MonitoringArea, area)
    assert area_row.next_check_at > utcnow()
    db.close()


def test_backoff_is_capped_and_clears_on_success():
    area = make_area()
    failed = queue(area)
    _set_status(failed, status="FAILED", error_code="PROVIDER_ERROR")
    scheduler = MonitoringScheduler(workers=1, execute=lambda _run_id: None)
    for _ in range(30):
        scheduler._record_outcome(failed)
    limit = utcnow() + timedelta(seconds=settings.scheduler_max_backoff_seconds + 5)
    assert scheduler.backoff_until <= limit
    good = queue(area)
    _set_status(good, status="COMPLETED")
    scheduler._record_outcome(good)
    assert scheduler.backoff_until is None and scheduler.provider_failures == 0


def test_graceful_stop_returns_promptly_with_no_work():
    scheduler = MonitoringScheduler(workers=3, execute=lambda _run_id: None)
    scheduler.start(run_tick=False)
    started = time.monotonic()
    scheduler.stop()
    assert time.monotonic() - started < 5
