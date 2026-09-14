"""Edit requests: proposals against an immutable snapshot of a note (design guide, section 3).

A submission captures the note's title and body as the request's base, stores the proposal, and leaves
the note untouched; owners review the server-computed diff and, in slice 9, merge a three-way candidate.
Only the note's owners and the proposer, while they can still read the note, may inspect a request; every
other caller gets ``404``. The effective ``requiredApprovals`` is a live view of the note's owners and
review policy while a request is open and is frozen when it closes.

Locking follows the plan's order, note then request: a request-scoped mutation reads only the request's
``note_id`` as a scalar, locks the note through ``notes.lock`` (which also settles visibility), and only
then loads the request row for the first time with ``FOR UPDATE``. A row loaded before the note lock would
keep stale attributes on PostgreSQL even after a later locking select, so nothing here loads it earlier.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from notes_api import cursors, etags
from notes_api.contract import FieldError
from notes_api.http.problems import Conflict, Forbidden, NotFound, PreconditionFailed, ValidationFailed
from notes_api.merge.three_way import Content
from notes_api.models import Approval, EditRequest, Note, User
from notes_api.services import notes, permissions
from notes_api.services.notes import NoteView

OPEN = "open"
MERGED = "merged"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
PEER_APPROVAL = "peer_approval"
INCOMING = "incoming"
TRASHED = "trashed"
NOTE_COLLECTION = "note_edit_requests"
INBOX_COLLECTION = "edit_requests"

EMPTY_PROPOSAL_DETAIL = "The proposed content is identical to the base snapshot."
EMPTY_PROPOSAL_ERROR = FieldError("body", "/proposedContent", "proposal must differ from the base")
STALE_BASE_DETAIL = (
    "The baseNoteETag you supplied no longer matches the note. Fetch the current note and reconcile "
    "before resubmitting."
)


@dataclass(frozen=True, slots=True)
class RequestView:
    """A request with the note as the caller sees it and the approvals in ``approvedAt, userId`` order."""

    request: EditRequest
    note: NoteView
    approvals: list[Approval]

    @property
    def etag(self) -> str:
        return etags.quote(self.request.version)

    @property
    def is_open(self) -> bool:
        return self.request.status == OPEN

    @property
    def owner_count(self) -> int:
        return len(self.note.owner_ids)

    @property
    def proposer_is_owner(self) -> bool:
        return self.request.proposer_id in self.note.owner_ids

    def required_approvals(self) -> int:
        """Live while open; the value frozen at closing afterwards (a closed row without one, which only
        test seeding can produce, falls back to the live value)."""
        frozen = self.request.required_approvals_at_close
        if not self.is_open and frozen is not None:
            return frozen
        return required_approvals(
            owner_count=self.owner_count,
            review_mode=self.note.note.review_mode,
            policy=self.note.note.review_required_approvals,
            proposer_is_owner=self.proposer_is_owner,
        )


def required_approvals(
    *, owner_count: int, review_mode: str, policy: int | None, proposer_is_owner: bool
) -> int:
    """The guide's formula: ``0`` on a single-owner or ``self_merge`` note; otherwise ``min(N, owners - 1)``
    for an owner's proposal and ``max(2, min(N, owners))`` for anyone else's, so that at least two distinct
    owners take part in every change that lands on a peer-approval note."""
    if owner_count <= 1 or review_mode != PEER_APPROVAL or policy is None:
        return 0
    if proposer_is_owner:
        return min(policy, owner_count - 1)
    return max(2, min(policy, owner_count))


def inspect(
    session: Session, *, caller: User, request_id: uuid.UUID, now: datetime, lock: bool
) -> RequestView:
    """The request as ``caller`` may see it, or ``404``.

    The note's own visibility comes first (expired: nobody; trashed: owners only), then the inspect rule:
    owners and the proposer, while they can still read the note. Other readers of the note get ``404`` like
    strangers. With ``lock`` the note is locked ``FOR UPDATE`` together with the caller's access rows, and
    the request row is loaded for the first time under ``FOR UPDATE``, so a competitor's commit cannot leave
    stale attributes behind (see the module docstring). Without it, one joined select reads the request and
    its note together: the coherent pair of versions a preview reports.
    """
    if lock:
        located = select(EditRequest.note_id).where(EditRequest.id == request_id)
        note_id = session.execute(located).scalar_one_or_none()
        if note_id is None:
            raise NotFound()
        view = notes.lock(session, caller=caller, note_id=note_id, now=now, lock_access=True)
        locked = (
            select(EditRequest)
            .where(EditRequest.id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        request = session.execute(locked).scalar_one_or_none()
        if request is None:
            raise NotFound()
    else:
        paired = select(EditRequest, Note).join(Note, Note.id == EditRequest.note_id)
        pair = session.execute(paired.where(EditRequest.id == request_id)).one_or_none()
        if pair is None:
            raise NotFound()
        request, note = pair[0], pair[1]
        view = notes.view_of(session, note, caller, now)
    if not (view.access.is_owner or request.proposer_id == caller.id):
        raise NotFound()
    return RequestView(request, view, approvals_of(session, request.id))


def approvals_of(session: Session, request_id: uuid.UUID) -> list[Approval]:
    """The request's approvals in the contract's order, ``approvedAt ASC, userId ASC``."""
    statement = (
        select(Approval)
        .where(Approval.request_id == request_id)
        .order_by(Approval.approved_at, Approval.user_id)
    )
    return list(session.execute(statement).scalars())


def list_for_note(
    session: Session, *, caller: User, view: NoteView, status: str, limit: int, cursor: str | None
) -> cursors.Page[RequestView]:
    """One page of the note's requests the caller may inspect, newest first: every request with ``status``
    for an owner, their own for anyone else, so a reader who never proposed gets an empty page."""
    statement = select(EditRequest).where(EditRequest.note_id == view.note.id, EditRequest.status == status)
    if not view.access.is_owner:
        statement = statement.where(EditRequest.proposer_id == caller.id)
    filters = {"noteId": str(view.note.id), "status": status}
    page = _page(
        session, statement, limit, cursor, cursors.fingerprint(caller.id, NOTE_COLLECTION, filters, limit)
    )
    return cursors.Page(views_for(session, page.items, caller, known={view.note.id: view}), page.next_cursor)


def inbox(
    session: Session,
    *,
    caller: User,
    view: str,
    status: str,
    state: str,
    limit: int,
    cursor: str | None,
    now: datetime,
) -> cursors.Page[RequestView]:
    """One page of the caller's inbox, newest first.

    ``incoming`` is every request with ``status`` on a note the caller owns (their own proposals on those
    notes included); ``outgoing`` is what they proposed on notes they can still read. ``state`` follows the
    note: active notes, or the caller's own unexpired trash, which is owners only, so an ``outgoing`` view of
    the trash holds only the requests an owner proposed on their own notes. Expired notes never match.
    """
    owned, readable = permissions.access_predicates(caller.id)
    statement = (
        select(EditRequest).join(Note, Note.id == EditRequest.note_id).where(EditRequest.status == status)
    )
    if state == TRASHED:
        statement = statement.where(Note.deleted_at.is_not(None), Note.expires_at > now, owned)
    else:
        statement = statement.where(Note.deleted_at.is_(None))
    if view == INCOMING:
        statement = statement.where(owned)
    else:
        statement = statement.where(EditRequest.proposer_id == caller.id, readable)
    filters = {"view": view, "status": status, "state": state}
    page = _page(
        session, statement, limit, cursor, cursors.fingerprint(caller.id, INBOX_COLLECTION, filters, limit)
    )
    return cursors.Page(views_for(session, page.items, caller), page.next_cursor)


def _page(
    session: Session, statement: Select[tuple[EditRequest]], limit: int, cursor: str | None, fingerprint: str
) -> cursors.Page[EditRequest]:
    return cursors.paginate(
        session,
        statement,
        moment=EditRequest.created_at,
        id_column=EditRequest.id,
        key_of=lambda row: cursors.Key(row.created_at, row.id),
        limit=limit,
        cursor=cursor,
        fingerprint=fingerprint,
    )


def views_for(
    session: Session,
    rows: list[EditRequest],
    caller: User,
    *,
    known: Mapping[uuid.UUID, NoteView] | None = None,
) -> list[RequestView]:
    """Request views for a page in a bounded number of statements: the notes not already ``known`` and
    their views for the caller through ``notes.views_for``, then every request's approvals, each in one
    batch keyed by the page's ids."""
    if not rows:
        return []
    note_views: dict[uuid.UUID, NoteView] = dict(known or {})
    missing = [note_id for note_id in {row.note_id for row in rows} if note_id not in note_views]
    if missing:
        note_rows = list(session.execute(select(Note).where(Note.id.in_(missing))).scalars())
        note_views.update((view.note.id, view) for view in notes.views_for(session, note_rows, caller))
    approvals: dict[uuid.UUID, list[Approval]] = {row.id: [] for row in rows}
    statement = (
        select(Approval)
        .where(Approval.request_id.in_(list(approvals)))
        .order_by(Approval.approved_at, Approval.user_id)
    )
    for approval in session.execute(statement).scalars():
        approvals[approval.request_id].append(approval)
    return [RequestView(row, note_views[row.note_id], approvals[row.id]) for row in rows]


def create(
    session: Session,
    *,
    view: NoteView,
    proposer: User,
    base_etag: str,
    proposed: Content,
    explanation: str | None,
    now: datetime,
) -> RequestView:
    """Submit a proposal against a note locked by ``notes.lock``.

    The ladder: ``propose_edit`` (``403``), then ``baseNoteETag`` against the live version (``412``, nothing
    written), then the lifecycle (``409 note_not_active``), then a proposal identical to the base
    (``422`` at ``/proposedContent``). The base is copied from the locked row, never from the client.
    """
    if not view.access.propose:
        raise Forbidden()
    if base_etag != view.etag:
        raise PreconditionFailed(detail=STALE_BASE_DETAIL)
    note = view.note
    if permissions.is_trashed(note):
        raise Conflict("note_not_active")
    if (proposed.title, proposed.body) == (note.title, note.body):
        raise ValidationFailed([EMPTY_PROPOSAL_ERROR], detail=EMPTY_PROPOSAL_DETAIL)
    request = EditRequest(
        id=uuid.uuid4(),
        note_id=note.id,
        proposer_id=proposer.id,
        status=OPEN,
        base_title=note.title,
        base_body=note.body,
        proposed_title=proposed.title,
        proposed_body=proposed.body,
        explanation=explanation,
        created_at=now,
        updated_at=now,
        closed_at=None,
        rejected_by=None,
        rejection_reason=None,
        merged_title=None,
        merged_body=None,
        merged_note_version=None,
        merged_by=None,
        merged_at=None,
        required_approvals_at_close=None,
        version=etags.new_version(),
    )
    session.add(request)
    session.flush()
    return RequestView(request, view, [])


# -- transitions ----------------------------------------------------------------------------------------


def require_proposer(rv: RequestView, caller: User) -> None:
    """Withdraw is the proposer's alone; an owner who did not propose is ``403``."""
    if rv.request.proposer_id != caller.id:
        raise Forbidden()


def require_proposer_with_propose(rv: RequestView, caller: User) -> None:
    """Revise is the proposer's, while they hold current ``propose_edit`` (owners always do)."""
    if rv.request.proposer_id != caller.id or not rv.note.access.propose:
        raise Forbidden()


def require_version(rv: RequestView, if_match: str) -> None:
    if if_match != rv.etag:
        raise PreconditionFailed()


def require_open_and_active(rv: RequestView) -> None:
    """A closed record is immutable whatever happens to its note, so ``request_not_open`` comes before the
    transient ``note_not_active``."""
    if not rv.is_open:
        raise Conflict("request_not_open")
    if permissions.is_trashed(rv.note.note):
        raise Conflict("note_not_active")


def revise(
    session: Session,
    *,
    rv: RequestView,
    proposed: Content | None,
    explanation: str | None,
    explanation_given: bool,
    now: datetime,
) -> RequestView:
    """Revise an open request whose proposer and version were already checked.

    The base never changes. A proposal equal to it is ``422``. A change to the stored proposal writes both
    fields, deletes every approval, and takes a new version; an explanation change alone (``null`` clears,
    ``""`` is stored) keeps the approvals and takes a new version; no effective change returns the request
    as it is. Re-sending the current proposal with a new explanation is an explanation-only revision.
    """
    require_open_and_active(rv)
    request = rv.request
    if proposed is not None and (proposed.title, proposed.body) == (request.base_title, request.base_body):
        raise ValidationFailed([EMPTY_PROPOSAL_ERROR], detail=EMPTY_PROPOSAL_DETAIL)
    content_changed = proposed is not None and (proposed.title, proposed.body) != (
        request.proposed_title,
        request.proposed_body,
    )
    explanation_changed = explanation_given and explanation != request.explanation
    if not content_changed and not explanation_changed:
        return rv
    approvals = rv.approvals
    if proposed is not None and content_changed:
        request.proposed_title, request.proposed_body = proposed.title, proposed.body
        session.execute(delete(Approval).where(Approval.request_id == request.id))
        approvals = []
    if explanation_changed:
        request.explanation = explanation
    request.version = etags.new_version()
    request.updated_at = now
    session.flush()
    return RequestView(request, rv.note, approvals)


def withdraw(session: Session, *, rv: RequestView, now: datetime) -> RequestView:
    """Close an open request as ``withdrawn``; the note is untouched."""
    require_open_and_active(rv)
    return _close(session, rv, WITHDRAWN, now)


def reject(
    session: Session, *, rv: RequestView, rejecter: User, reason: str | None, now: datetime
) -> RequestView:
    """Close an open request as ``rejected``, recording who did it and, optionally, why."""
    require_open_and_active(rv)
    rv.request.rejected_by = rejecter.id
    rv.request.rejection_reason = reason
    return _close(session, rv, REJECTED, now)


def _close(session: Session, rv: RequestView, status: str, now: datetime) -> RequestView:
    """Every close freezes ``requiredApprovals`` as it stood, sets ``closed_at``, and takes a new version, so
    an old ETag is ``412`` afterwards and the current one meets ``409 request_not_open``."""
    frozen = rv.required_approvals()  # while the request is still open, so the live value
    request = rv.request
    request.status = status
    request.closed_at = now
    request.required_approvals_at_close = frozen
    request.version = etags.new_version()
    request.updated_at = now
    session.flush()
    return RequestView(request, rv.note, rv.approvals)
