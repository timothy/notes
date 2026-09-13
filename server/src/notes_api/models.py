"""SQLAlchemy models: the contract's core model (design guide, section 1) as tables.

Enumerations are plain strings guarded by CHECK constraints so the schema is identical on SQLite and
PostgreSQL. ``version`` columns hold the opaque token behind the row's current ETag. Foreign keys cascade
so deleting a team removes its memberships and purging a note removes everything that hangs off it;
shares are polymorphic on ``recipient_type`` and are deleted by the services instead.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from notes_api.db import UTCDateTime

VERSION = String(32)


class Base(DeclarativeBase):
    # SQLAlchemy reads this class-level mapping once; it is configuration, not a shared mutable default.
    type_annotation_map = {datetime: UTCDateTime(), uuid.UUID: Uuid()}  # noqa: RUF012


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_users_identity"),
        Index("ix_users_created_at_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    issuer: Mapped[str] = mapped_column(Text)
    subject: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]


class Team(Base):
    __tablename__ = "teams"
    __table_args__ = (Index("ix_teams_created_at_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'member')", name="ck_memberships_role"),
        Index("ix_memberships_user_id", "user_id"),
        Index("ix_memberships_team_joined", "team_id", "joined_at", "user_id"),
    )

    team_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16))
    joined_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Note(Base):
    __tablename__ = "notes"
    __table_args__ = (
        CheckConstraint("review_mode IN ('self_merge', 'peer_approval')", name="ck_notes_review_mode"),
        CheckConstraint(
            "(review_mode = 'self_merge') = (review_required_approvals IS NULL)",
            name="ck_notes_review_policy",
        ),
        CheckConstraint("(deleted_at IS NULL) = (expires_at IS NULL)", name="ck_notes_trash_pair"),
        Index("ix_notes_created_at_id", "created_at", "id"),
        Index("ix_notes_author_id", "author_id"),
        Index("ix_notes_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    title_fold: Mapped[str] = mapped_column(Text)
    body_fold: Mapped[str] = mapped_column(Text)
    review_mode: Mapped[str] = mapped_column(String(16))
    review_required_approvals: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    deleted_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]
    version: Mapped[str] = mapped_column(VERSION)


class NoteOwner(Base):
    __tablename__ = "note_owners"
    __table_args__ = (
        UniqueConstraint("note_id", "position", name="uq_note_owners_position"),
        Index("ix_note_owners_user_id", "user_id"),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime]


class NoteTag(Base):
    __tablename__ = "note_tags"
    __table_args__ = (
        UniqueConstraint("note_id", "tag", name="uq_note_tags_tag"),
        Index("ix_note_tags_tag", "tag"),
    )

    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    position: Mapped[int] = mapped_column(Integer, primary_key=True)
    tag: Mapped[str] = mapped_column(Text)


class Share(Base):
    __tablename__ = "shares"
    __table_args__ = (
        CheckConstraint("recipient_type IN ('user', 'team')", name="ck_shares_recipient_type"),
        UniqueConstraint("note_id", "recipient_type", "recipient_id", name="uq_shares_recipient"),
        Index("ix_shares_recipient", "recipient_type", "recipient_id"),
        Index("ix_shares_note_created", "note_id", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    recipient_type: Mapped[str] = mapped_column(String(8))
    recipient_id: Mapped[uuid.UUID]
    can_comment: Mapped[bool] = mapped_column(Boolean)
    can_propose: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (Index("ix_comments_note_created", "note_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[str] = mapped_column(VERSION)


class EditRequest(Base):
    __tablename__ = "edit_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'merged', 'rejected', 'withdrawn')", name="ck_edit_requests_status"
        ),
        CheckConstraint("(status = 'open') = (closed_at IS NULL)", name="ck_edit_requests_closed"),
        Index("ix_edit_requests_note_status_created", "note_id", "status", "created_at", "id"),
        Index("ix_edit_requests_proposer_status_created", "proposer_id", "status", "created_at", "id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    note_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"))
    proposer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(16))
    base_title: Mapped[str] = mapped_column(Text)
    base_body: Mapped[str] = mapped_column(Text)
    proposed_title: Mapped[str] = mapped_column(Text)
    proposed_body: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    merged_title: Mapped[str | None] = mapped_column(Text)
    merged_body: Mapped[str | None] = mapped_column(Text)
    merged_note_version: Mapped[str | None] = mapped_column(VERSION)
    merged_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    merged_at: Mapped[datetime | None]
    required_approvals_at_close: Mapped[int | None] = mapped_column(Integer)
    version: Mapped[str] = mapped_column(VERSION)


class Approval(Base):
    __tablename__ = "approvals"

    request_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("edit_requests.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    approved_at: Mapped[datetime]


class RequestComment(Base):
    __tablename__ = "request_comments"
    __table_args__ = (Index("ix_request_comments_request_created", "request_id", "created_at", "id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("edit_requests.id", ondelete="CASCADE"))
    author_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime]
    updated_at: Mapped[datetime]
    version: Mapped[str] = mapped_column(VERSION)
