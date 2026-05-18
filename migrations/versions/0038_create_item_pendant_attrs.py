"""create item_pendant_attrs side table

Revision ID: 0038_create_item_pendant_attrs
Revises: 0037_create_item_chain_attrs
Create Date: 2026-05-15

S4 of the architectural additions spec. Pendant attributes: geometry,
bail, optional default chain pairing. ``default_chain_item_id`` is RESTRICT
— archiving the recommended chain shouldn't break the pendant's pairing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0038_create_item_pendant_attrs"
down_revision = "0037_create_item_chain_attrs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_pendant_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("length_mm", sa.Numeric(8, 2), nullable=True),
        sa.Column("width_mm", sa.Numeric(8, 2), nullable=True),
        sa.Column("bail_type", sa.String(length=16), nullable=True),
        sa.Column("bail_opening_mm", sa.Numeric(6, 2), nullable=True),
        sa.Column("includes_chain", sa.Boolean(), nullable=True),
        sa.Column(
            "default_chain_item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
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
        "ix_item_pendant_attrs_default_chain",
        "item_pendant_attrs",
        ["default_chain_item_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_item_pendant_attrs_default_chain",
        table_name="item_pendant_attrs",
    )
    op.drop_table("item_pendant_attrs")
