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
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from notes_api import etags
from notes_api.contract import FieldError
from notes_api.http.problems import Conflict, Forbidden, PreconditionFailed, ValidationFailed
from notes_api.merge.three_way import Content
from notes_api.models import Approval, EditRequest, User
from notes_api.services import permissions
from notes_api.services.notes import NoteView

OPEN = "open"
MERGED = "merged"
REJECTED = "rejected"
WITHDRAWN = "withdrawn"
PEER_APPROVAL = "peer_approval"

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
