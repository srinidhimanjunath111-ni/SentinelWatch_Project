"""Keep test runs isolated from the user's data and from the network."""

from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

# Set before anything imports app.config, which reads the environment once.
_TEST_ROOT = Path(tempfile.mkdtemp(prefix="sentinelwatch-tests-"))
os.environ.update(
    {
        "ENABLE_MONITORING_SCHEDULER": "false",
        "SENTINELWATCH_SEED_SITES": "false",
        "SENTINELWATCH_RESTORE_SNAPSHOT": "false",
        "SENTINELWATCH_ADMIN_PASSWORD": "",
        "DATABASE_URL": f"sqlite:///{_TEST_ROOT / 'never-used.db'}",
        "STORAGE_DIR": str(_TEST_ROOT / "storage"),
        "LOG_DIR": str(_TEST_ROOT / "logs"),
        "SMTP_HOST": "",
        "SMTP_FROM": "",
        "SMTP_REDIRECT_TO": "",
        "COPERNICUS_CLIENT_ID": "",
        "COPERNICUS_CLIENT_SECRET": "",
        "PROVIDER_BACKOFF_SECONDS": "0",
        "DISPATCH_BACKOFF_SECONDS": "0",
    }
)

import pytest  # noqa: E402


class NetworkAccessInTests(AssertionError):
    pass


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real socket connection fails loudly; tests mock providers and context."""

    def refuse(self, address):
        raise NetworkAccessInTests(f"Test tried to connect to {address}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Give each test its own SQLite file and storage tree."""

    import app.database as database
    import app.migrations as migrations
    import app.snapshot as snapshot
    from app.config import settings
    from app.database import Base, make_engine

    original_bind = database.SessionLocal.kw.get("bind")
    test_engine = make_engine(f"sqlite:///{tmp_path / 'sentinelwatch-test.db'}")
    database.SessionLocal.configure(bind=test_engine)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(migrations, "engine", test_engine)
    monkeypatch.setattr(snapshot, "engine", test_engine)
    monkeypatch.setattr(settings, "storage_dir", tmp_path / "storage")
    settings.ensure_directories()
    Base.metadata.create_all(bind=test_engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(bind=test_engine)
        database.SessionLocal.configure(bind=original_bind)
        test_engine.dispose()
