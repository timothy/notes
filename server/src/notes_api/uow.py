"""Unit of work: the one place where transactions begin, with a test seam in front of each one.

Every mutation runs inside ``transaction(session, op)``. ``hooks.before_begin(op)`` is a no-op in
production; race tests replace it with a competitor that runs in its own session before the primary
transaction starts, which proves that every check the service makes lives inside the transaction. A hook
that itself opens transactions does not trigger the hook again.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy.orm import Session


def _no_hook(op: str) -> None:
    return None


@dataclass
class Hooks:
    before_begin: Callable[[str], None] = _no_hook


hooks = Hooks()
_reentrancy = threading.local()


@contextmanager
def transaction(session: Session, op: str) -> Iterator[Session]:
    _run_before_begin(op)
    with session.begin():
        yield session


def _run_before_begin(op: str) -> None:
    if getattr(_reentrancy, "active", False):
        return
    _reentrancy.active = True
    try:
        hooks.before_begin(op)
    finally:
        _reentrancy.active = False
