"""Database plumbing shared by the models and the application.

``UTCDateTime`` keeps every timestamp aware and in UTC: it rejects naive values, stores microseconds on
both backends (``timestamptz`` on PostgreSQL, a naive UTC text value on SQLite, whose fixed six fractional
digits keep lexical order chronological), and always returns aware UTC.

``make_engine`` gives SQLite real write serialization. pysqlite's legacy mode emits ``BEGIN`` lazily before
the first write, so a read followed by a write holds no lock in between; the engine therefore disables
that behavior and emits ``BEGIN IMMEDIATE`` itself when SQLAlchemy starts a transaction, turns on WAL and
foreign keys, and waits on a busy database instead of failing. PostgreSQL needs none of this and gets a
pre-pinging pool.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, DateTime, Dialect, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.types import TypeDecorator, TypeEngine

SQLITE_BUSY_TIMEOUT_SECONDS = 5


class UTCDateTime(TypeDecorator[datetime]):
    impl = DateTime(timezone=True)
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(DateTime())
        return dialect.type_descriptor(DateTime(timezone=True))

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        value = value.astimezone(UTC)
        return value.replace(tzinfo=None) if dialect.name == "sqlite" else value

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def make_engine(url: str) -> Engine:
    if not url.startswith("sqlite"):
        return create_engine(url, pool_pre_ping=True)

    engine = create_engine(
        url, connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_SECONDS}
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None  # we emit BEGIN ourselves, see begin_immediate
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_SECONDS * 1000}")
        cursor.close()

    @event.listens_for(engine, "begin")
    def begin_immediate(connection: Connection) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessions whose objects stay readable after commit, so responses can be built from them."""
    return sessionmaker(bind=engine, expire_on_commit=False)
