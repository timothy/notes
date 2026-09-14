"""Engine configuration, real write serialization on SQLite, and the unit-of-work test seam."""

import threading
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from notes_api import uow
from notes_api.clock import SystemClock
from notes_api.db import make_engine, make_session_factory
from notes_api.models import Base, Team

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = make_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_session_factory(engine)


def _new_team(name: str) -> Team:
    return Team(id=uuid.uuid4(), name=name, created_at=NOW, updated_at=NOW)


def test_sqlite_engine_enables_foreign_keys_and_wal(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"


def test_concurrent_read_then_write_transactions_serialize(factory: sessionmaker[Session]) -> None:
    team = _new_team("0")
    with factory() as session, uow.transaction(session, "seed"):
        session.add(team)
    start = threading.Barrier(2)
    errors: list[BaseException] = []

    def bump() -> None:
        try:
            start.wait(timeout=5)
            with factory() as session, uow.transaction(session, "bump"):
                current = session.get_one(Team, team.id)
                time.sleep(0.2)
                current.name = str(int(current.name) + 1)
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=bump) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert errors == []
    with factory() as session:
        assert session.get_one(Team, team.id).name == "2"


def test_before_begin_hook_runs_before_the_transaction_starts(
    factory: sessionmaker[Session], restore_hooks: None
) -> None:
    competitor = _new_team("competitor")
    seen: list[str] = []

    def before_begin(op: str) -> None:
        seen.append(op)
        with factory() as other, uow.transaction(other, "competitor"):
            other.add(competitor)

    uow.hooks.before_begin = before_begin
    with factory() as session, uow.transaction(session, "primary"):
        assert session.get(Team, competitor.id) is not None
    assert seen == ["primary"], "the competitor's own transaction must not re-trigger the hook"


def test_objects_stay_usable_after_commit(factory: sessionmaker[Session]) -> None:
    team = _new_team("kept")
    with factory() as session, uow.transaction(session, "seed"):
        session.add(team)
    assert team.name == "kept"


def test_system_clock_is_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None and now.utcoffset() is not None
    assert now.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
