"""The initial migration creates the whole schema, agrees with the models, and cascades deletes."""

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event, func, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from notes_api import models
from notes_api.config import Settings

SERVER_DIR = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 13, 12, 0, 0, 123456, tzinfo=UTC)


def test_migration_cli_needs_only_database_url(settings: Settings) -> None:
    env = {name: value for name, value in os.environ.items() if not name.startswith("OIDC_")}
    env.pop("CURSOR_SIGNING_KEY", None)
    env["DATABASE_URL"] = settings.database_url
    command_line = [sys.executable, "-m", "alembic"]
    try:
        result = subprocess.run(
            [*command_line, "upgrade", "head"],
            env=env,
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        again = subprocess.run(
            [*command_line, "upgrade", "head"],
            env=env,
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
        assert again.returncode == 0, again.stderr
    finally:
        subprocess.run(
            [*command_line, "downgrade", "base"],
            env=env,
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            check=True,
        )


def alembic_config(url: str) -> Config:
    config = Config(str(SERVER_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


@pytest.fixture
def database_url(tmp_path: Path) -> str:
    return os.environ.get("NOTES_API_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'schema.sqlite'}"


@pytest.fixture
def migrated(database_url: str) -> Iterator[Engine]:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection: object, _record: object) -> None:
            dbapi_connection.execute("PRAGMA foreign_keys=ON")  # type: ignore[attr-defined]

    yield engine
    engine.dispose()
    command.downgrade(config, "base")


def test_migration_applies_and_matches_the_models(database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, "head")
    try:
        command.check(config)
    finally:
        command.downgrade(config, "base")


def _seed_note_with_children(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
    """Insert one note with every kind of dependent row, flushing parents before children."""
    ada = models.User(
        id=uuid.uuid4(), issuer="https://issuer.example", subject="ada", display_name="Ada", created_at=NOW
    )
    session.add(ada)
    session.flush()
    note = models.Note(
        id=uuid.uuid4(),
        author_id=ada.id,
        title="Release checklist",
        body="",
        title_fold="release checklist",
        body_fold="",
        review_mode="self_merge",
        review_required_approvals=None,
        created_at=NOW,
        updated_at=NOW,
        version="v1",
    )
    session.add(note)
    session.flush()
    request = models.EditRequest(
        id=uuid.uuid4(),
        note_id=note.id,
        proposer_id=ada.id,
        status="open",
        base_title="Release checklist",
        base_body="",
        proposed_title="Release runbook",
        proposed_body="",
        created_at=NOW,
        updated_at=NOW,
        version="v1",
    )
    session.add_all(
        [
            models.NoteOwner(note_id=note.id, user_id=ada.id, position=0, added_at=NOW),
            models.NoteTag(note_id=note.id, position=0, tag="release"),
            models.Share(
                id=uuid.uuid4(),
                note_id=note.id,
                recipient_type="user",
                recipient_id=uuid.uuid4(),
                can_comment=True,
                can_propose=False,
                created_at=NOW,
                updated_at=NOW,
            ),
            models.Comment(
                id=uuid.uuid4(),
                note_id=note.id,
                author_id=ada.id,
                body="c",
                created_at=NOW,
                updated_at=NOW,
                version="v1",
            ),
            request,
        ]
    )
    session.flush()
    session.add_all(
        [
            models.Approval(request_id=request.id, user_id=ada.id, approved_at=NOW),
            models.RequestComment(
                id=uuid.uuid4(),
                request_id=request.id,
                author_id=ada.id,
                body="rc",
                created_at=NOW,
                updated_at=NOW,
                version="v1",
            ),
        ]
    )
    session.flush()
    return ada.id, note.id


def test_deleting_a_note_cascades_to_every_dependent_row(migrated: Engine) -> None:
    with Session(migrated) as session, session.begin():
        _, note_id = _seed_note_with_children(session)
    with Session(migrated) as session, session.begin():
        session.delete(session.get_one(models.Note, note_id))
    with Session(migrated) as session:
        for table in (
            models.NoteOwner,
            models.NoteTag,
            models.Share,
            models.Comment,
            models.EditRequest,
            models.Approval,
            models.RequestComment,
        ):
            assert session.scalar(select(func.count()).select_from(table)) == 0, table.__tablename__
        assert session.scalar(select(func.count()).select_from(models.User)) == 1


def test_trash_columns_must_be_set_together(migrated: Engine) -> None:
    with Session(migrated) as session, session.begin():
        _, note_id = _seed_note_with_children(session)
    with pytest.raises(IntegrityError), Session(migrated) as session, session.begin():
        note = session.get_one(models.Note, note_id)
        note.deleted_at = NOW
        session.flush()
    with pytest.raises(IntegrityError), Session(migrated) as session, session.begin():
        note = session.get_one(models.Note, note_id)
        note.review_mode = "peer_approval"
        session.flush()


def test_timestamps_round_trip_with_microseconds_as_aware_utc(migrated: Engine) -> None:
    with Session(migrated) as session, session.begin():
        _, note_id = _seed_note_with_children(session)
    with Session(migrated) as session:
        note = session.get_one(models.Note, note_id)
        assert note.created_at == NOW
        assert note.created_at.tzinfo is not None and note.created_at.utcoffset() == timedelta(0)
        assert note.created_at.microsecond == 123456


def test_naive_timestamps_are_rejected(migrated: Engine) -> None:
    naive = NOW.replace(tzinfo=None)
    with pytest.raises(StatementError), Session(migrated) as session, session.begin():
        session.add(models.User(id=uuid.uuid4(), issuer="i", subject="s", display_name="n", created_at=naive))
        session.flush()
