"""create item_engraving_attrs side table

Revision ID: 0039_create_item_engraving_attrs
Revises: 0038_create_item_pendant_attrs
Create Date: 2026-05-15

S4 of the architectural additions spec. Engraving attributes — orthogonal
to category (offered on rings, bands, pendants, ...). ``engraving_available``
is NOT NULL with default False so the column is always meaningful — an
explicit "no" for items that don't offer engraving. Max-chars columns
remain nullable because they're optional even on engraving-available items.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0039_create_item_engraving_attrs"
down_revision = "0038_create_item_pendant_attrs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_engraving_attrs",
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "engraving_available",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("max_chars_outside", sa.Integer(), nullable=True),
        sa.Column("max_chars_inside", sa.Integer(), nullable=True),
        sa.Column("engraving_text", sa.String(length=255), nullable=True),
        sa.Column("engraving_font", sa.String(length=64), nullable=True),
        sa.Column("engraving_style", sa.String(length=16), nullable=True),
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
    op.drop_table("item_engraving_attrs")
