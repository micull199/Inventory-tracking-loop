"""create item_band_attrs side table

Revision ID: 0035_create_item_band_attrs
Revises: 0034_create_item_engagement_attrs
Create Date: 2026-05-15

S4 of the architectural additions spec. Wedding-band / dress-band
attributes (set style, paired engagement, optional grouping code).
``pairs_with_engagement_item_id`` is the inverse of
``item_engagement_attrs.pairs_with_wedding_band_item_id`` — the route
layer keeps both in sync when registering a his/hers set.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0035_create_item_band_attrs"
down_revision = "0034_create_item_engagement_attrs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_band_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("band_set_style", sa.String(length=16), nullable=True),
        sa.Column(
            "pairs_with_engagement_item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("matching_set_id", sa.String(length=32), nullable=True),
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
    op.create_index(
        "ix_item_band_attrs_pairs_with_engagement",
        "item_band_attrs",
        ["pairs_with_engagement_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_item_band_attrs_pairs_with_engagement",
        table_name="item_band_attrs",
    )
    op.drop_table("item_band_attrs")
