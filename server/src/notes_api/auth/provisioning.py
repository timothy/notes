"""From a verified identity to a local user: select, else insert under a SAVEPOINT, else re-select.

``(issuer, subject)`` maps to exactly one row of ``users``, enforced by the unique constraint. The lookup
and the insert are two separate transactions so that no lock is held between them; two callers arriving
with the same fresh identity both miss, one insert wins, and the other hits the constraint, rolls back to
its SAVEPOINT, and re-selects the winner's row. ``hooks.before_begin("provision_user")`` fires exactly
between the miss and the insert, which is how the tests reproduce the race deterministically on SQLite.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from notes_api import uow
from notes_api.auth.jwt import Identity
from notes_api.clock import Clock
from notes_api.models import User

FIND_OP = "find_user"
PROVISION_OP = "provision_user"


def get_or_create_user(session_factory: sessionmaker[Session], identity: Identity, clock: Clock) -> User:
    with session_factory() as session:
        with uow.transaction(session, FIND_OP):
            found = _find(session, identity)
        if found is not None:
            return found
        with uow.transaction(session, PROVISION_OP):
            try:
                with session.begin_nested():
                    user = User(
                        id=uuid.uuid4(),
                        issuer=identity.issuer,
                        subject=identity.subject,
                        display_name=identity.display_name,
                        created_at=clock.now(),
                    )
                    session.add(user)
                    session.flush()
                return user
            except IntegrityError:
                winner = _find(session, identity)
                if winner is None:  # pragma: no cover - the constraint that fired is the one we select by
                    raise
                return winner


def _find(session: Session, identity: Identity) -> User | None:
    statement = select(User).where(User.issuer == identity.issuer, User.subject == identity.subject)
    return session.execute(statement).scalar_one_or_none()
