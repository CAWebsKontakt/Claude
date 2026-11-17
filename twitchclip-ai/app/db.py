"""Datenbankanbindung (SQLAlchemy 2.x, synchron – passt zu FastAPI-Threadpool und RQ-Workern)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, MetaData, create_engine
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from app.config import get_settings

# Einheitliche Namen für Constraints – wichtig für reproduzierbare Alembic-Migrationen.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# JSONB auf PostgreSQL, generisches JSON auf SQLite (Tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class UTCDateTime(TypeDecorator[datetime]):
    """Zeitstempel immer zeitzonenbewusst in UTC – auch auf SQLite (Tests)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class Database:
    def __init__(self, url: str, *, echo: bool = False) -> None:
        kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False}
            if ":memory:" in url or url.endswith("sqlite://"):
                kwargs["poolclass"] = StaticPool
        else:
            kwargs.update(pool_size=5, max_overflow=5, pool_recycle=1800)
        self.url = url
        self.engine: Engine = create_engine(url, **kwargs)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)

    def session(self) -> Session:
        return self.session_factory()

    def dispose(self) -> None:
        self.engine.dispose()


_database: Database | None = None


def get_database() -> Database:
    global _database
    if _database is None:
        _database = Database(get_settings().database_url)
    return _database


def set_database(database: Database | None) -> None:
    """Für Tests: eigene Datenbank einsetzen."""
    global _database
    _database = database


def get_db() -> Iterator[Session]:
    """FastAPI-Dependency: eine Session pro Request."""
    session = get_database().session()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Für Worker/CLI: commit bei Erfolg, rollback bei Fehler."""
    session = get_database().session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
