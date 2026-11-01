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
from app.services.events import bus
from app.services.monitoring import execute_monitoring_run
from tests.fakes import (
    BARE,
    BBOX,
    CROP,
    FOG,
    FOREST,
    LEAFLESS,
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
    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: quiet_context())
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
    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: rainy)
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
    _replay_run(area_id, AFTER, replay_id="second", force=True)
    all_findings = sorted((f.category, f.area_pixels, f.verdict) for f in _rows(Finding))
    assert all_findings == sorted(first * 2)
    assert provider.band_reads + provider.scl_reads == reads


def test_scene_is_read_from_cache_when_every_provider_is_down():
    provider = install({AFTER: Scene(FOREST, BARE), BEFORE: Scene(FOREST), REFERENCE: Scene(FOREST)})
    area_id = make_area()
    _replay_run(area_id, AFTER, replay_id="first")
    provider.fail_reads = True
    provider.fail_search = True
    run = _replay_run(area_id, AFTER, replay_id="second", force=True)
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
