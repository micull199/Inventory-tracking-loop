"""create item_engagement_attrs side table

Revision ID: 0034_create_item_engagement_attrs
Revises: 0033_create_item_ring_attrs
Create Date: 2026-05-15

S4 of the architectural additions spec. Engagement-ring-specific attributes
(setting style, prongs, gallery, mount price). The
``pairs_with_wedding_band_item_id`` FK is RESTRICT — archiving a matched
band shouldn't break the linkage.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0034_create_item_engagement_attrs"
down_revision = "0033_create_item_ring_attrs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_engagement_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("setting_style", sa.String(length=16), nullable=True),
        sa.Column("setting_variation", sa.String(length=64), nullable=True),
        sa.Column("prong_count", sa.Integer(), nullable=True),
        sa.Column("prong_style", sa.String(length=16), nullable=True),
        sa.Column("gallery_style", sa.String(length=16), nullable=True),
        sa.Column("under_bezel", sa.Boolean(), nullable=True),
        sa.Column(
            "pairs_with_wedding_band_item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("mount_price", sa.Numeric(14, 4), nullable=True),
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
        "ix_item_engagement_attrs_pairs_with_wedding_band",
        "item_engagement_attrs",
        ["pairs_with_wedding_band_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_item_engagement_attrs_pairs_with_wedding_band",
        table_name="item_engagement_attrs",
    )
    op.drop_table("item_engagement_attrs")
