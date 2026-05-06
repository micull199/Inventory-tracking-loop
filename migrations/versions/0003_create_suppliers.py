"""create suppliers table

Revision ID: 0003_create_suppliers
Revises: 0002_create_audit_log
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0003_create_suppliers"
down_revision = "0002_create_audit_log"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=64), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("name", name="uq_suppliers_name"),
    )
    op.create_index("ix_suppliers_archived_at", "suppliers", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_suppliers_archived_at", table_name="suppliers")
    op.drop_table("suppliers")
