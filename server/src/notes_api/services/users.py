"""The user directory: registered users, their IDs, display names, and creation times."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from notes_api import cursors
from notes_api.http.problems import NotFound
from notes_api.models import User

COLLECTION = "users"


def list_users(session: Session, *, caller: User, limit: int, cursor: str | None) -> cursors.Page[User]:
    """Every registered user, newest first."""
    return cursors.paginate(
        session,
        select(User),
        moment=User.created_at,
        id_column=User.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        fingerprint=cursors.fingerprint(caller.id, COLLECTION, {}, limit),
    )


def get_user(session: Session, user_id: uuid.UUID) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFound()
    return user
