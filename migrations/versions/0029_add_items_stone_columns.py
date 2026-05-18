"""add stone-related columns to items

Revision ID: 0029_add_items_stone_columns
Revises: 0028_create_item_stones
Create Date: 2026-05-15

S1 of the architectural additions spec. Final S1 migration: adds the new
stone-related columns to ``items``. Order matters — ``stones`` and
``item_stones`` must exist first so ``centre_stone_id`` can FK into stones.

The legacy freetext ``items.stone_shape`` (String(64)) survives; new tracked
stones link via ``items.centre_stone_id`` → ``stones.shape_id``. The
freetext column is deprecated but kept until usage drops to zero — same
posture used for the legacy ``item_field_values`` table prior to 0024.

``total_carat_weight`` is derived: sum of tracked stones in this item +
``melee_total_ct``. ``melee_count`` and ``melee_total_ct`` are NOT NULL with
a default of 0 so the column is meaningful on every row going forward.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0029_add_items_stone_columns"
down_revision = "0028_create_item_stones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot ``ALTER TABLE … ADD CONSTRAINT`` for the new FK to
    # ``stones``. ``batch_alter_table`` recreates the table and copies the
    # data; on Postgres it falls through to a direct ALTER. Same posture as
    # ``0012_add_stock_movements_po_id_fk`` and ``0019_transfer_orders``.
    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(
            sa.Column("centre_stone_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("total_carat_weight", sa.Numeric(10, 4), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "melee_count",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column(
                "melee_total_ct",
                sa.Numeric(10, 4),
                nullable=False,
                server_default=sa.text("0"),
            )
        )
        batch_op.add_column(
            sa.Column("melee_stone_type", sa.String(length=32), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_items_centre_stone_id",
            "stones",
            ["centre_stone_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_items_centre_stone_id",
            ["centre_stone_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_index("ix_items_centre_stone_id")
        batch_op.drop_constraint(
            "fk_items_centre_stone_id", type_="foreignkey"
        )
        batch_op.drop_column("melee_stone_type")
        batch_op.drop_column("melee_total_ct")
        batch_op.drop_column("melee_count")
        batch_op.drop_column("total_carat_weight")
        batch_op.drop_column("centre_stone_id")
