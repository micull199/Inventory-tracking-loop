"""create metal_spot_prices

Revision ID: 0031_create_metal_spot_prices
Revises: 0030_create_metal_master
Create Date: 2026-05-15

S2 of the architectural additions spec. Daily spot per metal — a separate
table because spot prices change daily and the row count grows
append-heavy. v1: manual entry by manager via the admin price route
(future slice). v2 (post-$200M): pulled from a feed.

Unique on ``(metal_id, as_of_date)`` — one price per metal per day.
Duplicate entries are operator error.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0031_create_metal_spot_prices"
down_revision = "0030_create_metal_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "metal_spot_prices",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "metal_id",
            sa.Integer(),
            sa.ForeignKey("metal_master.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("as_of_date", sa.Date(), nullable=False),
        sa.Column("price_per_gram", sa.Numeric(14, 6), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "uq_metal_spot_prices_date",
        "metal_spot_prices",
        ["metal_id", "as_of_date"],
        unique=True,
    )
    op.create_index(
        "ix_metal_spot_prices_metal_id", "metal_spot_prices", ["metal_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_metal_spot_prices_metal_id", table_name="metal_spot_prices"
    )
    op.drop_index(
        "uq_metal_spot_prices_date", table_name="metal_spot_prices"
    )
    op.drop_table("metal_spot_prices")
