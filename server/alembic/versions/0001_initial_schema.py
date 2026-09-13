"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-09-13 09:54:42.124514
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "teams",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.create_index("ix_teams_created_at_id", ["created_at", "id"], unique=False)

    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject", name="uq_users_identity"),
    )
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.create_index("ix_users_created_at_id", ["created_at", "id"], unique=False)

    op.create_table(
        "memberships",
        sa.Column("team_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("role IN ('admin', 'member')", name="ck_memberships_role"),
        sa.ForeignKeyConstraint(["team_id"], ["teams.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("team_id", "user_id"),
    )
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.create_index("ix_memberships_team_joined", ["team_id", "joined_at", "user_id"], unique=False)
        batch_op.create_index("ix_memberships_user_id", ["user_id"], unique=False)

    op.create_table(
        "notes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("title_fold", sa.Text(), nullable=False),
        sa.Column("body_fold", sa.Text(), nullable=False),
        sa.Column("review_mode", sa.String(length=16), nullable=False),
        sa.Column("review_required_approvals", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "(review_mode = 'self_merge') = (review_required_approvals IS NULL)",
            name="ck_notes_review_policy",
        ),
        sa.CheckConstraint("review_mode IN ('self_merge', 'peer_approval')", name="ck_notes_review_mode"),
        sa.CheckConstraint("(deleted_at IS NULL) = (expires_at IS NULL)", name="ck_notes_trash_pair"),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("notes", schema=None) as batch_op:
        batch_op.create_index("ix_notes_author_id", ["author_id"], unique=False)
        batch_op.create_index("ix_notes_created_at_id", ["created_at", "id"], unique=False)
        batch_op.create_index("ix_notes_expires_at", ["expires_at"], unique=False)

    op.create_table(
        "comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("comments", schema=None) as batch_op:
        batch_op.create_index("ix_comments_note_created", ["note_id", "created_at", "id"], unique=False)

    op.create_table(
        "edit_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("proposer_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("base_title", sa.Text(), nullable=False),
        sa.Column("base_body", sa.Text(), nullable=False),
        sa.Column("proposed_title", sa.Text(), nullable=False),
        sa.Column("proposed_body", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.Uuid(), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("merged_title", sa.Text(), nullable=True),
        sa.Column("merged_body", sa.Text(), nullable=True),
        sa.Column("merged_note_version", sa.String(length=32), nullable=True),
        sa.Column("merged_by", sa.Uuid(), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("required_approvals_at_close", sa.Integer(), nullable=True),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.CheckConstraint("(status = 'open') = (closed_at IS NULL)", name="ck_edit_requests_closed"),
        sa.CheckConstraint(
            "status IN ('open', 'merged', 'rejected', 'withdrawn')", name="ck_edit_requests_status"
        ),
        sa.ForeignKeyConstraint(
            ["merged_by"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["proposer_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("edit_requests", schema=None) as batch_op:
        batch_op.create_index(
            "ix_edit_requests_note_status_created", ["note_id", "status", "created_at", "id"], unique=False
        )
        batch_op.create_index(
            "ix_edit_requests_proposer_status_created",
            ["proposer_id", "status", "created_at", "id"],
            unique=False,
        )

    op.create_table(
        "note_owners",
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("note_id", "user_id"),
        sa.UniqueConstraint("note_id", "position", name="uq_note_owners_position"),
    )
    with op.batch_alter_table("note_owners", schema=None) as batch_op:
        batch_op.create_index("ix_note_owners_user_id", ["user_id"], unique=False)

    op.create_table(
        "note_tags",
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("tag", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("note_id", "position"),
        sa.UniqueConstraint("note_id", "tag", name="uq_note_tags_tag"),
    )
    with op.batch_alter_table("note_tags", schema=None) as batch_op:
        batch_op.create_index("ix_note_tags_tag", ["tag"], unique=False)

    op.create_table(
        "shares",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_type", sa.String(length=8), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("can_comment", sa.Boolean(), nullable=False),
        sa.Column("can_propose", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("recipient_type IN ('user', 'team')", name="ck_shares_recipient_type"),
        sa.ForeignKeyConstraint(["note_id"], ["notes.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("note_id", "recipient_type", "recipient_id", name="uq_shares_recipient"),
    )
    with op.batch_alter_table("shares", schema=None) as batch_op:
        batch_op.create_index("ix_shares_note_created", ["note_id", "created_at", "id"], unique=False)
        batch_op.create_index("ix_shares_recipient", ["recipient_type", "recipient_id"], unique=False)

    op.create_table(
        "approvals",
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], ["edit_requests.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
        ),
        sa.PrimaryKeyConstraint("request_id", "user_id"),
    )
    op.create_table(
        "request_comments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("author_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["users.id"],
        ),
        sa.ForeignKeyConstraint(["request_id"], ["edit_requests.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("request_comments", schema=None) as batch_op:
        batch_op.create_index(
            "ix_request_comments_request_created", ["request_id", "created_at", "id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("request_comments", schema=None) as batch_op:
        batch_op.drop_index("ix_request_comments_request_created")

    op.drop_table("request_comments")
    op.drop_table("approvals")
    with op.batch_alter_table("shares", schema=None) as batch_op:
        batch_op.drop_index("ix_shares_recipient")
        batch_op.drop_index("ix_shares_note_created")

    op.drop_table("shares")
    with op.batch_alter_table("note_tags", schema=None) as batch_op:
        batch_op.drop_index("ix_note_tags_tag")

    op.drop_table("note_tags")
    with op.batch_alter_table("note_owners", schema=None) as batch_op:
        batch_op.drop_index("ix_note_owners_user_id")

    op.drop_table("note_owners")
    with op.batch_alter_table("edit_requests", schema=None) as batch_op:
        batch_op.drop_index("ix_edit_requests_proposer_status_created")
        batch_op.drop_index("ix_edit_requests_note_status_created")

    op.drop_table("edit_requests")
    with op.batch_alter_table("comments", schema=None) as batch_op:
        batch_op.drop_index("ix_comments_note_created")

    op.drop_table("comments")
    with op.batch_alter_table("notes", schema=None) as batch_op:
        batch_op.drop_index("ix_notes_expires_at")
        batch_op.drop_index("ix_notes_created_at_id")
        batch_op.drop_index("ix_notes_author_id")

    op.drop_table("notes")
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.drop_index("ix_memberships_user_id")
        batch_op.drop_index("ix_memberships_team_joined")

    op.drop_table("memberships")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_index("ix_users_created_at_id")

    op.drop_table("users")
    with op.batch_alter_table("teams", schema=None) as batch_op:
        batch_op.drop_index("ix_teams_created_at_id")

    op.drop_table("teams")
