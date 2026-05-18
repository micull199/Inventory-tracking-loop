"""create item_chain_attrs side table

Revision ID: 0037_create_item_chain_attrs
Revises: 0036_create_item_earring_attrs
Create Date: 2026-05-15

S4 of the architectural additions spec. Chain / necklace / bracelet
attributes — linear products that share an attribute footprint
(style, length, link width, closure, worn-as). ``min_length_mm`` and
``max_length_mm`` are only populated when ``adjustable=True``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0037_create_item_chain_attrs"
down_revision = "0036_create_item_earring_attrs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_chain_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chain_style", sa.String(length=16), nullable=True),
        sa.Column("length_mm", sa.Numeric(8, 2), nullable=True),
        sa.Column("adjustable", sa.Boolean(), nullable=True),
        sa.Column("min_length_mm", sa.Numeric(8, 2), nullable=True),
        sa.Column("max_length_mm", sa.Numeric(8, 2), nullable=True),
        sa.Column("link_width_mm", sa.Numeric(6, 2), nullable=True),
        sa.Column("closure_type", sa.String(length=16), nullable=True),
        sa.Column("worn_as", sa.String(length=16), nullable=True),
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
    op.drop_table("item_chain_attrs")
