"""Schema upgrade, site seeding and the data snapshot."""

from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from app.config import settings
from app.database import SessionLocal, make_engine
from app.models import Alert, AuthorisedZone, Case, Finding, MonitoringArea
from app.seed_sites import ZONES_FILE, apply_seed_files, load_sites, seed_sites
from app.services.monitoring import execute_monitoring_run
from app.snapshot import export_snapshot, restore_snapshot, restore_snapshot_if_empty
from tests.fakes import BARE, FOREST, Scene, days_ago, install, make_area, queue, quiet_context

OLD_SCHEMA = """
CREATE TABLE monitoring_areas (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(160) NOT NULL, geometry_json TEXT NOT NULL,
    bbox_json VARCHAR(160) NOT NULL, center_lat FLOAT NOT NULL, center_lon FLOAT NOT NULL,
    categories_json TEXT NOT NULL, max_cloud_cover FLOAT NOT NULL, min_change_area_m2 FLOAT NOT NULL,
    sensitivity VARCHAR(16) NOT NULL, monitoring_interval_minutes INTEGER NOT NULL,
    status VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
    last_checked_at DATETIME
);
CREATE TABLE cases (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(160) NOT NULL, location VARCHAR(255),
    before_date VARCHAR(32), after_date VARCHAR(32), pixel_resolution_m FLOAT NOT NULL,
    source VARCHAR(120), status VARCHAR(32) NOT NULL, analysis_method VARCHAR(64) NOT NULL,
    overall_confidence FLOAT NOT NULL, changed_area_px INTEGER NOT NULL, changed_area_m2 FLOAT NOT NULL,
    highest_severity VARCHAR(20) NOT NULL, before_image VARCHAR(512) NOT NULL,
    after_image VARCHAR(512) NOT NULL, mask_image VARCHAR(512) NOT NULL, created_at DATETIME NOT NULL,
    reviewed_at DATETIME, review_note TEXT, aoi_id VARCHAR(36), data_origin VARCHAR(48)
);
CREATE TABLE findings (
    id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL, activity VARCHAR(40) NOT NULL,
    category VARCHAR(48), confidence FLOAT NOT NULL, severity VARCHAR(20) NOT NULL,
    area_pixels INTEGER NOT NULL, area_m2 FLOAT NOT NULL, centroid_x FLOAT NOT NULL,
    centroid_y FLOAT NOT NULL, bbox VARCHAR(128) NOT NULL, evidence_json TEXT NOT NULL
);
CREATE TABLE alerts (
    id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL, finding_id VARCHAR(36),
    title VARCHAR(180) NOT NULL, message TEXT NOT NULL, severity VARCHAR(20) NOT NULL,
    status VARCHAR(20) NOT NULL, acknowledgement_note TEXT, created_at DATETIME NOT NULL,
    acknowledged_at DATETIME
);
CREATE TABLE case_reviews (
    id VARCHAR(36) PRIMARY KEY, case_id VARCHAR(36) NOT NULL, action VARCHAR(32) NOT NULL,
    note TEXT, reviewer VARCHAR(120), created_at DATETIME NOT NULL
);
CREATE TABLE notification_attempts (
    id VARCHAR(36) PRIMARY KEY, alert_id VARCHAR(36) NOT NULL, department_id VARCHAR(36),
    channel VARCHAR(32) NOT NULL, destination VARCHAR(255), status VARCHAR(32) NOT NULL,
    detail TEXT, created_at DATETIME NOT NULL
);
INSERT INTO cases VALUES ('real', 'Old real case', NULL, '2025-01-01', '2025-02-01', 10, 'Copernicus',
    'new', 'spectral-baseline', 0.7, 10, 1000, 'medium', '/files/storage/outputs/b.jpg',
    '/files/storage/outputs/a.jpg', '/files/storage/outputs/m.jpg', '2025-02-02 00:00:00', NULL, NULL,
    NULL, 'copernicus_operational');
INSERT INTO cases VALUES ('demo', 'Fake', NULL, NULL, NULL, 2, 'Synthetic demo', 'new', 'spectral-baseline',
    0.7, 10, 1000, 'medium', '/b', '/a', '/m', '2025-02-02 00:00:00', NULL, NULL, NULL, 'synthetic_test');
INSERT INTO findings VALUES ('f1', 'real', 'deforestation', 'VEGETATION_LOSS', 0.7, 'medium', 10, 1000, 1, 1,
    '1,1,2,2', '{}');
INSERT INTO findings VALUES ('f2', 'demo', 'mining', 'MINING_LAND_DISTURBANCE', 0.7, 'medium', 10, 1000, 1, 1,
    '1,1,2,2', '{}');
INSERT INTO alerts VALUES ('a1', 'real', 'f1', 'Old alert', 'Message', 'medium', 'open', NULL,
    '2025-02-02 00:00:00', NULL);
INSERT INTO alerts VALUES ('a2', 'demo', 'f2', 'Fake alert', 'Message', 'medium', 'open', NULL,
    '2025-02-02 00:00:00', NULL);
"""


@pytest.fixture
def old_database(tmp_path, monkeypatch):
    import app.database as database
    import app.migrations as migrations
    import app.snapshot as snapshot

    path = tmp_path / "old.db"
    connection = sqlite3.connect(path)
    connection.executescript(OLD_SCHEMA)
    connection.commit()
    connection.close()
    old_engine = make_engine(f"sqlite:///{path}")
    database.SessionLocal.configure(bind=old_engine)
    monkeypatch.setattr(database, "engine", old_engine)
    monkeypatch.setattr(migrations, "engine", old_engine)
    monkeypatch.setattr(snapshot, "engine", old_engine)
    yield path
    old_engine.dispose()


def test_old_database_upgrades_in_place_and_keeps_its_real_records(old_database):
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        cases = client.get("/api/cases").json()
        assert [case["id"] for case in cases] == ["real"], "Synthetic fixtures are removed"
        case = client.get("/api/cases/real").json()
        assert case["origin"] == "live"
        assert case["findings"][0]["category"] == "DEFORESTATION"
        assert case["findings"][0]["verdict"] == "NEEDS_REVIEW"
        alerts = client.get("/api/alerts").json()
        assert [alert["id"] for alert in alerts] == ["a1"]
        assert alerts[0]["category"] == "DEFORESTATION"
        assert alerts[0]["created_at"].endswith("Z")
        assert client.get("/api/overview").status_code == 200
        assert client.get("/api/departments").json() == []


def test_migration_can_run_twice(old_database):
    from app.migrations import migrate_schema

    migrate_schema()
    migrate_schema()
    db = SessionLocal()
    try:
        assert db.get(Case, "real") is not None
    finally:
        db.close()


def test_sqlite_runs_in_wal_with_foreign_keys_and_a_long_busy_timeout(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'pragma.db'}")
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 30000
    engine.dispose()


# Seeding ------------------------------------------------------------------------------


SITE = {
    "key": "test-site",
    "name": "Test Site",
    "state": "Delhi",
    "district": "North",
    "site_type": "disaster",
    "bbox": [77.20, 28.65, 77.25, 28.70],
    "categories": ["FLOOD"],
    "description": "A description",
    "interval": 720,
    "sources": [{"title": "t", "url": "https://example.invalid"}],
    "replay": {"start_date": "2023-06-08", "end_date": "2023-07-13", "note": "note"},
    "max_cloud_cover": 85,
}


def _areas():
    db = SessionLocal()
    try:
        rows = db.query(MonitoringArea).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def test_shipped_seed_files_are_well_formed_and_only_use_example_mailboxes():
    sites = load_sites()
    assert len(sites) == 7
    assert len({site["key"] for site in sites}) == 7
    for site in sites:
        assert set(site) >= {"key", "name", "state", "bbox", "categories", "interval", "sources", "replay"}
        assert set(site["replay"]) == {"start_date", "end_date", "note"}
    assert next(site for site in sites if "Yamuna" in site["name"])["max_cloud_cover"] == 85
    from app.schemas import AuthorisedZoneCreate

    for entry in json.loads(ZONES_FILE.read_text(encoding="utf-8")):
        assert AuthorisedZoneCreate(**entry).is_illustrative is True


def test_seed_preserves_the_replay_suggestion_and_payload_exposes_it():
    from app.services.payloads import area_payload

    assert seed_sites([SITE]) == 1
    area = _areas()[0]
    payload = area_payload(area)
    assert payload["suggested_replay"] == SITE["replay"]
    assert payload["site_key"] == "test-site"
    assert area.max_cloud_cover == 85.0
    assert area.next_check_at is not None


def test_changed_entry_updates_the_row_by_key_even_when_renamed():
    seed_sites([SITE])
    original = _areas()[0].id
    assert seed_sites([SITE]) == 0, "An unchanged file changes nothing"
    changed = dict(SITE, name="Renamed", bbox=[77.21, 28.65, 77.26, 28.70])
    assert seed_sites([changed]) == 1
    areas = _areas()
    assert len(areas) == 1 and areas[0].id == original
    assert areas[0].name == "Renamed"
    assert json.loads(areas[0].bbox_json)[0] == 77.21


def test_row_from_before_keys_is_adopted_by_name():
    legacy = make_area(name="Test Site")
    seed_sites([SITE])
    areas = _areas()
    assert len(areas) == 1 and areas[0].id == legacy and areas[0].site_key == "test-site"


def test_api_edit_survives_a_restart_until_the_file_changes():
    seed_sites([SITE])
    db = SessionLocal()
    row = db.query(MonitoringArea).first()
    row.monitoring_interval_minutes = 60
    db.commit()
    db.close()
    seed_sites([SITE])
    assert _areas()[0].monitoring_interval_minutes == 60


def test_site_deleted_through_the_api_stays_deleted():
    from fastapi.testclient import TestClient

    from app.main import app

    seed_sites([SITE])
    with TestClient(app) as client:
        area_id = client.get("/api/areas").json()[0]["id"]
        assert client.delete(f"/api/areas/{area_id}").status_code == 204
    seed_sites([SITE])
    assert _areas() == []


def test_zones_seed_idempotently_with_their_flag(monkeypatch):
    seed_sites([])
    seed_sites([])
    db = SessionLocal()
    try:
        zones = db.query(AuthorisedZone).all()
        expected = json.loads(ZONES_FILE.read_text(encoding="utf-8"))
        assert len(zones) == len(expected)
        assert {zone.is_illustrative for zone in zones} == {True}
        by_name = {zone.name: zone for zone in zones}
        for entry in expected:
            assert by_name[entry["name"]].valid_to == entry["valid_to"]
    finally:
        db.close()


def test_seeding_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "seed_sites_on_first_run", False)
    assert apply_seed_files() == 0
    assert _areas() == []


# Snapshot -----------------------------------------------------------------------------


def _analysed(monkeypatch):
    monkeypatch.setattr("app.services.monitoring.fetch_case_context", lambda *a, **k: quiet_context())
    monkeypatch.setattr("app.services.monitoring.read_slope_degrees", lambda grid: None)
    install({days_ago(10): Scene(FOREST, BARE), days_ago(40): Scene(FOREST), days_ago(10 + 365): Scene(FOREST)})
    area = make_area()
    execute_monitoring_run(queue(area))
    return area


def test_snapshot_exports_jpeg_only_and_restores_into_an_empty_database(monkeypatch, tmp_path):
    _analysed(monkeypatch)
    snapshot = tmp_path / "snap"
    manifest = export_snapshot(snapshot)
    assert manifest["counts"]["cases"] == 1 and manifest["counts"]["alerts"] == 1
    assert manifest["images"] > 5
    suffixes = {path.suffix for path in (snapshot / "storage").rglob("*") if path.is_file()}
    assert suffixes == {".jpg"}
    assert not (snapshot / "storage" / "satellite-cache").exists()

    import app.database as database
    import app.migrations as migrations
    import app.snapshot as snapshot_module
    from app.migrations import migrate_schema

    fresh = make_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    previous = database.SessionLocal.kw.get("bind")
    database.SessionLocal.configure(bind=fresh)
    monkeypatch.setattr(database, "engine", fresh)
    monkeypatch.setattr(migrations, "engine", fresh)
    monkeypatch.setattr(snapshot_module, "engine", fresh)
    monkeypatch.setattr(settings, "snapshot_dir", snapshot)
    monkeypatch.setattr(settings, "restore_snapshot_on_first_run", True)
    shutil.rmtree(settings.output_dir)
    try:
        migrate_schema()
        assert restore_snapshot_if_empty() is True
        db = SessionLocal()
        try:
            assert db.query(Case).count() == 1
            assert db.query(Alert).count() == 1
            assert db.query(Finding).count() >= 1
            image = db.query(Case).first().before_image
        finally:
            db.close()
        from app.services.storage import resolve_public_path

        assert resolve_public_path(image) is not None
        assert restore_snapshot_if_empty() is False, "A populated database is left alone"
    finally:
        database.SessionLocal.configure(bind=previous)
        fresh.dispose()


def test_snapshot_restore_without_files_does_nothing(tmp_path):
    assert restore_snapshot(tmp_path / "absent") is False
