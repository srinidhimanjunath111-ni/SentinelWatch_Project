"""Small, dependency-free SQLite compatibility migrations.

This project deliberately avoids hiding schema changes behind an unconfigured
migration framework. New installations are created from SQLAlchemy metadata; these
additive migrations upgrade an existing database in place without losing data.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect

from app.database import Base, engine
from app.vocabulary import LEGACY_CATEGORY_MAP

logger = logging.getLogger(__name__)

_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "monitoring_areas": {
        "state": "VARCHAR(80)",
        "district": "VARCHAR(120)",
        "site_type": "VARCHAR(64)",
        "description": "TEXT",
        "sources_json": "TEXT",
        "next_check_at": "DATETIME",
        "site_key": "VARCHAR(64)",
        "seed_hash": "VARCHAR(64)",
        "suggested_replay_json": "TEXT",
    },
    "satellite_acquisitions": {
        "scene_key": "VARCHAR(64)",
        "tile": "VARCHAR(16)",
    },
    "aoi_acquisitions": {
        "aoi_clear_fraction": "FLOAT",
        "scene_status": "VARCHAR(32) DEFAULT 'DISCOVERED'",
        "thumbnail": "VARCHAR(512)",
        "case_id": "VARCHAR(36)",
    },
    "monitoring_runs": {
        "origin": "VARCHAR(24) DEFAULT 'live'",
        "trigger": "VARCHAR(32)",
        "as_of_date": "VARCHAR(10)",
        "replay_id": "VARCHAR(36)",
        "replay_force": "BOOLEAN DEFAULT 0",
        "provider": "VARCHAR(64)",
    },
    "cases": {
        "aoi_id": "VARCHAR(36)",
        "analysis_id": "VARCHAR(36)",
        "before_acquisition_id": "VARCHAR(36)",
        "after_acquisition_id": "VARCHAR(36)",
        "data_origin": "VARCHAR(48)",
        "requires_human_verification": "BOOLEAN DEFAULT 1",
        "model_version_label": "VARCHAR(128)",
        "quality_note": "TEXT",
        "origin": "VARCHAR(24) DEFAULT 'live'",
        "replay_id": "VARCHAR(36)",
        "image_bounds_json": "VARCHAR(160)",
        "images_json": "TEXT",
        "seasonal_reference_json": "TEXT",
        "context_json": "TEXT",
    },
    "findings": {
        "category": "VARCHAR(48) DEFAULT 'OTHER_CHANGE'",
        "geometry_json": "TEXT",
        "ndvi_before": "FLOAT",
        "ndvi_after": "FLOAT",
        "ndvi_delta": "FLOAT",
        "persistence_score": "FLOAT",
        "requires_human_verification": "BOOLEAN DEFAULT 1",
        "verdict": "VARCHAR(32)",
        "verdict_reasons_json": "TEXT",
        "transition_from": "VARCHAR(24)",
        "transition_to": "VARCHAR(24)",
        "indices_json": "TEXT",
        "change_probability": "FLOAT",
        "seasonal_persists": "BOOLEAN",
        "centroid_lat": "FLOAT",
        "centroid_lon": "FLOAT",
        "thumbnail": "VARCHAR(512)",
        "alert_id": "VARCHAR(36)",
    },
    "alerts": {
        "department_id": "VARCHAR(36)",
        "priority_score": "FLOAT",
        "category": "VARCHAR(48)",
        "verdict": "VARCHAR(32)",
        "origin": "VARCHAR(24) DEFAULT 'live'",
        "replay_id": "VARCHAR(36)",
        "aoi_id": "VARCHAR(36)",
        "confidence": "FLOAT",
        "area_m2": "FLOAT",
        "acquisition_date": "VARCHAR(10)",
        "detected_at": "DATETIME",
        "dispatched_at": "DATETIME",
        "scene_processed_at": "DATETIME",
        "latency_seconds": "FLOAT",
        "persistence_count": "INTEGER DEFAULT 1",
        "last_seen_at": "DATETIME",
        "closed_at": "DATETIME",
        "status_note": "TEXT",
    },
    "departments": {
        "state": "VARCHAR(80)",
    },
    "notification_attempts": {
        "attempts": "INTEGER DEFAULT 1",
        "file_path": "VARCHAR(512)",
        "triggered_by": "VARCHAR(24) DEFAULT 'auto'",
    },
}

# create_all never adds an index to a table that already exists.
_ADDITIVE_INDEXES = {
    "ix_monitoring_areas_next_check_at": ("monitoring_areas", "next_check_at"),
    "ix_monitoring_areas_site_key": ("monitoring_areas", "site_key"),
    "ix_satellite_acquisitions_scene_key": ("satellite_acquisitions", "scene_key"),
    "ix_monitoring_runs_replay_id": ("monitoring_runs", "replay_id"),
    "ix_cases_origin": ("cases", "origin"),
    "ix_findings_verdict": ("findings", "verdict"),
    "ix_findings_alert_id": ("findings", "alert_id"),
    "ix_alerts_created_at": ("alerts", "created_at"),
    "ix_alerts_category": ("alerts", "category"),
    "ix_alerts_aoi_id": ("alerts", "aoi_id"),
    "ix_notification_attempts_created_at": ("notification_attempts", "created_at"),
}


def _remove_synthetic_records(connection) -> None:
    """Prototype fixtures were labelled synthetic; v3 retains only real observations."""

    synthetic = "(SELECT id FROM cases WHERE source LIKE 'Synthetic%' OR data_origin = 'synthetic_test')"
    connection.exec_driver_sql(
        f"DELETE FROM notification_attempts WHERE alert_id IN "
        f"(SELECT id FROM alerts WHERE case_id IN {synthetic})"
    )
    connection.exec_driver_sql(f"DELETE FROM alerts WHERE case_id IN {synthetic}")
    connection.exec_driver_sql(f"DELETE FROM findings WHERE case_id IN {synthetic}")
    connection.exec_driver_sql(f"DELETE FROM case_reviews WHERE case_id IN {synthetic}")
    connection.exec_driver_sql(f"DELETE FROM cases WHERE id IN {synthetic}")


def _backfill(connection) -> None:
    for legacy, current in LEGACY_CATEGORY_MAP.items():
        connection.exec_driver_sql(
            "UPDATE findings SET category = ? WHERE category = ?", (current, legacy)
        )
    connection.exec_driver_sql("UPDATE cases SET origin = 'live' WHERE origin IS NULL")
    connection.exec_driver_sql("UPDATE monitoring_runs SET origin = 'live' WHERE origin IS NULL")
    connection.exec_driver_sql("UPDATE findings SET verdict = 'NEEDS_REVIEW' WHERE verdict IS NULL")
    connection.exec_driver_sql(
        "UPDATE alerts SET origin = COALESCE(origin, 'live'), "
        "persistence_count = COALESCE(persistence_count, 1), "
        "detected_at = COALESCE(detected_at, created_at), "
        "verdict = COALESCE(verdict, 'NEEDS_REVIEW'), "
        "category = COALESCE(category, (SELECT category FROM findings WHERE findings.id = alerts.finding_id)), "
        "confidence = COALESCE(confidence, (SELECT confidence FROM findings WHERE findings.id = alerts.finding_id)), "
        "area_m2 = COALESCE(area_m2, (SELECT area_m2 FROM findings WHERE findings.id = alerts.finding_id)), "
        "aoi_id = COALESCE(aoi_id, (SELECT aoi_id FROM cases WHERE cases.id = alerts.case_id)), "
        "acquisition_date = COALESCE(acquisition_date, (SELECT after_date FROM cases WHERE cases.id = alerts.case_id)) "
        "WHERE category IS NULL OR origin IS NULL OR detected_at IS NULL"
    )


def migrate_schema() -> None:
    """Create new tables, add columns introduced after the prototype, and backfill them."""

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added = 0
    with engine.begin() as connection:
        for table, additions in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {
                row[1]
                for row in connection.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()
            }
            for column, sql_type in additions.items():
                if column not in present:
                    connection.exec_driver_sql(
                        f'ALTER TABLE "{table}" ADD COLUMN "{column}" {sql_type}'
                    )
                    added += 1
        for name, (table, column) in _ADDITIVE_INDEXES.items():
            if table in existing_tables:
                connection.exec_driver_sql(
                    f'CREATE INDEX IF NOT EXISTS {name} ON "{table}" ("{column}")'
                )
        _remove_synthetic_records(connection)
        _backfill(connection)
    if added:
        logger.info("Schema upgraded in place: %d column(s) added.", added)
