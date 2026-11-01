"""Keep test runs isolated from the user's local SentinelWatch evidence database."""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    """Bind the application session factory/migrator to one temporary SQLite file."""

    import app.database as database
    import app.migrations as migrations
    from app.database import Base

    original_bind = database.SessionLocal.kw.get("bind")
    test_engine = create_engine(
        f"sqlite:///{tmp_path / 'sentinelwatch-test.db'}", connect_args={"check_same_thread": False}
    )
    database.SessionLocal.configure(bind=test_engine)
    monkeypatch.setattr(database, "engine", test_engine)
    monkeypatch.setattr(migrations, "engine", test_engine)
    Base.metadata.create_all(bind=test_engine)
    try:
        yield
    finally:
        Base.metadata.drop_all(bind=test_engine)
        database.SessionLocal.configure(bind=original_bind)
        test_engine.dispose()
