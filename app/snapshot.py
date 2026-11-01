"""Export real results to demo_snapshot/ and restore them into an empty database.

A fresh clone then starts populated with genuine prior analyses and needs no re-download.
Only JPEG evidence travels, because the snapshot lives in git; raw imagery windows and the
outbox do not. Everything in a snapshot came from real scenes, and rows keep their dates.

    python -m app.snapshot export
    python -m app.snapshot restore
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import settings
from app.database import Base, engine
from app.timeutil import iso_z, utcnow

logger = logging.getLogger(__name__)

SNAPSHOT_DB = "sentinelwatch.db"
MANIFEST = "manifest.json"
_IMAGE_SUFFIXES = {".jpg", ".jpeg"}


def _database_path() -> Path:
    database = make_url(str(engine.url)).database
    if engine.dialect.name != "sqlite" or not database:
        raise RuntimeError("Snapshots support the SQLite database only.")
    return Path(database)


def export_snapshot(destination: Path | None = None) -> dict:
    """Write a compact copy of the database and JPEG evidence, replacing any earlier one."""

    target = destination or settings.snapshot_dir
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    database_copy = target / SNAPSHOT_DB
    source = sqlite3.connect(_database_path())
    try:
        # VACUUM INTO gives a consistent, defragmented copy even while the server is running.
        source.execute("VACUUM INTO ?", (str(database_copy),))
    finally:
        source.close()
    copy = sqlite3.connect(database_copy)
    try:
        # The catalogue records and audit stay; transient queue state does not travel.
        copy.execute(
            "UPDATE monitoring_runs SET status = 'FAILED', stage = 'FAILED', error_code = 'NOT_IN_SNAPSHOT', "
            "message = 'Interrupted when this snapshot was taken.' WHERE status IN ('QUEUED', 'RUNNING')"
        )
        # Outbox bodies do not travel; retain delivery history without dangling download links.
        copy.execute("UPDATE notification_attempts SET file_path = NULL WHERE file_path IS NOT NULL")
        copy.commit()
        counts = {
            table: copy.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in ("monitoring_areas", "cases", "findings", "alerts", "analyses")
        }
    finally:
        copy.close()
    copied = 0
    images_bytes = 0
    outputs = settings.output_dir
    for path in outputs.rglob("*") if outputs.is_dir() else []:
        if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES:
            relative = path.relative_to(settings.storage_dir)
            output = target / "storage" / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, output)
            copied += 1
            images_bytes += path.stat().st_size
    manifest = {
        "created_at": iso_z(utcnow()),
        "counts": counts,
        "images": copied,
        "images_bytes": images_bytes,
        "database_bytes": database_copy.stat().st_size,
    }
    (target / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def _database_empty() -> bool:
    with engine.connect() as connection:
        for table in ("monitoring_areas", "cases"):
            if connection.exec_driver_sql(f'SELECT 1 FROM "{table}" LIMIT 1').first() is not None:
                return False
    return True


def restore_snapshot(source: Path | None = None) -> bool:
    """Copy a snapshot's rows and images in. Column intersection tolerates a newer schema."""

    origin = source or settings.snapshot_dir
    snapshot_database = origin / SNAPSHOT_DB
    if not snapshot_database.is_file():
        return False
    connection = sqlite3.connect(_database_path(), timeout=30)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("ATTACH DATABASE ? AS snapshot", (str(snapshot_database),))
        for table in Base.metadata.sorted_tables:
            name = table.name
            exists = connection.execute(
                "SELECT 1 FROM snapshot.sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            if not exists:
                continue
            ours = [row[1] for row in connection.execute(f'PRAGMA main.table_info("{name}")')]
            theirs = {row[1] for row in connection.execute(f'PRAGMA snapshot.table_info("{name}")')}
            columns = ", ".join(f'"{column}"' for column in ours if column in theirs)
            connection.execute(
                f'INSERT OR IGNORE INTO main."{name}" ({columns}) SELECT {columns} FROM snapshot."{name}"'
            )
        # Evidence dates remain historical. Poll live catalogues immediately on this install,
        # rather than inheriting a deadline from the machine that exported the snapshot.
        connection.execute(
            "UPDATE monitoring_areas SET next_check_at = ? WHERE status = 'ACTIVE'",
            (utcnow().isoformat(" "),),
        )
        connection.commit()
        connection.execute("DETACH DATABASE snapshot")
    finally:
        connection.close()
    stored = origin / "storage"
    if stored.is_dir():
        shutil.copytree(stored, settings.storage_dir, dirs_exist_ok=True)
    logger.info("Restored the demo snapshot from %s.", origin)
    return True


def restore_snapshot_if_empty() -> bool:
    if not settings.restore_snapshot_on_first_run or not (settings.snapshot_dir / MANIFEST).is_file():
        return False
    try:
        if not _database_empty():
            return False
        return restore_snapshot()
    except (RuntimeError, sqlite3.Error, OSError):
        logger.exception("Could not restore the demo snapshot; continuing without it.")
        return False


if __name__ == "__main__":
    from app.migrations import migrate_schema

    parser = argparse.ArgumentParser(description="Export or restore a SentinelWatch data snapshot.")
    parser.add_argument("action", choices=["export", "restore"])
    options = parser.parse_args()
    settings.ensure_directories()
    migrate_schema()
    if options.action == "export":
        print(json.dumps(export_snapshot(), indent=2))
    elif _database_empty():
        print("Restored." if restore_snapshot() else "No snapshot found.")
    else:
        print("The database is not empty, so nothing was restored.")
