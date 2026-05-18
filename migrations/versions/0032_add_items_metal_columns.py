"""add metal columns to items

Revision ID: 0032_add_items_metal_columns
Revises: 0031_create_metal_spot_prices
Create Date: 2026-05-15

Final S2 migration: adds the metal master FKs and the derived pure-weight
field to ``items``. Both metal FKs are nullable RESTRICT — bulk consumables
and unique tools without a metal carry NULL on both. ``metal_id`` is the
primary alloy; ``secondary_metal_id`` covers two-tone pieces.
``pure_metal_weight_g`` is the cached product of ``weight_grams ×
metal.purity_pct`` so the precious-metal accounting report can render
without joining.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0032_add_items_metal_columns"
down_revision = "0031_create_metal_spot_prices"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite cannot ALTER TABLE … ADD CONSTRAINT for the FKs — same posture
    # as the earlier batch_alter_table migrations (0019, 0029).
    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(sa.Column("metal_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("secondary_metal_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("pure_metal_weight_g", sa.Numeric(14, 4), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_items_metal_id",
            "metal_master",
            ["metal_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_items_secondary_metal_id",
            "metal_master",
            ["secondary_metal_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index("ix_items_metal_id", ["metal_id"])
        batch_op.create_index(
            "ix_items_secondary_metal_id", ["secondary_metal_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_index("ix_items_secondary_metal_id")
        batch_op.drop_index("ix_items_metal_id")
        batch_op.drop_constraint(
            "fk_items_secondary_metal_id", type_="foreignkey"
        )
        batch_op.drop_constraint("fk_items_metal_id", type_="foreignkey")
        batch_op.drop_column("pure_metal_weight_g")
        batch_op.drop_column("secondary_metal_id")
        batch_op.drop_column("metal_id")
