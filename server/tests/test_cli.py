"""`notes-api purge-expired` deletes exactly the expired notes and their children, and it fails fast like
the server when the configuration is missing."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from notes_api.cli import main, purge_expired
from notes_api.config import Settings
from notes_api.models import Comment, Note, NoteOwner, NoteTag, Share, User

NOW = datetime.now(UTC)


def seed(app: FastAPI) -> dict[str, uuid.UUID]:
    """Three notes: expired an hour ago, expiring in an hour, and active; the expired one has children."""
    ids = {"expired": uuid.uuid4(), "pending": uuid.uuid4(), "active": uuid.uuid4()}
    author = uuid.uuid4()
    with app.state.session_factory() as session, session.begin():
        session.add(User(id=author, issuer="i", subject="ada", display_name="Ada", created_at=NOW))
        session.flush()
        for name, note_id in ids.items():
            deleted = (
                None
                if name == "active"
                else NOW - timedelta(hours=720) + timedelta(hours=-1 if name == "expired" else 1)
            )
            session.add(
                Note(
                    id=note_id,
                    author_id=author,
                    title=name,
                    body="",
                    title_fold=name,
                    body_fold="",
                    review_mode="self_merge",
                    review_required_approvals=None,
                    created_at=NOW,
                    updated_at=NOW,
                    deleted_at=deleted,
                    expires_at=None if deleted is None else deleted + timedelta(hours=720),
                    version="v",
                )
            )
            session.flush()
            session.add(NoteOwner(note_id=note_id, user_id=author, position=0, added_at=NOW))
            session.add(NoteTag(note_id=note_id, position=0, tag=name))
            session.add(
                Share(
                    id=uuid.uuid4(),
                    note_id=note_id,
                    recipient_type="team",
                    recipient_id=uuid.uuid4(),
                    can_comment=False,
                    can_propose=False,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            session.add(
                Comment(
                    id=uuid.uuid4(),
                    note_id=note_id,
                    author_id=author,
                    body="c",
                    created_at=NOW,
                    updated_at=NOW,
                    version="c",
                )
            )
    return ids


def counts(app: FastAPI) -> dict[str, int]:
    with app.state.session_factory() as session:
        return {
            model.__tablename__: int(session.execute(select(func.count()).select_from(model)).scalar_one())
            for model in (Note, NoteOwner, NoteTag, Share, Comment, User)
        }


def test_purge_deletes_exactly_the_expired_notes_and_their_children(app: FastAPI) -> None:
    ids = seed(app)
    assert counts(app) == {
        "notes": 3,
        "note_owners": 3,
        "note_tags": 3,
        "shares": 3,
        "comments": 3,
        "users": 1,
    }
    assert purge_expired(app.state.session_factory, NOW) == 1
    assert counts(app) == {
        "notes": 2,
        "note_owners": 2,
        "note_tags": 2,
        "shares": 2,
        "comments": 2,
        "users": 1,
    }
    with app.state.session_factory() as session:
        remaining = {row.title for row in session.execute(select(Note)).scalars()}
    assert remaining == {"pending", "active"}
    assert ids["expired"] not in {row for row in remaining}
    assert purge_expired(app.state.session_factory, NOW) == 0
    assert purge_expired(app.state.session_factory, NOW + timedelta(hours=2)) == 1


def test_the_command_reads_the_servers_settings_and_reports_the_count(
    app: FastAPI, settings: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seed(app)
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    monkeypatch.setenv("OIDC_ISSUER", settings.oidc_issuer)
    monkeypatch.setenv("OIDC_AUDIENCE", settings.oidc_audience)
    monkeypatch.setenv("OIDC_JWKS", settings.oidc_jwks or "")
    monkeypatch.delenv("OIDC_JWKS_URL", raising=False)
    assert main(["purge-expired"]) == 0
    assert capsys.readouterr().out == "purged 1 expired notes\n"
    assert main(["purge-expired"]) == 0
    assert capsys.readouterr().out == "purged 0 expired notes\n"


def test_missing_settings_fail_fast_without_leaking_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://notes:s3cret-password@db:5432/notes")
    for name in ("OIDC_ISSUER", "OIDC_AUDIENCE", "OIDC_JWKS", "OIDC_JWKS_URL"):
        monkeypatch.delenv(name, raising=False)
    assert main(["purge-expired"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "OIDC_ISSUER" in captured.err and "s3cret-password" not in captured.err


def test_the_module_answers_help_without_any_configuration() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "notes_api.cli", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0 and "purge-expired" in result.stdout
