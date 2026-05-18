"""create item_earring_attrs side table

Revision ID: 0036_create_item_earring_attrs
Revises: 0035_create_item_band_attrs
Create Date: 2026-05-15

S4 of the architectural additions spec. Earring attributes: pair-or-single,
closure, style, drop length, hoop diameter. ``hoop_diameter_mm`` only
applies to hoops / huggies; ``drop_length_mm`` to drops / chandeliers /
threaders — consistency enforced at the route layer in a future slice.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0036_create_item_earring_attrs"
down_revision = "0035_create_item_band_attrs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_earring_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sold_as", sa.String(length=16), nullable=True),
        sa.Column("closure_type", sa.String(length=16), nullable=True),
        sa.Column("style", sa.String(length=16), nullable=True),
        sa.Column("drop_length_mm", sa.Numeric(6, 2), nullable=True),
        sa.Column("hoop_diameter_mm", sa.Numeric(6, 2), nullable=True),
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
    op.drop_table("item_earring_attrs")
