"""A whole run from catalogue to dispatched alert, with imagery and context scripted."""

from __future__ import annotations

import email
import json
from email import policy

import numpy as np
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import (
    Alert,
    AnalysisRecord,
    AOIAcquisition,
    AuditLog,
    AuthorisedZone,
    Case,
    Department,
    Finding,
    MonitoringArea,
    MonitoringRun,
    NotificationAttempt,
)
from app.satellite.grid import build_grid
from app.services.alerting import alert_query, create_or_merge_alerts
from app.services.events import bus
from app.services.monitoring import execute_monitoring_run
from app.services.payloads import alert_payload
from app.timeutil import utcnow
from tests.fakes import (
    BARE,
    BBOX,
    CROP,
    FOG,
    FOREST,
    LEAFLESS,
    NO_LATE_CONTEXT,
    WATER,
    FixedLandCover,
    Scene,
    days_ago,
    install,
    make_area,
    queue,
    quiet_context,
)


@pytest.fixture(autouse=True)
def scripted_context(monkeypatch):
    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: (quiet_context(), NO_LATE_CONTEXT))
    monkeypatch.setattr("app.services.monitoring.read_slope_degrees", lambda grid: None)


def _run(area_id: str, **kwargs) -> MonitoringRun:
    run_id = queue(area_id, **kwargs)
    execute_monitoring_run(run_id)
    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        db.expunge(run)
        return run
    finally:
        db.close()


def _rows(model, *criteria):
    db = SessionLocal()
    try:
        rows = db.query(model).filter(*criteria).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


AFTER, BEFORE, REFERENCE = days_ago(10), days_ago(40), days_ago(10 + 365)


def test_leaf_fall_like_last_year_produces_no_alert_but_keeps_evidence():
    install(
        {
            AFTER: Scene(FOREST, LEAFLESS),
            BEFORE: Scene(FOREST, None),
            REFERENCE: Scene(FOREST, LEAFLESS),
        }
    )
    run = _run(make_area())
    assert run.status == "COMPLETED", run.message
    findings = _rows(Finding)
    assert findings and all(f.verdict == "SEASONAL_BENIGN" for f in findings)
    assert all(f.seasonal_persists is False for f in findings)
    assert _rows(Alert) == []
    assert len(_rows(Case)) == 1


def test_real_clearing_with_same_reference_alerts_and_dispatches(tmp_path):
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "COMPLETED", run.message
    alerts = _rows(Alert)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.category == "DEFORESTATION"
    assert alert.verdict == "SUSPECTED_UNAUTHORISED"
    assert alert.status == "dispatched"
    assert alert.latency_seconds is not None and alert.dispatched_at is not None
    assert alert.origin == "live"
    department = _rows(Department)[0]
    assert department.name == "Kerala Forest Department"
    assert department.recipient_email == "forest.kerala@sentinelwatch.example"


def test_analysis_method_reports_what_ran():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    assert _rows(Case)[0].analysis_method == "spectral-baseline | spectral-landcover"


def test_model_probability_is_labelled_when_a_detector_runs(monkeypatch):
    class Detector:
        available, threshold, version = True, 0.5, "v-test"

        def predict(self, before, after, valid):
            return np.ones(valid.shape, dtype=np.float32)

    monkeypatch.setattr("app.services.monitoring.get_change_detector", lambda: Detector())
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    case = _rows(Case)[0]
    assert case.analysis_method.startswith("siamese-unet+spectral")
    assert case.model_version_label == "v-test"


def test_pipeline_accepts_classifier_nodata_with_zero_probabilities(monkeypatch):
    from app.ml.inference import LANDCOVER_NODATA

    class NoDataLandCover:
        available = True

        def predict(self, scene, valid):
            return (
                np.full(valid.shape, LANDCOVER_NODATA, dtype=np.uint8),
                np.zeros((8, *valid.shape), dtype=np.float32),
            )

    monkeypatch.setattr("app.services.monitoring.get_landcover_classifier", NoDataLandCover)
    class SuppressedDetector:
        available, threshold, version = True, 0.47827455747013037, "v2-test"

        def predict(self, before, after, valid):
            return np.zeros(valid.shape, dtype=np.float32)

    monkeypatch.setattr("app.services.monitoring.get_change_detector", SuppressedDetector)
    rainy = quiet_context(rainfall={"available": True, "total_mm": 220.0})
    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: (rainy, NO_LATE_CONTEXT))
    install({AFTER: Scene(WATER), BEFORE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "COMPLETED", run.message
    findings = _rows(Finding)
    assert findings and all(f.transition_from is None and f.transition_to is None for f in findings)
    assert _rows(Case)[0].analysis_method.endswith("landcover-unet")
    assert [(a.category, a.verdict) for a in _rows(Alert)] == [("FLOOD", "NATURAL_EVENT")]


def test_model_failure_degrades_to_labelled_spectral(monkeypatch):
    class Broken:
        available, threshold = True, 0.5

        def predict(self, *_):
            raise RuntimeError("out of memory")

    monkeypatch.setattr("app.services.monitoring.get_change_detector", lambda: Broken())
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "COMPLETED"
    assert _rows(Case)[0].analysis_method.startswith("spectral-baseline (model error)")


def test_missing_reference_is_recorded_as_null():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST)})
    _run(make_area())
    finding = _rows(Finding)[0]
    assert finding.seasonal_persists is None
    assert "No clear same-season scene" in finding.verdict_reasons_json
    assert _rows(Case)[0].seasonal_reference_json is None


def test_flood_with_rain_goes_to_the_disaster_authority(monkeypatch):
    rainy = quiet_context(rainfall={"available": True, "total_mm": 220.0})
    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: (rainy, NO_LATE_CONTEXT))
    install({AFTER: Scene(FOREST, WATER), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    alert = _rows(Alert)[0]
    assert (alert.category, alert.verdict) == ("FLOOD", "NATURAL_EVENT")
    department = _rows(Department)[0]
    assert department.name == "Kerala State Disaster Management Authority"
    assert department.recipient_email.endswith("@sentinelwatch.example")


def test_all_cloud_scene_costs_one_read_and_alerts_nothing():
    provider = install({AFTER: Scene(FOREST, BARE, scl=9)})
    run = _run(make_area())
    assert run.status == "NO_SUITABLE_ACQUISITION"
    assert "0% usable" in run.message
    assert provider.scl_reads == 1
    assert provider.band_reads == 0
    assert _rows(Alert) == [] and _rows(Case) == []
    db = SessionLocal()
    try:
        link = db.query(AOIAcquisition).first()
        assert link.aoi_clear_fraction == 0.0
        assert link.scene_status == "CLOUDED"
    finally:
        db.close()


def test_fog_that_scl_calls_clear_is_rejected_as_baseline_or_scene():
    install({AFTER: Scene(FOG), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "NO_SUITABLE_ACQUISITION"
    assert _rows(Alert) == []


def test_no_change_completes_without_a_case():
    install({AFTER: Scene(FOREST), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "COMPLETED_NO_SIGNIFICANT_CHANGE"
    assert _rows(Case) == [] and len(_rows(AnalysisRecord)) == 1


def test_small_region_under_the_aoi_minimum_is_filtered():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area(min_change_area_m2=500_000.0))
    assert run.status == "COMPLETED_NO_SIGNIFICANT_CHANGE"


def _zone(**overrides) -> None:
    west, south, east, north = BBOX
    values = {
        "name": "Registered lease",
        "zone_type": "FORESTRY",
        "geometry_json": json.dumps(
            {
                "type": "Polygon",
                "coordinates": [[[west, south], [east, south], [east, north], [west, north], [west, south]]],
            }
        ),
        "authority": "Forest Department",
        "reference": "FC-11",
    }
    values.update(overrides)
    db = SessionLocal()
    db.add(AuthorisedZone(**values))
    db.commit()
    db.close()


def test_authorised_zone_downgrades_to_a_notice_with_no_email():
    _zone(valid_from=str(days_ago(400)), valid_to=str(days_ago(-400)))
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    alert = _rows(Alert)[0]
    assert alert.verdict == "AUTHORISED_ACTIVITY" and alert.severity == "low"
    assert {a.channel for a in _rows(NotificationAttempt)} == {"IN_APP"}
    assert "Registered lease" in _rows(Finding)[0].verdict_reasons_json


def test_zone_validity_is_judged_on_the_after_scene_date():
    _zone(valid_from=str(days_ago(400)), valid_to=str(days_ago(20)))
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    assert _rows(Alert)[0].verdict == "SUSPECTED_UNAUTHORISED"


def test_zone_not_yet_valid_on_the_after_scene_date_does_not_authorise():
    _zone(valid_from=str(days_ago(5)))
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    assert _rows(Alert)[0].verdict == "SUSPECTED_UNAUTHORISED"


def test_illustrative_zone_outside_aoi_has_metric_review_buffer_only():
    from app.satellite.geometry import bbox_polygon
    from app.satellite.grid import build_grid
    from app.services.monitoring import _active_zones

    west, south, east, north = BBOX
    _zone(
        is_illustrative=True,
        geometry_json=json.dumps(bbox_polygon([east + 0.001, south, east + 0.003, north])),
        valid_from=str(days_ago(400)), valid_to=str(days_ago(-400)),
    )
    db = SessionLocal()
    try:
        zones, public = _active_zones(db, build_grid(bbox_polygon(BBOX)), AFTER)
        assert len(zones) == len(public) == 1
        assert not zones[0]["mask"].any(), "Nearby is never an authorising overlap"
        assert zones[0]["review_mask"].any()
        assert _active_zones(db, build_grid(bbox_polygon(BBOX)), days_ago(500)) == ([], [])
    finally:
        db.close()


def test_malformed_stored_zone_date_is_skipped_and_the_run_completes():
    _zone(valid_from="2026-99-99")
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "COMPLETED", run.error_detail
    assert _rows(Alert)[0].verdict == "SUSPECTED_UNAUTHORISED"


def _two_detections(first_scene: Scene, second_scene: Scene) -> tuple[dict, str]:
    """Both later scenes compare against one clean baseline, so the same change is seen twice."""

    scenes = {
        days_ago(30): Scene(FOREST),
        days_ago(10): first_scene,
        days_ago(10 + 365): Scene(FOREST),
        days_ago(5 + 365): Scene(FOREST),
    }
    install(scenes)
    area_id = make_area()
    assert _run(area_id).status == "COMPLETED"
    scenes[days_ago(5)] = second_scene
    assert _run(area_id).status == "COMPLETED"
    return scenes, area_id


def test_repeat_detection_updates_persistence_instead_of_a_new_alert():
    _two_detections(Scene(FOREST, BARE), Scene(FOREST, BARE))
    alerts = _rows(Alert)
    assert len(alerts) == 1
    assert alerts[0].persistence_count == 2
    assert len(_rows(Case)) == 2
    assert _rows(AuditLog, AuditLog.event_type == "alert_persistence_updated")
    assert "repeats an open alert" in " ".join(f.verdict_reasons_json for f in _rows(Finding))


def test_escalation_from_authorised_to_suspected_creates_and_dispatches_a_new_alert():
    _zone(valid_from=str(days_ago(400)), valid_to=str(days_ago(7)))
    _two_detections(Scene(FOREST, BARE), Scene(FOREST, BARE))
    alerts = sorted(_rows(Alert), key=lambda item: item.created_at)
    assert [a.verdict for a in alerts] == ["AUTHORISED_ACTIVITY", "SUSPECTED_UNAUTHORISED"]
    assert alerts[1].severity in ("high", "critical")
    assert alerts[1].status == "dispatched"
    assert alerts[0].persistence_count == 1


def test_same_verdict_repeat_raises_severity_when_area_crosses_threshold():
    small = Scene(FOREST, BARE, patch_slices=(slice(40, 60), slice(40, 60)))
    _two_detections(small, Scene(BARE))
    alerts = _rows(Alert)
    assert len(alerts) == 1
    assert alerts[0].persistence_count == 2
    assert alerts[0].severity == "critical"


def test_outbox_eml_is_real_and_honest_about_not_being_sent():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    email_attempt = _rows(NotificationAttempt, NotificationAttempt.channel == "EMAIL")[0]
    assert email_attempt.status == "OUTBOX_SAVED"
    assert "Not sent" in email_attempt.detail
    files = list(settings.outbox_dir.glob("*.eml"))
    assert len(files) == 1
    message = email.message_from_bytes(files[0].read_bytes(), policy=policy.default)
    assert message["To"].endswith("@sentinelwatch.example")
    attachment = next(message.iter_attachments())
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_content().startswith(b"%PDF")


def test_smtp_failure_retries_a_bounded_number_of_times_and_never_crashes_the_run(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.invalid")
    monkeypatch.setattr(settings, "smtp_from", "alerts@sentinelwatch.example")
    calls = []

    def broken(message):
        calls.append(message)
        raise OSError("connection refused")

    monkeypatch.setattr("app.services.dispatch._send_smtp", broken)
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area())
    assert run.status == "COMPLETED"
    assert len(calls) == settings.dispatch_retries
    attempt = _rows(NotificationAttempt, NotificationAttempt.channel == "EMAIL")[0]
    assert attempt.status == "FAILED" and attempt.attempts == settings.dispatch_retries


def test_smtp_success_records_sent(monkeypatch):
    monkeypatch.setattr(settings, "smtp_host", "smtp.invalid")
    monkeypatch.setattr(settings, "smtp_from", "alerts@sentinelwatch.example")
    sent = []
    monkeypatch.setattr("app.services.dispatch._send_smtp", sent.append)
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    assert len(sent) == 1
    assert _rows(NotificationAttempt, NotificationAttempt.channel == "EMAIL")[0].status == "SENT"


def test_events_publish_only_after_their_rows_are_committed():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area_id = make_area()
    seen = []

    def check(event):
        # A different session sees only committed rows, like a client reacting to the event.
        db = SessionLocal()
        try:
            if event.name == "run":
                run = db.get(MonitoringRun, event.data["run_id"])
                seen.append(("run", run is not None and run.status == event.data["status"]))
            elif event.name == "alert":
                alert = db.get(Alert, event.data["id"])
                seen.append(("alert", alert is not None and alert.status == event.data["status"]))
            elif event.name == "dispatch":
                rows = db.query(NotificationAttempt).filter_by(alert_id=event.data["alert_id"]).all()
                seen.append(("dispatch", any(row.channel == event.data["channel"] for row in rows)))
            elif event.name == "scene":
                link = db.query(AOIAcquisition).filter(AOIAcquisition.aoi_clear_fraction.isnot(None)).first()
                seen.append(("scene", link is not None))
        finally:
            db.close()

    remove = bus.add_listener(check)
    try:
        _run(area_id)
    finally:
        remove()
    kinds = {kind for kind, _ in seen}
    assert {"run", "alert", "dispatch", "scene"} <= kinds
    assert all(ok for _, ok in seen), seen


def _replay_run(area_id: str, as_of, replay_id="replay-1", **kwargs):
    return _run(area_id, trigger="replay", origin="archive_replay", as_of=str(as_of), replay_id=replay_id, **kwargs)


def test_replay_marks_origin_and_ignores_scenes_after_as_of():
    as_of = days_ago(100)
    install(
        {
            days_ago(10): Scene(BARE),  # After as_of, so it must never be used.
            as_of: Scene(FOREST, BARE),
            days_ago(130): Scene(FOREST),
            days_ago(100 + 365): Scene(FOREST),
        }
    )
    run = _replay_run(make_area(), as_of)
    assert run.status == "COMPLETED", run.message
    case, alert = _rows(Case)[0], _rows(Alert)[0]
    assert case.origin == "archive_replay" and alert.origin == "archive_replay"
    assert case.after_date == str(as_of)


def test_replay_step_uses_the_previous_step_as_its_baseline():
    first, second = days_ago(300), days_ago(30)
    install(
        {
            first: Scene(FOREST),
            days_ago(330): Scene(FOREST),
            second: Scene(FOREST, BARE),
            days_ago(60): Scene(FOREST),  # A nearer scene that would hide any older change.
            days_ago(30 + 365): Scene(FOREST),
            days_ago(300 + 365): Scene(FOREST),
        }
    )
    area_id = make_area()
    _replay_run(area_id, first)
    _replay_run(area_id, second)
    case = _rows(Case)[0]
    assert case.before_date == str(first) and case.after_date == str(second)


def test_long_pair_takes_its_reference_from_before_the_baseline():
    baseline, after = days_ago(1200), days_ago(30)
    # Last year's scene already shows the new surface; only an older reference can see it is new.
    install(
        {
            baseline: Scene(FOREST),
            days_ago(1230): Scene(FOREST),
            after: Scene(FOREST, BARE),
            days_ago(30 + 365): Scene(FOREST, BARE),
            days_ago(30 + 365 * 4): Scene(FOREST),
            days_ago(1200 + 365): Scene(FOREST),
        }
    )
    area_id = make_area()
    _replay_run(area_id, baseline)
    _replay_run(area_id, after)
    finding = _rows(Finding)[0]
    assert finding.seasonal_persists is True
    assert finding.verdict == "SUSPECTED_UNAUTHORISED"


def test_replay_and_live_keep_separate_records_of_what_was_analysed():
    scenes = {
        AFTER: Scene(FOREST, BARE),
        BEFORE: Scene(FOREST),
        REFERENCE: Scene(FOREST),
        days_ago(40 + 365): Scene(FOREST),
    }
    install(scenes)
    area_id = make_area()
    assert _replay_run(area_id, AFTER).status == "COMPLETED"
    assert _run(area_id).status == "COMPLETED"
    assert {a.origin for a in _rows(Alert)} == {"live", "archive_replay"}


def test_replay_reuses_cache_and_is_deterministic():
    provider = install(
        {AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)}
    )
    area_id = make_area()
    _replay_run(area_id, AFTER, replay_id="first")
    first = sorted((f.category, f.area_pixels, f.verdict) for f in _rows(Finding))
    reads = provider.band_reads + provider.scl_reads
    _replay_run(area_id, AFTER, replay_id="second")
    all_findings = sorted((f.category, f.area_pixels, f.verdict) for f in _rows(Finding))
    assert all_findings == sorted(first * 2)
    assert provider.band_reads + provider.scl_reads == reads


def test_scene_is_read_from_cache_when_every_provider_is_down():
    provider = install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area_id = make_area()
    _replay_run(area_id, AFTER, replay_id="first")
    provider.fail_reads = True
    provider.fail_search = True
    run = _replay_run(area_id, AFTER, replay_id="second")
    assert run.status == "COMPLETED", run.error_detail


def _chronological_setup():
    scenes = {
        days_ago(60): Scene(FOREST),
        days_ago(30): Scene(FOREST),
        days_ago(60 + 365): Scene(FOREST),
        days_ago(30 + 365): Scene(FOREST),
        days_ago(90): Scene(FOREST),
    }
    install(scenes)
    area_id = make_area()
    assert _run(area_id).status == "COMPLETED_NO_SIGNIFICANT_CHANGE"
    return scenes, area_id


def test_scenes_after_downtime_are_analysed_oldest_first_and_none_is_skipped():
    scenes, area_id = _chronological_setup()
    intermediate, newest = days_ago(20), days_ago(10)
    scenes[intermediate] = Scene(FOREST, BARE)
    scenes[newest] = Scene(FOREST, BARE)
    scenes[days_ago(20 + 365)] = Scene(FOREST)
    scenes[days_ago(10 + 365)] = Scene(FOREST)
    assert execute_monitoring_run(queue(area_id)) is True, "Another scene is still waiting"
    assert execute_monitoring_run(queue(area_id)) is False
    db = SessionLocal()
    try:
        cases = db.query(Case).order_by(Case.after_date.asc()).all()
        assert [case.after_date for case in cases] == [str(intermediate), str(newest)]
    finally:
        db.close()


def test_catch_up_is_bounded_to_the_newest_dates(monkeypatch):
    monkeypatch.setattr(settings, "max_catch_up_dates", 2)
    scenes, area_id = _chronological_setup()
    for age in (25, 20, 15, 10):
        scenes[days_ago(age)] = Scene(FOREST)
    run_id = queue(area_id)
    execute_monitoring_run(run_id)
    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        assert run.status == "COMPLETED_NO_SIGNIFICANT_CHANGE"
        record = db.query(AuditLog).filter(AuditLog.event_type == "catch_up_limit_applied").one()
        assert json.loads(record.payload_json)["dates_skipped"] == 2
    finally:
        db.close()


def test_new_aoi_starts_from_the_newest_scene_without_backfilling():
    install(
        {
            days_ago(10): Scene(FOREST),
            days_ago(20): Scene(FOREST),
            days_ago(40): Scene(FOREST),
            days_ago(70): Scene(FOREST),
        }
    )
    area_id = make_area()
    first = queue(area_id)
    assert execute_monitoring_run(first) is False
    assert len(_rows(AnalysisRecord)) == 1


def test_clear_scene_with_no_baseline_does_not_block_newer_scenes():
    scenes, area_id = _chronological_setup()
    scenes[days_ago(20)] = Scene(FOREST)
    scenes[days_ago(10)] = Scene(FOREST)
    monkey = pytest.MonkeyPatch()
    monkey.setattr("app.services.monitoring._select_baseline", lambda *a, **k: None)
    monkey.setattr("app.services.monitoring._extended_baseline", lambda *a, **k: None)
    try:
        assert execute_monitoring_run(queue(area_id)) is True
    finally:
        monkey.undo()
    assert execute_monitoring_run(queue(area_id)) is False
    statuses = {row.scene_status for row in _rows(AOIAcquisition)}
    assert "NO_BASELINE" in statuses


def test_unexpected_error_marks_the_run_failed_and_does_not_block_the_queue(monkeypatch):
    scenes, area_id = _chronological_setup()
    scenes[days_ago(20)] = Scene(FOREST)
    scenes[days_ago(10)] = Scene(FOREST)

    def boom(*_args, **_kwargs):
        raise ValueError("bad array")

    monkeypatch.setattr("app.services.monitoring.build_change_map", boom)
    run_id = queue(area_id)
    execute_monitoring_run(run_id)
    db = SessionLocal()
    try:
        run = db.get(MonitoringRun, run_id)
        assert run.status == "FAILED" and run.error_code == "UNEXPECTED_ERROR"
        assert db.query(AOIAcquisition).filter_by(scene_status="ANALYSIS_ERROR").count() == 1
    finally:
        db.close()


def test_provider_failure_is_a_recorded_run_failure():
    provider = install({AFTER: Scene(FOREST)})
    provider.fail_search = True
    run = _run(make_area())
    assert run.status == "FAILED" and run.error_code == "PROVIDER_ERROR"
    assert _rows(Case) == []


def test_paused_area_makes_no_provider_request():
    provider = install({AFTER: Scene(FOREST)})
    provider.fail_search = True
    run = _run(make_area(status="PAUSED"))
    assert run.status == "SKIPPED_PAUSED"


def test_landcover_model_output_drives_the_transition(monkeypatch):
    classes = FixedLandCover([0, 7])  # tree before, mine after
    monkeypatch.setattr("app.services.monitoring.get_landcover_classifier", lambda: classes)
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    finding = _rows(Finding)[0]
    assert (finding.transition_from, finding.transition_to) == ("tree", "mine")
    assert finding.category == "MINING"
    alert = _rows(Alert)[0]
    department = _rows(Department)[0]
    assert department.id == alert.department_id and "Mines" in department.name


def test_case_stores_contract_evidence_fields():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    _run(make_area())
    case = _rows(Case)[0]
    images = json.loads(case.images_json)
    assert set(images) >= {"before", "after", "overlay", "ndvi_before", "ndvi_after", "ndvi_change", "panel"}
    (south, west), (north, east) = json.loads(case.image_bounds_json)
    assert BBOX[1] - 0.001 <= south < north <= BBOX[3] + 0.001
    assert BBOX[0] - 0.001 <= west < east <= BBOX[2] + 0.001
    geometry = json.loads(_rows(Finding)[0].geometry_json)
    assert geometry["type"] in ("Polygon", "MultiPolygon")
    assert json.loads(case.seasonal_reference_json)["date"] == str(REFERENCE)


def test_crop_field_verdict_is_benign_without_alert(monkeypatch):
    classes = FixedLandCover([2, 4])
    monkeypatch.setattr("app.services.monitoring.get_landcover_classifier", lambda: classes)
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST, CROP), REFERENCE: Scene(FOREST, CROP)})
    _run(make_area())
    assert _rows(Alert) == []
    assert _rows(Finding)[0].category == "CROP_CYCLE"


def test_area_model_is_left_consistent_after_a_run():
    install({AFTER: Scene(FOREST), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area_id = make_area()
    _run(area_id)
    area = _rows(MonitoringArea, MonitoringArea.id == area_id)[0]
    assert area.last_checked_at is not None
    assert area.next_check_at > area.last_checked_at


# Alert grouping ----------------------------------------------------------------------------


def _pits(*corners, size=25):
    """A scene with several separate bare patches on one forest, as a mine's benches would be."""

    class Pits(Scene):
        def bands(self, shape):
            array = super().bands(shape)
            spectrum = np.array(BARE, dtype=np.float32)[:, None, None]
            for row, column in corners:
                array[:, row : row + size, column : column + size] = spectrum
            return array

    return Pits(FOREST)


def test_similar_regions_group_into_one_alert_with_regions_listed():
    install(
        {
            AFTER: _pits((5, 5), (5, 50), (50, 5), (50, 50)),
            BEFORE: Scene(FOREST),
            REFERENCE: Scene(FOREST),
        }
    )
    _run(make_area())
    findings = _rows(Finding)
    assert len(findings) == 4, "Findings stay separate underneath"
    alerts = _rows(Alert)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.area_m2 == sum(f.area_m2 for f in findings)
    assert "4 regions" in alert.message
    assert {f.alert_id for f in findings} == {alert.id}
    assert alert.finding_id == max(findings, key=lambda f: f.area_m2).id
    assert len(_rows(NotificationAttempt, NotificationAttempt.channel == "EMAIL")) == 1
    db = SessionLocal()
    try:
        payload = alert_payload(alert_query(db).one())
    finally:
        db.close()
    assert payload["region_count"] == 4
    assert {region["finding_id"] for region in payload["regions"]} == {f.id for f in findings}
    assert payload["area_m2"] == alert.area_m2


def test_group_severity_follows_the_total_area():
    # Each pit alone is below the critical area; together they pass it.
    install(
        {
            AFTER: _pits((5, 5), (5, 40), (40, 5), (40, 40), size=15),
            BEFORE: Scene(FOREST),
            REFERENCE: Scene(FOREST),
        }
    )
    _run(make_area())
    alert = _rows(Alert)[0]
    assert all(f.severity == "high" for f in _rows(Finding))
    assert alert.severity == "critical"


def _make_finding(db, case, category, verdict, area_m2, confidence=0.8) -> Finding:
    finding = Finding(
        case_id=case.id,
        activity=category.lower(),
        category=category,
        verdict=verdict,
        confidence=confidence,
        severity="medium",
        area_pixels=int(area_m2 / 100),
        area_m2=area_m2,
        centroid_x=1.0,
        centroid_y=1.0,
        bbox="1,1,2,2",
        evidence_json="{}",
        verdict_reasons_json="[]",
    )
    case.findings.append(finding)
    db.add(finding)
    db.flush()
    return finding


def _group(findings_spec):
    grid = build_grid(json.loads(json.dumps({
        "type": "Polygon",
        "coordinates": [[[BBOX[0], BBOX[1]], [BBOX[2], BBOX[1]], [BBOX[2], BBOX[3]], [BBOX[0], BBOX[3]], [BBOX[0], BBOX[1]]]],
    })))
    area_id = make_area()
    db = SessionLocal()
    try:
        area = db.get(MonitoringArea, area_id)
        case = Case(title="t", before_image="/b", after_image="/a", mask_image="/m", aoi_id=area_id, origin="live", after_date="2026-01-01")
        db.add(case)
        db.flush()
        for category, verdict, area_m2 in findings_spec:
            _make_finding(db, case, category, verdict, area_m2)
        created, updated = create_or_merge_alerts(
            db, case, area, {}, grid, confidence_threshold=0.62, processed_at=utcnow()
        )
        db.commit()
        summary = [(a.category, a.verdict, a.severity, a.area_m2) for a in created]
        reasons = [json.loads(f.verdict_reasons_json) for f in case.findings]
        linked = [f.alert_id for f in case.findings]
        return summary, reasons, linked
    finally:
        db.close()


def test_one_alert_per_category_and_verdict_ranked_by_severity():
    summary, _, _ = _group(
        [
            ("OTHER_CHANGE", "NEEDS_REVIEW", 20_000.0),
            ("MINING", "SUSPECTED_UNAUTHORISED", 30_000.0),
            ("MINING", "SUSPECTED_UNAUTHORISED", 40_000.0),
            ("MINING", "AUTHORISED_ACTIVITY", 40_000.0),
        ]
    )
    assert summary == [
        ("MINING", "SUSPECTED_UNAUTHORISED", "critical", 70_000.0),
        ("OTHER_CHANGE", "NEEDS_REVIEW", "medium", 20_000.0),
        ("MINING", "AUTHORISED_ACTIVITY", "low", 40_000.0),
    ]


def test_tiny_other_change_stays_as_evidence_beside_larger_change():
    summary, reasons, linked = _group(
        [("MINING", "SUSPECTED_UNAUTHORISED", 30_000.0), ("OTHER_CHANGE", "NEEDS_REVIEW", 6_000.0)]
    )
    assert [item[0] for item in summary] == ["MINING"]
    assert linked[1] is None
    assert any("Under 1 ha" in reason for reason in reasons[1])


def test_tiny_other_change_alerts_when_it_is_the_only_thing():
    summary, _, _ = _group([("OTHER_CHANGE", "NEEDS_REVIEW", 6_000.0), ("OTHER_CHANGE", "NEEDS_REVIEW", 7_000.0)])
    assert summary == [("OTHER_CHANGE", "NEEDS_REVIEW", "medium", 13_000.0)]


def test_grouped_alert_still_dedupes_across_consecutive_scenes():
    pits = _pits((5, 5), (5, 50), (50, 5))
    _two_detections(pits, pits)
    alerts = _rows(Alert)
    assert len(alerts) == 1 and alerts[0].persistence_count == 2


# Replay independence -----------------------------------------------------------------------


def test_replay_over_old_dates_runs_after_live_has_moved_on_and_leaves_live_state_alone():
    scenes = {
        days_ago(200): Scene(FOREST),
        days_ago(170): Scene(FOREST, BARE),
        days_ago(170 + 365): Scene(FOREST),
        days_ago(10): Scene(FOREST, BARE),
        days_ago(40): Scene(FOREST, BARE),
        days_ago(10 + 365): Scene(FOREST, BARE),
    }
    install(scenes)
    area_id = make_area()
    assert _run(area_id).status == "COMPLETED_NO_SIGNIFICANT_CHANGE"
    before = _rows(MonitoringArea, MonitoringArea.id == area_id)[0]
    # An old as_of, far behind the live cutoff, still analyses.
    first = _replay_run(area_id, days_ago(170), replay_id="one")
    assert first.status == "COMPLETED", first.message
    after = _rows(MonitoringArea, MonitoringArea.id == area_id)[0]
    assert after.last_checked_at == before.last_checked_at
    assert after.next_check_at == before.next_check_at
    # Live still knows its own cutoff: there is nothing new to analyse.
    assert _run(area_id).status == "NO_NEW_ACQUISITION"


def test_repeating_a_replay_gives_new_results_and_new_alerts_from_the_cache():
    provider = install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area_id = make_area()
    assert _replay_run(area_id, AFTER, replay_id="one").status == "COMPLETED"
    reads = provider.band_reads + provider.scl_reads
    assert _replay_run(area_id, AFTER, replay_id="two").status == "COMPLETED"
    assert provider.band_reads + provider.scl_reads == reads
    assert len(_rows(Case)) == 2
    assert len(_rows(Alert)) == 2, "Dedupe applies only within one replay"
    # Within one replay it still applies.
    assert _replay_run(area_id, AFTER, replay_id="two").status == "NO_NEW_ACQUISITION"


def test_replay_does_not_replace_a_live_result_on_the_timeline():
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area_id = make_area()
    _run(area_id)
    live_case = _rows(Case)[0]
    _replay_run(area_id, AFTER, replay_id="one")
    link = next(row for row in _rows(AOIAcquisition) if row.case_id)
    assert link.case_id == live_case.id


# Contradictory reasons ---------------------------------------------------------------------


def test_crop_cycle_reasons_never_say_it_is_not_seasonal(monkeypatch):
    classes = FixedLandCover([2, 4])
    monkeypatch.setattr("app.services.monitoring.get_landcover_classifier", lambda: classes)
    # Unlike last year, so seasonality persists, but crop-to-bare still reads as a crop cycle.
    install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST, CROP), REFERENCE: Scene(FOREST, CROP)})
    _run(make_area())
    finding = _rows(Finding)[0]
    assert finding.verdict == "SEASONAL_BENIGN"
    assert finding.seasonal_persists is True
    assert "not seasonal" not in finding.verdict_reasons_json
    assert "sowing dates" in finding.verdict_reasons_json


def test_regrowth_never_alerts_and_benign_regions_keep_a_place_in_the_evidence():
    from types import SimpleNamespace

    from app.services.monitoring import _regions_to_store

    material = [(SimpleNamespace(verdict="NEEDS_REVIEW", area_m2=100.0 + n), n) for n in range(50)]
    benign = [(SimpleNamespace(verdict="SEASONAL_BENIGN", area_m2=9000.0 + n), n) for n in range(50)]
    kept = _regions_to_store(material + benign, 40)
    assert len(kept) == 40
    assert sum(pair[0].verdict == "SEASONAL_BENIGN" for pair in kept) == 8
    assert kept[0][0].area_m2 == 149.0


def test_short_pair_browning_keeps_evidence_without_dispatch_even_with_wrong_built_label(monkeypatch):
    classes = FixedLandCover([2, 3])  # A crop-to-built error must not override the spectrum.
    monkeypatch.setattr("app.services.monitoring.get_landcover_classifier", lambda: classes)
    browning = (0.05, 0.07, 0.08, 0.24, 0.20, 0.14)
    install({AFTER: Scene(FOREST, browning), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    run = _run(make_area(site_type="infrastructure"))
    assert run.status == "COMPLETED"
    findings = _rows(Finding)
    assert findings and all(f.verdict == "NEEDS_REVIEW" for f in findings)
    assert all(json.loads(f.evidence_json)["short_pair_browning"] for f in findings)
    assert _rows(Alert) == []
    assert _rows(NotificationAttempt) == []


def test_imagery_outage_keeps_scene_eligible_for_a_successful_retry(monkeypatch):
    from app.satellite.provider import ProviderError

    provider = install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    original = provider.read_bands
    monkeypatch.setattr(provider, "read_bands", lambda *args: (_ for _ in ()).throw(ProviderError("outage")))
    area = make_area()
    failed = _run(area)
    assert failed.status == "FAILED" and failed.error_code == "PROVIDER_ERROR"
    assert "will retry at the next check" in failed.message
    assert _rows(Alert) == [] and _rows(AnalysisRecord) == []
    assert all(link.scene_status != "ANALYSIS_ERROR" for link in _rows(AOIAcquisition))
    monkeypatch.setattr(provider, "read_bands", original)
    retried = _run(area)
    assert retried.status == "COMPLETED", retried.message
    assert _rows(Alert) and _rows(AnalysisRecord)


def test_cached_archive_pair_and_evidence_work_during_a_provider_outage():
    from fastapi.testclient import TestClient

    from app.main import app

    provider = install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area = make_area()
    first = _run(area)
    assert first.status == "COMPLETED"
    provider.fail_search = provider.fail_reads = True
    with TestClient(app) as client:
        case = client.get("/api/cases").json()[0]
        detail = client.get("/api/cases/" + case["id"]).json()
        for url in detail["images"].values():
            assert client.get(url).status_code == 200
        report = client.get("/api/cases/" + case["id"] + "/report")
        assert report.status_code == 200 and report.content.startswith(b"%PDF-")
        response = client.post(f"/api/areas/{area}/replay", json={
            "start_date": BEFORE.isoformat(), "end_date": AFTER.isoformat(), "max_scenes": 2,
        })
        assert response.status_code == 202, response.text
        import time
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            runs = [client.get("/api/monitoring-runs/" + rid).json() for rid in response.json()["run_ids"]]
            if all(run["status"] not in ("QUEUED", "RUNNING") for run in runs):
                break
            time.sleep(0.02)
        assert [run["status"] for run in runs] == ["REPLAY_BASELINE", "COMPLETED"]
        assert all(run["error_code"] is None for run in runs)
