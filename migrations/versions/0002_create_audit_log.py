"""create audit_log table + immutability triggers

Revision ID: 0002_create_audit_log
Revises: 0001_create_users
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from app.audit import apply_immutability_triggers

# revision identifiers, used by Alembic.
revision = "0002_create_audit_log"
down_revision = "0001_create_users"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "actor_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL", name="fk_audit_log_actor_id"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("before_json", sa.JSON(), nullable=True),
        sa.Column("after_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_audit_log_entity",
        "audit_log",
        ["entity_type", "entity_id"],
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])

    apply_immutability_triggers(op.get_bind())


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS audit_log_block_update")
        op.execute("DROP TRIGGER IF EXISTS audit_log_block_delete")
    elif bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_log_block_update ON audit_log")
        op.execute("DROP TRIGGER IF EXISTS audit_log_block_delete ON audit_log")
        op.execute("DROP FUNCTION IF EXISTS audit_log_block_modify()")

    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
