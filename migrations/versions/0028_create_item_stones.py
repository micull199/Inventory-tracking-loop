"""create item_stones linkage table

Revision ID: 0028_create_item_stones
Revises: 0027_create_stone_events
Create Date: 2026-05-15

S1 of the architectural additions spec. Many-stones-per-item with position
semantics. Soft-end pattern via ``unset_at`` keeps a historical record of
every stone that's ever been set into an item — replacing a centre stone
fills the prior row's ``unset_at`` and inserts a new row.

Partial unique indexes guard the active set:
- ``uq_item_stones_active_stone``: a stone can be set in at most one item
  at a time (only the active row, ``unset_at IS NULL``, is considered).
- ``uq_item_stones_position``: only one stone occupies a given slot
  (``item_id`` + ``position`` + ``position_index``) at a time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0028_create_item_stones"
down_revision = "0027_create_stone_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_stones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "stone_id",
            sa.Integer(),
            sa.ForeignKey("stones.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("position", sa.String(length=16), nullable=False),
        sa.Column(
            "position_index",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "set_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("unset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=500), nullable=True),
    )
    op.create_index("ix_item_stones_item_id", "item_stones", ["item_id"])
    op.create_index("ix_item_stones_stone_id", "item_stones", ["stone_id"])
    op.create_index(
        "uq_item_stones_active_stone",
        "item_stones",
        ["stone_id"],
        unique=True,
        sqlite_where=sa.text("unset_at IS NULL"),
        postgresql_where=sa.text("unset_at IS NULL"),
    )
    op.create_index(
        "uq_item_stones_position",
        "item_stones",
        ["item_id", "position", "position_index"],
        unique=True,
        sqlite_where=sa.text("unset_at IS NULL"),
        postgresql_where=sa.text("unset_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_item_stones_position", table_name="item_stones")
    op.drop_index("uq_item_stones_active_stone", table_name="item_stones")
    op.drop_index("ix_item_stones_stone_id", table_name="item_stones")
    op.drop_index("ix_item_stones_item_id", table_name="item_stones")
    op.drop_table("item_stones")
