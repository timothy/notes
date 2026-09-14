"""Notes: create and read, with the building blocks the mutations share (lock, visibility, versions, tags).

Every mutation locks the note row first (``SELECT ... FOR UPDATE``; SQLite serializes writers with
``BEGIN IMMEDIATE``), resolves the caller's access, and only then looks at preconditions and lifecycle, so
a caller who may not see the note learns nothing (``404``) and a reader who may not act gets ``403``
before any version or conflict detail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session

from notes_api import cursors, etags
from notes_api.clock import Clock
from notes_api.contract import FieldError
from notes_api.http.problems import Conflict, Forbidden, NotFound, PreconditionFailed, ValidationFailed
from notes_api.models import Membership, Note, NoteOwner, NoteTag, Share, User
from notes_api.services import permissions
from notes_api.services.permissions import Access

SELF_MERGE = "self_merge"
TRASH_RETENTION = timedelta(hours=720)  # "exactly 30 x 24 hours"
NOTES_COLLECTION = "notes"


@dataclass(frozen=True, slots=True)
class NoteView:
    """A note as one caller sees it: the row, its owners and tags by position, and the caller's access."""

    note: Note
    owner_ids: list[uuid.UUID]
    tags: list[str]
    access: Access

    @property
    def etag(self) -> str:
        return etags.quote(self.note.version)


def create(
    session: Session, *, author: User, title: str, body: str, tags: list[str], clock: Clock
) -> NoteView:
    now = clock.now()
    note = Note(
        id=uuid.uuid4(),
        author_id=author.id,
        title=title,
        body=body,
        title_fold=title.casefold(),
        body_fold=body.casefold(),
        review_mode=SELF_MERGE,
        review_required_approvals=None,
        created_at=now,
        updated_at=now,
        deleted_at=None,
        expires_at=None,
        version=etags.new_version(),
    )
    session.add(note)
    session.flush()  # the owner and tag rows reference it; nothing orders the inserts otherwise
    session.add(NoteOwner(note_id=note.id, user_id=author.id, position=0, added_at=now))
    session.add_all(_tag_rows(note.id, tags))
    session.flush()
    return NoteView(note, [author.id], list(tags), Access.owner())


def read(session: Session, *, caller: User, note_id: uuid.UUID, now: datetime) -> NoteView:
    """The note as the caller sees it; ``404`` when missing, hidden, trashed for a reader, or expired."""
    return view_of(session, session.get(Note, note_id), caller, now)


def lock(
    session: Session, *, caller: User, note_id: uuid.UUID, now: datetime, lock_access: bool = False
) -> NoteView:
    """The note locked for the rest of the transaction, with the same ``404`` rule as ``read``.

    ``lock_access=True`` also locks the share and membership rows the caller's access came from (``FOR
    SHARE`` on PostgreSQL), for child mutations that a revocation must not race past; owners need no such
    rows, and the note lock itself serializes ownership changes.
    """
    statement = select(Note).where(Note.id == note_id).with_for_update()
    note = session.execute(statement).scalar_one_or_none()
    return view_of(session, note, caller, now, lock_access=lock_access)


def require_owner(view: NoteView) -> None:
    if not view.access.is_owner:
        raise Forbidden()


def require_version(view: NoteView, if_match: str) -> None:
    if if_match != view.etag:
        raise PreconditionFailed()


def update(
    session: Session,
    *,
    view: NoteView,
    title: str | None,
    body: str | None,
    tags: list[str] | None,
    clock: Clock,
) -> NoteView:
    """Apply a partial update to a note locked by ``lock`` whose owner and version were already checked.

    A trashed note is ``409 note_not_active``. A protected note (more than one owner, counted under the
    lock) refuses ``title`` and ``body`` whole with ``409 direct_edit_not_allowed``; tags may still change.
    An effective change takes a new version and ``updated_at``; a no-op keeps both.
    """
    note = view.note
    if permissions.is_trashed(note):
        raise Conflict("note_not_active")
    if (title is not None or body is not None) and len(view.owner_ids) > 1:
        raise Conflict("direct_edit_not_allowed")
    changed = False
    if title is not None and title != note.title:
        note.title, note.title_fold, changed = title, title.casefold(), True
    if body is not None and body != note.body:
        note.body, note.body_fold, changed = body, body.casefold(), True
    if tags is not None and tags != view.tags:
        replace_tags(session, note.id, tags)
        changed = True
    if changed:
        note.version = etags.new_version()
        note.updated_at = clock.now()
        session.flush()
    return NoteView(note, view.owner_ids, list(tags) if tags is not None else view.tags, view.access)


def trash(session: Session, *, view: NoteView, clock: Clock) -> NoteView:
    """Move a note whose owner and version were already checked to the trash.

    Every share goes (owners stay), ``deleted_at`` and ``expires_at`` are set together, and the version
    advances. A note that is already in the trash is left exactly as it is, so a repeated request with the
    trash ETag is idempotent and never extends the recovery period.
    """
    note = view.note
    if permissions.is_trashed(note):
        return view
    now = clock.now()
    session.execute(delete(Share).where(Share.note_id == note.id))
    note.deleted_at = now
    note.expires_at = now + TRASH_RETENTION
    note.version = etags.new_version()
    note.updated_at = now
    session.flush()
    return NoteView(note, view.owner_ids, view.tags, view.access)


def restore(session: Session, *, view: NoteView, clock: Clock) -> NoteView:
    """Bring a trashed note back, private to its owners; an active note is ``409 note_already_active``."""
    note = view.note
    if not permissions.is_trashed(note):
        raise Conflict("note_already_active")
    note.deleted_at = None
    note.expires_at = None
    note.version = etags.new_version()
    note.updated_at = clock.now()
    session.flush()
    return NoteView(note, view.owner_ids, view.tags, view.access)


@dataclass(frozen=True, slots=True)
class ListFilters:
    """The ``GET /notes`` filters, already parsed; ``tags`` keep their order for the fingerprint's sake."""

    scope: str = "all"
    state: str = "active"
    q: str | None = None
    tags: tuple[str, ...] = ()
    team_id: uuid.UUID | None = None


def list_notes(
    session: Session, *, caller: User, filters: ListFilters, limit: int, cursor: str | None, now: datetime
) -> cursors.Page[NoteView]:
    """One page of the notes the caller may read, each once, newest first.

    Access is expressed as ``EXISTS`` predicates (owner row, direct share, team share through a current
    membership), so a note shared along several paths appears once. ``state=trashed`` is the caller's own
    unexpired trash; ``scope=shared`` excludes owned notes; ``q`` is a folded literal substring over title or
    body; each ``tag`` must be present; ``teamId`` requires a share to that team and grants nothing.
    """
    _check_filters(filters)
    owner_row = select(NoteOwner.note_id).where(NoteOwner.note_id == Note.id, NoteOwner.user_id == caller.id)
    direct_share = select(Share.id).where(
        Share.note_id == Note.id, Share.recipient_type == permissions.USER, Share.recipient_id == caller.id
    )
    team_share = (
        select(Share.id)
        .join(Membership, Membership.team_id == Share.recipient_id)
        .where(
            Share.note_id == Note.id,
            Share.recipient_type == permissions.TEAM,
            Membership.user_id == caller.id,
        )
    )
    owned, readable = owner_row.exists(), or_(owner_row.exists(), direct_share.exists(), team_share.exists())

    statement = select(Note)
    if filters.state == "trashed":
        statement = statement.where(owned, Note.deleted_at.is_not(None), Note.expires_at > now)
    else:
        statement = statement.where(Note.deleted_at.is_(None))
    if filters.scope == "mine":
        statement = statement.where(owned)
    elif filters.scope == "shared":
        statement = statement.where(~owned, readable)
    else:
        statement = statement.where(readable)
    if filters.q is not None:
        folded = filters.q.casefold()
        statement = statement.where(
            or_(
                Note.title_fold.contains(folded, autoescape=True),
                Note.body_fold.contains(folded, autoescape=True),
            )
        )
    for tag in filters.tags:
        statement = statement.where(
            select(NoteTag.tag).where(NoteTag.note_id == Note.id, NoteTag.tag == tag).exists()
        )
    if filters.team_id is not None:
        to_team = select(Share.id).where(
            Share.note_id == Note.id,
            Share.recipient_type == permissions.TEAM,
            Share.recipient_id == filters.team_id,
        )
        statement = statement.where(to_team.exists())

    view = {
        "scope": filters.scope,
        "state": filters.state,
        "q": filters.q,
        "tag": sorted(filters.tags),
        "teamId": str(filters.team_id) if filters.team_id is not None else None,
    }
    page = cursors.paginate(
        session,
        statement,
        moment=Note.created_at,
        id_column=Note.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        fingerprint=cursors.fingerprint(caller.id, NOTES_COLLECTION, view, limit),
    )
    return cursors.Page(views_for(session, page.items, caller), page.next_cursor)


def views_for(session: Session, rows: list[Note], caller: User) -> list[NoteView]:
    """Owners, tags, and the caller's access for a page of notes in five queries, never one per note."""
    if not rows:
        return []
    ids = [row.id for row in rows]
    owners: dict[uuid.UUID, list[uuid.UUID]] = {note_id: [] for note_id in ids}
    for note_id, user_id in session.execute(
        select(NoteOwner.note_id, NoteOwner.user_id)
        .where(NoteOwner.note_id.in_(ids))
        .order_by(NoteOwner.position)
    ):
        owners[note_id].append(user_id)
    tags: dict[uuid.UUID, list[str]] = {note_id: [] for note_id in ids}
    for note_id, tag in session.execute(
        select(NoteTag.note_id, NoteTag.tag).where(NoteTag.note_id.in_(ids)).order_by(NoteTag.position)
    ):
        tags[note_id].append(tag)
    owned = set(
        session.execute(
            select(NoteOwner.note_id).where(NoteOwner.note_id.in_(ids), NoteOwner.user_id == caller.id)
        ).scalars()
    )
    granted: dict[uuid.UUID, list[Share]] = {note_id: [] for note_id in ids}
    direct = select(Share).where(
        Share.note_id.in_(ids), Share.recipient_type == permissions.USER, Share.recipient_id == caller.id
    )
    through_teams = (
        select(Share)
        .join(Membership, Membership.team_id == Share.recipient_id)
        .where(
            Share.note_id.in_(ids), Share.recipient_type == permissions.TEAM, Membership.user_id == caller.id
        )
    )
    for share in [*session.execute(direct).scalars(), *session.execute(through_teams).scalars()]:
        granted[share.note_id].append(share)
    return [
        NoteView(
            row,
            owners[row.id],
            tags[row.id],
            Access.owner() if row.id in owned else Access.from_shares(granted[row.id]),
        )
        for row in rows
    ]


def _check_filters(filters: ListFilters) -> None:
    """The rules FastAPI's parameter validation cannot express: no duplicate tags and no NUL anywhere."""
    errors: list[FieldError] = []
    if len(set(filters.tags)) != len(filters.tags):
        errors.append(FieldError("query", "tag", "must not contain duplicate items"))
    if any("\x00" in tag for tag in filters.tags):
        errors.append(FieldError("query", "tag", "must not contain NUL characters"))
    if filters.q is not None and "\x00" in filters.q:
        errors.append(FieldError("query", "q", "must not contain NUL characters"))
    if errors:
        raise ValidationFailed(errors, detail="A query parameter is invalid.")


def owner_ids(session: Session, note_id: uuid.UUID) -> list[uuid.UUID]:
    statement = select(NoteOwner.user_id).where(NoteOwner.note_id == note_id).order_by(NoteOwner.position)
    return list(session.execute(statement).scalars())


def tags_of(session: Session, note_id: uuid.UUID) -> list[str]:
    statement = select(NoteTag.tag).where(NoteTag.note_id == note_id).order_by(NoteTag.position)
    return list(session.execute(statement).scalars())


def replace_tags(session: Session, note_id: uuid.UUID, tags: list[str]) -> None:
    """Replace the tag rows wholesale.

    The Core delete runs first and immediately; adding the new rows before it would autoflush them into
    the old positions, and deleting the old rows through the ORM in the same flush would collide on
    ``UNIQUE(note_id, tag)`` through row-switch updates.
    """
    session.execute(delete(NoteTag).where(NoteTag.note_id == note_id))
    session.add_all(_tag_rows(note_id, tags))
    session.flush()


def _tag_rows(note_id: uuid.UUID, tags: list[str]) -> list[NoteTag]:
    return [NoteTag(note_id=note_id, position=position, tag=tag) for position, tag in enumerate(tags)]


def view_of(
    session: Session, note: Note | None, caller: User, now: datetime, *, lock_access: bool = False
) -> NoteView:
    """The note as ``caller`` sees it; ``404`` when there is no such note or it is invisible to them.

    Expired notes are invisible to everyone and trashed notes to non-owners. Services that already hold a
    ``Note`` row (a request read together with its note) build their view here instead of re-reading it.
    """
    if note is None:
        raise NotFound()
    access = permissions.resolve(session, note.id, caller.id, lock=lock_access)
    if not permissions.visible(note, access, now):
        raise NotFound()
    return NoteView(note, owner_ids(session, note.id), tags_of(session, note.id), access)
