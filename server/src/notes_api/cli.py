"""Operator commands, installed as ``notes-api`` in the image's virtualenv.

``notes-api purge-expired`` permanently deletes notes whose trash retention has ended, together with
everything that hangs off them (owners, tags, shares, comments, edit requests, approvals, request
comments) through the foreign keys. Expiry is already enforced at read time, so this only reclaims
storage; run it from an external scheduler such as a cron job or a Kubernetes CronJob using the same
image and the same environment as the server. It reads the same settings as the server and fails the
same way when they are missing.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete
from sqlalchemy.orm import Session, sessionmaker

from notes_api import uow
from notes_api.clock import SystemClock
from notes_api.config import ConfigurationError, load_settings
from notes_api.db import make_engine, make_session_factory
from notes_api.models import Note


def purge_expired(session_factory: sessionmaker[Session], now: datetime) -> int:
    """Delete every note at or past its ``expires_at``; returns how many notes went."""
    with session_factory() as session, uow.transaction(session, "purge_expired"):
        result = cast(CursorResult[Any], session.execute(delete(Note).where(Note.expires_at <= now)))
        return int(result.rowcount)  # the notes themselves; cascaded rows are not counted


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="notes-api", description="Operator commands for the Notes API server."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "purge-expired",
        help="permanently delete notes whose trash retention has ended, and everything that hangs off them",
    )
    parser.parse_args(argv)
    try:
        settings = load_settings()
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    engine = make_engine(settings.database_url)
    try:
        count = purge_expired(make_session_factory(engine), SystemClock().now())
    finally:
        engine.dispose()
    print(f"purged {count} expired notes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
