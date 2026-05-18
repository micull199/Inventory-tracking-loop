"""create item_ring_attrs side table

Revision ID: 0033_create_item_ring_attrs
Revises: 0032_add_items_metal_columns
Create Date: 2026-05-15

S4 of the architectural additions spec. First of the per-category side
tables: ring size + standard, band geometry, profile, finish, shank style.

``item_id`` is the PK *and* a CASCADE FK — deleting an item drops the side
row automatically. Every attribute column is nullable so a partially-
described ring is still valid; the catalog dispatcher (storage=SIDE_TABLE,
follow-up slice) lazy-creates the row on first non-NULL write and deletes
it when every value goes back to NULL.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0033_create_item_ring_attrs"
down_revision = "0032_add_items_metal_columns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_ring_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("ring_size", sa.Numeric(6, 2), nullable=True),
        sa.Column("size_standard", sa.String(length=16), nullable=True),
        sa.Column("resize_tolerance_low", sa.Numeric(6, 2), nullable=True),
        sa.Column("resize_tolerance_high", sa.Numeric(6, 2), nullable=True),
        sa.Column("band_width_mm", sa.Numeric(6, 2), nullable=True),
        sa.Column("band_depth_mm", sa.Numeric(6, 2), nullable=True),
        sa.Column("profile", sa.String(length=16), nullable=True),
        sa.Column("finish", sa.String(length=16), nullable=True),
        sa.Column("comfort_fit", sa.Boolean(), nullable=True),
        sa.Column("shank_style", sa.String(length=16), nullable=True),
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


def downgrade() -> None:
    op.drop_table("item_ring_attrs")
