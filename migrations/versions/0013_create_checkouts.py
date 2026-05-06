"""create checkouts table

Revision ID: 0013_create_checkouts
Revises: 0012_add_stock_movements_po_id_fk
Create Date: 2026-05-07

C1 lays down the schema. Routes (C2 check-out, C3 check-in, C4 manager
"who has what / overdue") arrive in subsequent slices. The columns mirror
MISSION §6 exactly. ``user_id`` is FK SET NULL so a user soft-delete does
not cascade through historical checkouts; ``item_id`` and ``item_unit_id``
are RESTRICT so a checkout row blocks any future hard-delete path.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0013_create_checkouts"
down_revision = "0012_add_stock_movements_po_id_fk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "checkouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "item_unit_id",
            sa.Integer(),
            sa.ForeignKey("item_units.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("checked_out_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_return", sa.DateTime(timezone=True), nullable=True),
        sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("condition_note", sa.String(length=2000), nullable=True),
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
    )
    op.create_index("ix_checkouts_item_id", "checkouts", ["item_id"])
    op.create_index("ix_checkouts_item_unit_id", "checkouts", ["item_unit_id"])
    op.create_index("ix_checkouts_user_id", "checkouts", ["user_id"])
    # ``returned_at`` lets C4's "currently out" / "overdue" queries hit an
    # index. The B-tree handles NULL values; a partial index on NULL would be
    # tighter but adds Postgres / SQLite branching for marginal gain at v1
    # scale.
    op.create_index("ix_checkouts_returned_at", "checkouts", ["returned_at"])


def downgrade() -> None:
    op.drop_index("ix_checkouts_returned_at", table_name="checkouts")
    op.drop_index("ix_checkouts_user_id", table_name="checkouts")
    op.drop_index("ix_checkouts_item_unit_id", table_name="checkouts")
    op.drop_index("ix_checkouts_item_id", table_name="checkouts")
    op.drop_table("checkouts")
