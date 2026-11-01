"""The HTTP contract: shapes, lifecycle, timestamps, file safety, status truthfulness and login."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import AuditLog, MonitoringRun
from app.services.events import CLIENT_QUEUE_SIZE, bus, stream_events
from app.services.monitoring import execute_monitoring_run
from tests.fakes import BARE, FOREST, Scene, days_ago, install, make_area, queue, quiet_context

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
AOI_BODY = {
    "name": "Contract AOI",
    "geometry": {
        "type": "Polygon",
        "coordinates": [[[77.60, 28.16], [77.61, 28.16], [77.61, 28.17], [77.60, 28.17], [77.60, 28.16]]],
    },
    "categories": ["DEFORESTATION", "CONSTRUCTION"],
    "state": "Kerala",
    "district": "Wayanad",
    "site_type": "forest",
    "description": "A test site",
}


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def analysed_case(monkeypatch):
    """A real pipeline result: one case, one alert, dispatched to the outbox."""

    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: quiet_context())
    monkeypatch.setattr("app.services.monitoring.read_slope_degrees", lambda grid: None)
    install(
        {
            days_ago(10): Scene(FOREST, BARE),
            days_ago(40): Scene(FOREST),
            days_ago(10 + 365): Scene(FOREST),
        }
    )
    area_id = make_area(name="Analysed AOI")
    execute_monitoring_run(queue(area_id))
    return area_id


def _datetime_strings(value, key=""):
    if isinstance(value, dict):
        for child_key, child in value.items():
            yield from _datetime_strings(child, child_key)
    elif isinstance(value, list):
        for child in value:
            yield from _datetime_strings(child, key)
    elif isinstance(value, str) and re.match(r"^\d{4}-\d{2}-\d{2}T\d", value):
        yield key, value


def test_health_shape(client):
    assert set(client.get("/api/health").json()) >= {"status", "service", "version", "uptime_seconds"}


def test_overview_shape_matches_the_contract(client, analysed_case):
    body = client.get("/api/overview").json()
    assert set(body["counts"]) >= {
        "areas", "active_areas", "open_alerts", "critical_alerts", "cases", "scenes_analysed",
        "changed_area_m2", "alerts_24h", "dispatches_sent",
    }
    assert set(body["scheduler"]) >= {
        "enabled", "last_tick_at", "next_due_at", "active_runs", "queued_runs", "workers",
    }
    assert set(body["provider"]) >= {"name", "healthy", "last_scene_at", "last_error"}
    assert set(body["models"]) == {"change", "landcover"}
    assert body["counts"]["open_alerts"] == 1
    assert body["by_category"]["DEFORESTATION"] == 1
    assert body["by_verdict"]["SUSPECTED_UNAUTHORISED"] == 1
    assert {item["type"] for item in body["activity"]} >= {"run", "alert", "dispatch"}


def test_every_api_timestamp_is_utc_with_z(client, analysed_case):
    case = client.get("/api/cases").json()[0]
    alert = client.get("/api/alerts").json()[0]
    department = client.get("/api/departments").json()[0]
    urls = [
        "/api/overview", "/api/areas", f"/api/areas/{analysed_case}", f"/api/areas/{analysed_case}/timeline",
        f"/api/areas/{analysed_case}/acquisitions", f"/api/areas/{analysed_case}/analyses", "/api/alerts",
        f"/api/alerts/{alert['id']}", "/api/cases", f"/api/cases/{case['id']}", "/api/departments",
        f"/api/departments/{department['id']}/inbox", "/api/dispatches", "/api/system/status", "/api/data-status",
    ]
    checked = 0
    for url in urls:
        response = client.get(url)
        assert response.status_code == 200, url
        for key, value in _datetime_strings(response.json()):
            checked += 1
            assert ISO_Z.match(value), f"{url} {key}={value}"
    assert checked > 20


def test_area_crud_with_contract_fields(client):
    created = client.post("/api/areas", json=AOI_BODY)
    assert created.status_code == 201
    area = created.json()
    assert area["state"] == "Kerala" and area["health"] == "OK" and area["open_alerts"] == 0
    assert area["next_check_at"].endswith("Z") and area["last_scene"] is None
    assert client.patch(f"/api/areas/{area['id']}", json={"district": "Idukki"}).json()["district"] == "Idukki"
    assert client.post(f"/api/areas/{area['id']}/pause").json()["health"] == "PAUSED"
    assert client.post(f"/api/areas/{area['id']}/check-now").status_code == 409
    assert client.post(f"/api/areas/{area['id']}/resume").json()["status"] == "ACTIVE"
    assert client.delete(f"/api/areas/{area['id']}").status_code == 204
    assert client.get(f"/api/areas/{area['id']}").status_code == 404


def test_oversized_aoi_is_rejected_on_creation_with_a_clear_message(client):
    body = dict(AOI_BODY, geometry={
        "type": "Polygon",
        "coordinates": [[[77.0, 28.0], [77.5, 28.0], [77.5, 28.5], [77.0, 28.5], [77.0, 28.0]]],
    })
    response = client.post("/api/areas", json=body)
    assert response.status_code == 422
    assert "maximum" in response.text


def test_legacy_category_names_are_accepted_and_mapped(client):
    body = dict(AOI_BODY, categories=["VEGETATION_LOSS", "mining_land_disturbance"])
    assert client.post("/api/areas", json=body).json()["categories"] == ["DEFORESTATION", "MINING"]


def test_check_now_goes_through_the_pool_and_a_second_is_refused(client, monkeypatch):
    area = client.post("/api/areas", json=AOI_BODY).json()
    from app.services.scheduler import scheduler

    monkeypatch.setattr(scheduler, "submit", lambda run_id, aoi_id: None)
    response = client.post(f"/api/areas/{area['id']}/check-now")
    assert response.status_code == 202
    assert response.json()["status"] == "QUEUED"
    assert client.post(f"/api/areas/{area['id']}/check-now").status_code == 409
    assert client.get(f"/api/monitoring-runs/{response.json()['id']}").json()["trigger"] == "manual"


def test_areas_list_runs_a_constant_number_of_queries(client):
    from app.database import SessionLocal as factory

    for index in range(2):
        make_area(name=f"First {index}")

    def count_for_listing() -> int:
        engine = factory.kw["bind"]
        statements = []

        def record(_conn, _cursor, statement, *_rest):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", record)
        try:
            assert client.get("/api/areas").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", record)
        return len(statements)

    small = count_for_listing()
    for index in range(8):
        make_area(name=f"More {index}")
    assert count_for_listing() == small


def test_alert_payload_has_contract_fields(client, analysed_case):
    alert = client.get("/api/alerts").json()[0]
    assert set(alert) >= {
        "category", "verdict", "origin", "area_id", "area_name", "department", "dispatched_at", "detected_at",
        "acquisition_date", "thumbnail", "confidence", "area_m2", "priority_score", "latency_seconds",
    }
    assert alert["department"]["name"] == "Kerala Forest Department"
    assert alert["area_id"] == analysed_case and alert["area_name"] == "Analysed AOI"
    detail = client.get(f"/api/alerts/{alert['id']}").json()
    assert detail["case"]["id"] == alert["case_id"]
    assert {d["channel"] for d in detail["dispatches"]} == {"IN_APP", "EMAIL"}


def test_alert_filters(client, analysed_case):
    assert len(client.get("/api/alerts", params={"category": "DEFORESTATION"}).json()) == 1
    assert client.get("/api/alerts", params={"category": "FLOOD"}).json() == []
    assert len(client.get("/api/alerts", params={"area_id": analysed_case, "status": "dispatched"}).json()) == 1
    assert client.get("/api/alerts", params={"status": "resolved"}).json() == []
    assert len(client.get("/api/alerts", params={"status": "active"}).json()) == 1
    assert client.get("/api/alerts", params={"severity": "low"}).json() == []
    department = client.get("/api/departments").json()[0]
    assert len(client.get("/api/alerts", params={"department_id": department["id"]}).json()) == 1
    inbox = client.get(f"/api/departments/{department['id']}/inbox").json()
    assert inbox["alerts"][0]["dispatches"]


def test_alert_lifecycle_is_ordered_and_every_transition_is_audited(client, analysed_case):
    alert = client.get("/api/alerts").json()[0]
    url = f"/api/alerts/{alert['id']}/status"
    assert alert["status"] == "dispatched"
    assert client.patch(url, json={"status": "open"}).status_code == 409
    assert client.patch(url, json={"status": "bogus"}).status_code == 422
    for status in ("acknowledged", "field_verification", "false_positive"):
        response = client.patch(url, json={"status": status, "note": f"to {status}", "actor": "ranger"})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == status
    assert client.patch(url, json={"status": "resolved"}).status_code == 409
    db = SessionLocal()
    try:
        moves = [
            json.loads(row.payload_json)
            for row in db.query(AuditLog)
            .filter(AuditLog.entity_id == alert["id"], AuditLog.event_type == "alert_status_changed")
            .order_by(AuditLog.created_at.asc())
        ]
    finally:
        db.close()
    assert [move["to"] for move in moves] == ["acknowledged", "field_verification", "false_positive"]
    assert moves[0]["actor"] == "ranger"


def test_manual_redispatch_adds_records(client, analysed_case):
    alert = client.get("/api/alerts").json()[0]
    body = client.post(f"/api/alerts/{alert['id']}/dispatch", json={}).json()
    assert len(body["dispatches"]) == 4
    assert client.get("/api/dispatches").json()[0]["triggered_by"] == "manual"


def test_case_detail_has_contract_fields_and_servable_images(client, analysed_case):
    summary = client.get("/api/cases", params={"area_id": analysed_case}).json()[0]
    case = client.get(f"/api/cases/{summary['id']}").json()
    assert case["origin"] == "live"
    assert case["area"]["name"] == "Analysed AOI" and len(case["area"]["bbox"]) == 4
    assert len(case["image_bounds"]) == 2
    assert set(case["context"]) >= {"rainfall", "disasters", "protected_areas", "osm_features", "authorised_zones"}
    assert case["seasonal_reference"]["date"]
    for name in ("before", "after", "overlay", "ndvi_before", "ndvi_after", "ndvi_change", "panel"):
        image = client.get(case["images"][name])
        assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg", name
        assert "max-age" in image.headers["cache-control"]
    finding = case["findings"][0]
    assert set(finding) >= {
        "verdict", "verdict_reasons", "transition", "indices", "change_probability", "seasonal_persists", "geometry",
    }
    assert set(finding["indices"]) == {"ndvi_delta", "nbr_delta", "ndwi_delta", "bsi_delta", "ndbi_delta"}
    assert finding["geometry"]["type"] in ("Polygon", "MultiPolygon")


def test_pdf_report_is_valid_for_a_real_case(client, analysed_case):
    case = client.get("/api/cases").json()[0]
    response = client.get(f"/api/cases/{case['id']}/report")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/pdf")
    assert response.content.startswith(b"%PDF")
    assert len(response.content) > 5000


def test_pdf_survives_many_regions_with_long_geometry(client, analysed_case):
    """A long stored geometry once raised LayoutError and turned the report into a 500."""

    db = SessionLocal()
    try:
        from app.models import Finding

        template = db.query(Finding).first()
        for index in range(30):
            db.add(
                Finding(
                    case_id=template.case_id, activity="mining", category="MINING", confidence=0.7,
                    severity="high", area_pixels=100, area_m2=10000.0, centroid_x=1.0, centroid_y=1.0,
                    bbox="1,1,2,2", geometry_json=json.dumps({"type": "Polygon", "coordinates": [[[0, 0]] * 4000]}),
                    evidence_json="{}", verdict="NEEDS_REVIEW",
                    verdict_reasons_json=json.dumps([f"Reason {index} " * 40]),
                )
            )
        db.commit()
    finally:
        db.close()
    case = client.get("/api/cases").json()[0]
    response = client.get(f"/api/cases/{case['id']}/report")
    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")


def test_case_status_and_review_are_kept_and_audited(client, analysed_case):
    case = client.get("/api/cases").json()[0]
    updated = client.patch(
        f"/api/cases/{case['id']}/status", json={"status": "verified", "review_note": "seen", "reviewer": "me"}
    ).json()
    assert updated["status"] == "verified" and updated["reviews"][0]["reviewer"] == "me"
    assert client.post(f"/api/cases/{case['id']}/reviews", json={"action": "escalated"}).status_code == 201


def test_timeline_shape(client, analysed_case):
    entries = client.get(f"/api/areas/{analysed_case}/timeline").json()
    assert set(entries[0]) == {
        "date", "product_id", "cloud_cover", "aoi_clear_fraction", "status", "thumbnail", "case_id",
        "categories", "satellite",
    }
    newest = entries[0]
    assert newest["status"] == "CHANGE" and newest["case_id"] and newest["categories"] == ["DEFORESTATION"]
    assert client.get(newest["thumbnail"]).status_code == 200
    assert [entry["date"] for entry in entries] == sorted({entry["date"] for entry in entries}, reverse=True)


def test_area_health_reports_alert_after_a_run(client, analysed_case):
    area = client.get(f"/api/areas/{analysed_case}").json()
    assert area["health"] == "ALERT" and area["open_alerts"] == 1
    assert area["last_scene"]["aoi_clear_fraction"] == 1.0


def test_replay_endpoint_returns_ids_in_date_order(client, monkeypatch):
    from app.services.scheduler import scheduler

    monkeypatch.setattr(scheduler, "submit", lambda run_id, aoi_id: None)
    start, end = days_ago(200), days_ago(20)
    install({start: Scene(), days_ago(100): Scene(), end: Scene()})
    area = client.post("/api/areas", json=AOI_BODY).json()
    response = client.post(
        f"/api/areas/{area['id']}/replay",
        json={"start_date": str(start), "end_date": str(end), "max_scenes": 2},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["dates"] == [str(start), str(end)]
    assert len(body["run_ids"]) == 2 and body["replay_id"]
    db = SessionLocal()
    try:
        runs = [db.get(MonitoringRun, run_id) for run_id in body["run_ids"]]
        assert [run.as_of_date for run in runs] == [str(start), str(end)]
        assert {run.origin for run in runs} == {"archive_replay"}
    finally:
        db.close()


def test_replay_validates_dates(client):
    area = client.post("/api/areas", json=AOI_BODY).json()
    url = f"/api/areas/{area['id']}/replay"
    assert client.post(url, json={"start_date": "2024-99-99", "end_date": "2024-12-31"}).status_code == 422
    assert client.post(url, json={"start_date": "2025-01-01", "end_date": "2024-12-31"}).status_code == 422
    assert client.post(url, json={"start_date": "2024-01-01", "end_date": "2999-12-31"}).status_code == 422


def test_replay_scene_budget_respects_the_area_cloud_field(client, monkeypatch):
    """Delhi's after scene has 77% tile cloud with a clear AOI, which max_cloud_cover=85 keeps."""

    from dataclasses import replace

    from app.satellite.chain import get_chain
    from app.services.scheduler import scheduler

    monkeypatch.setattr(scheduler, "submit", lambda run_id, aoi_id: None)
    start, end = days_ago(35), days_ago(5)
    provider = install({start: Scene(), end: Scene()})
    original = provider._candidate
    provider._candidate = lambda day: replace(original(day), cloud_cover=77.16 if day == end else 1.0)
    assert get_chain() is not None
    strict = client.post("/api/areas", json={**AOI_BODY, "name": "Strict", "max_cloud_cover": 40}).json()
    wide = client.post("/api/areas", json={**AOI_BODY, "name": "Wide", "max_cloud_cover": 85}).json()
    payload = {"start_date": str(start), "end_date": str(end), "max_scenes": 2}
    assert client.post(f"/api/areas/{strict['id']}/replay", json=payload).json()["dates"] == [str(start)]
    assert client.post(f"/api/areas/{wide['id']}/replay", json=payload).json()["dates"] == [str(start), str(end)]


# Departments and zones ----------------------------------------------------------------


def test_seeded_departments_only_use_reserved_mailboxes(client, analysed_case):
    for department in client.get("/api/departments").json():
        assert department["recipient_email"].endswith("@sentinelwatch.example")


def test_zone_crud_and_calendar_validation(client):
    body = {"name": "Lease", "zone_type": "mining", "geometry": AOI_BODY["geometry"], "is_illustrative": True}
    created = client.post("/api/authorised-zones", json=body)
    assert created.status_code == 201
    zone = created.json()
    assert set(zone) >= {
        "id", "name", "zone_type", "geometry", "authority", "reference", "valid_from", "valid_to", "source",
        "is_illustrative",
    }
    assert zone["zone_type"] == "MINING" and zone["is_illustrative"] is True
    assert client.post("/api/authorised-zones", json={**body, "valid_from": "2026-99-99"}).status_code == 422
    assert client.post("/api/authorised-zones", json={**body, "valid_to": "2026-02-30"}).status_code == 422
    assert (
        client.post(
            "/api/authorised-zones", json={**body, "valid_from": "2026-02-01", "valid_to": "2026-01-01"}
        ).status_code
        == 422
    )
    assert client.get(f"/api/authorised-zones/{zone['id']}").status_code == 200
    assert client.delete(f"/api/authorised-zones/{zone['id']}").status_code == 204
    assert client.get("/api/authorised-zones").json() == []


# Files --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/files/../../etc/passwd",
        "/files/..%2f..%2fetc%2fpasswd",
        "/files/outputs/../../secret.txt",
        "/files//etc/passwd",
        "/files/satellite-cache/x.tif",
    ],
)
def test_path_traversal_and_unserved_folders_are_refused(client, path):
    secret = settings.storage_dir.parent / "secret.txt"
    secret.write_text("secret")
    cached = settings.satellite_cache_dir / "x.tif"
    cached.write_bytes(b"data")
    response = client.get(path)
    assert response.status_code == 404
    assert b"secret" not in response.content


def test_served_evidence_has_cache_headers(client):
    target = settings.output_dir / "a.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff")
    response = client.get("/files/outputs/a.jpg")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]


def test_legacy_storage_prefixed_urls_still_resolve(client):
    target = settings.output_dir / "old.jpg"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\xff\xd8\xff")
    assert client.get("/files/storage/outputs/old.jpg").status_code == 200


# Status (review finding 7) -----------------------------------------------------------------


def test_status_reports_providers_scheduler_smtp_and_auth(client):
    body = client.get("/api/system/status").json()
    assert set(body) >= {"provider", "providers", "scheduler", "model_status", "device", "smtp", "auth_required"}
    assert body["auth_required"] is False
    assert body["smtp"]["mode"] == "outbox"
    assert "demo_mode" not in body


def test_failed_model_load_is_never_reported_as_ready(client, monkeypatch, tmp_path):
    from app.ml import inference
    from app.services import system

    (tmp_path / "checkpoint.pt").write_bytes(b"x")
    monkeypatch.setattr(system, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        system, "read_registry", lambda: [{"key": "change", "status": "READY", "checkpoint": "checkpoint.pt"}]
    )
    monkeypatch.setattr(inference, "_change_detector", None)
    model = client.get("/api/models").json()[0]
    assert (model["status"], model["runtime_status"]) == ("READY", "NOT_LOADED")
    monkeypatch.setattr(
        inference,
        "_change_detector",
        SimpleNamespace(
            available=False, device="cpu", unavailable_reason="bad file", checkpoint=tmp_path / "checkpoint.pt"
        ),
    )
    model = client.get("/api/models").json()[0]
    assert model["status"] == "LOAD_FAILED"
    assert model["registry_status"] == "READY"
    assert model["unavailable_reason"] == "bad file"
    assert client.get("/api/overview").json()["models"]["change"]["status"] == "LOAD_FAILED"


def test_models_endpoint_gives_absolute_artifact_urls(client, monkeypatch, tmp_path):
    from app.services import system

    monkeypatch.setattr(system, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        system,
        "read_registry",
        lambda: [{"key": "change", "status": "READY", "checkpoint": "c.pt", "artifacts": {"curve": "reports/ml/x.png"}}],
    )
    model = client.get("/api/models").json()[0]
    assert model["artifacts"]["curve"] == "http://testserver/reports/ml/x.png"


# SSE ----------------------------------------------------------------------------------


def _collect(frames_wanted: int, *, publish=None, last_event_id=None, heartbeat_seconds=0.05):
    async def main():
        disconnected = {"value": False}

        async def is_disconnected():
            return disconnected["value"]

        frames = []
        stream = stream_events(
            is_disconnected,
            lambda: {"active_runs": 1, "queued_runs": 2},
            last_event_id=last_event_id,
            heartbeat_seconds=heartbeat_seconds,
        )
        async for frame in stream:
            frames.append(frame)
            if len(frames) == 2 and publish is not None:
                await asyncio.to_thread(publish)
            if len(frames) >= frames_wanted:
                break
        await stream.aclose()
        return frames

    return asyncio.run(main())


def test_stream_sends_retry_heartbeat_and_published_events():
    frames = _collect(3, publish=lambda: bus.publish("run", {"run_id": "r1"}))
    assert frames[0].startswith("retry:")
    assert frames[1].startswith("event: heartbeat")
    assert '"active_runs": 1' in frames[1]
    assert any("event: run" in frame and '"run_id": "r1"' in frame for frame in frames[2:])


def test_stream_repeats_heartbeats_while_idle():
    frames = _collect(4)
    assert sum(frame.startswith("event: heartbeat") for frame in frames) == 3


def test_stream_honours_last_event_id_on_reconnect():
    first = bus.publish("alert", {"id": "older"})
    bus.publish("alert", {"id": "missed"})
    frames = _collect(3, last_event_id=first.id)
    joined = "".join(frames)
    assert '"missed"' in joined and '"older"' not in joined


def test_client_disconnect_releases_the_subscription():
    before = bus.subscriber_count

    async def main():
        calls = {"count": 0}

        async def disconnects_soon():
            calls["count"] += 1
            return calls["count"] > 1

        return [
            frame
            async for frame in stream_events(disconnects_soon, dict, heartbeat_seconds=0.05)
        ]

    frames = asyncio.run(main())
    assert frames and bus.subscriber_count == before


def test_slow_client_is_bounded_and_cannot_block_the_publisher():
    async def main():
        subscriber = bus.subscribe()
        for index in range(CLIENT_QUEUE_SIZE * 3):
            bus.publish("run", {"n": index})
        await asyncio.sleep(0.05)
        size, dropped = subscriber.queue.qsize(), subscriber.dropped
        newest = None
        while not subscriber.queue.empty():
            newest = subscriber.queue.get_nowait()
        bus.unsubscribe(subscriber)
        return size, dropped, newest

    size, dropped, newest = asyncio.run(main())
    assert size == CLIENT_QUEUE_SIZE
    assert dropped >= CLIENT_QUEUE_SIZE
    assert newest.data["n"] == CLIENT_QUEUE_SIZE * 3 - 1


def test_closing_the_bus_ends_open_streams():
    async def main():
        async def never():
            return False

        frames = []
        async for frame in stream_events(never, dict, heartbeat_seconds=5):
            frames.append(frame)
            if len(frames) == 2:
                bus.close()
        return frames

    assert len(asyncio.run(main())) == 2
    bus.reopen()


def test_a_broken_listener_does_not_disturb_the_publisher():
    def broken(_event):
        raise RuntimeError("listener failure")

    remove = bus.add_listener(broken)
    try:
        assert bus.publish("run", {}).id > 0
    finally:
        remove()


# Auth ---------------------------------------------------------------------------------


def test_open_when_no_password_is_set(client):
    assert client.post("/api/areas", json=AOI_BODY).status_code == 201
    assert client.get("/api/auth/me").json() == {"authenticated": True, "auth_required": False}


def test_password_protects_mutations_but_not_reads(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_password", "correct horse")
    assert client.get("/api/system/status").json()["auth_required"] is True
    assert client.get("/api/areas").status_code == 200
    assert client.post("/api/areas", json=AOI_BODY).status_code == 401
    assert client.get("/api/auth/me").json()["authenticated"] is False
    assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
    login = client.post("/api/auth/login", json={"password": "correct horse"})
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/auth/me").json()["authenticated"] is True
    assert client.post("/api/areas", json=AOI_BODY).status_code == 201
    assert client.post("/api/auth/logout").status_code == 200
    client.cookies.clear()
    assert client.post("/api/areas", json=AOI_BODY).status_code == 401


def test_tampered_or_expired_session_tokens_are_rejected(monkeypatch):
    from app import auth

    monkeypatch.setattr(settings, "admin_password", "pw")
    token = auth.issue_token(now=1000.0)
    assert auth.token_valid(token, now=1001.0)
    assert not auth.token_valid(token, now=1000.0 + settings.auth_session_hours * 3600 + 1)
    assert not auth.token_valid(token[:-1] + ("0" if token[-1] != "0" else "1"), now=1001.0)
    assert not auth.token_valid(None)
    monkeypatch.setattr(settings, "admin_password", "changed")
    assert not auth.token_valid(token, now=1001.0)


def test_no_demo_mode_leftovers():
    root = Path(__file__).resolve().parents[1]
    assert not (root / "app" / "demo_seed.py").exists()
    assert not hasattr(settings, "demo_mode")
