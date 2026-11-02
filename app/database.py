"""Database session management.

Workers and API requests each use their own session. SQLite runs in WAL mode so a long
analysis never blocks readers, and a 30 s busy timeout absorbs brief writer contention.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)
    created = create_engine(
        url,
        connect_args={"check_same_thread": False, "timeout": 30},
        pool_size=16,
        max_overflow=16,
        pool_timeout=30,
        pool_pre_ping=True,
    )

    @event.listens_for(created, "connect")
    def _configure_sqlite(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return created


settings.ensure_directories()
engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
