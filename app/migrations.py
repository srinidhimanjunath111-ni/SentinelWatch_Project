"""Small, dependency-free SQLite compatibility migrations.

This project deliberately avoids hiding schema changes behind an unconfigured
migration framework.  New installations are created from SQLAlchemy metadata;
these additive migrations keep the earlier final-year prototype database usable.
For production, replace this module with reviewed Alembic revisions.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.database import Base, engine

_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "cases": {
        "aoi_id": "VARCHAR(36)",
        "analysis_id": "VARCHAR(36)",
        "before_acquisition_id": "VARCHAR(36)",
        "after_acquisition_id": "VARCHAR(36)",
        "data_origin": "VARCHAR(48)",
        "requires_human_verification": "BOOLEAN DEFAULT 1",
        "model_version_label": "VARCHAR(128)",
        "quality_note": "TEXT",
    },
    "findings": {
        "category": "VARCHAR(48) DEFAULT 'OTHER_CHANGE'",
        "geometry_json": "TEXT",
        "ndvi_before": "FLOAT",
        "ndvi_after": "FLOAT",
        "ndvi_delta": "FLOAT",
        "persistence_score": "FLOAT",
        "requires_human_verification": "BOOLEAN DEFAULT 1",
    },
    "alerts": {
        "department_id": "VARCHAR(36)",
        "priority_score": "FLOAT",
    },
}


def migrate_schema() -> None:
    """Create new tables and safely add columns introduced after the prototype."""

    Base.metadata.create_all(bind=engine)
    if engine.dialect.name != "sqlite":
        return
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    with engine.begin() as connection:
        for table, additions in _ADDITIVE_COLUMNS.items():
            if table not in existing_tables:
                continue
            present = {
                row[1]
                for row in connection.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            }
            for column, sql_type in additions.items():
                if column not in present:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}"
                    )
