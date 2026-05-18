"""create stone_shape_master lookup + seed canonical shapes

Revision ID: 0025_create_stone_shape_master
Revises: 0024_promote_standard_fields
Create Date: 2026-05-15

S1 of the architectural additions spec. The first dependency of ``stones``:
a tiny administered lookup that replaces freetext shape on tracked stones.

Seed values match the spec exactly so the dev / prod DB ships with the
canonical 13 shapes available on day one. Same archive-doesn't-free-the-name
convention as ``suppliers`` / ``locations``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0025_create_stone_shape_master"
down_revision = "0024_promote_standard_fields"
branch_labels = None
depends_on = None


_SEED_SHAPES: tuple[str, ...] = (
    "round",
    "oval",
    "cushion",
    "emerald",
    "pear",
    "radiant",
    "marquise",
    "princess",
    "asscher",
    "heart",
    "trillion",
    "baguette",
    "other",
)


def upgrade() -> None:
    op.create_table(
        "stone_shape_master",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
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
        "uq_stone_shape_master_name",
        "stone_shape_master",
        ["name"],
        unique=True,
    )
    op.create_index(
        "ix_stone_shape_master_archived_at",
        "stone_shape_master",
        ["archived_at"],
    )

    # Seed canonical shapes. ``sort_order`` follows insertion order so the
    # admin list renders in spec order on first paint.
    rows = [{"name": name, "sort_order": idx} for idx, name in enumerate(_SEED_SHAPES)]
    op.bulk_insert(
        sa.table(
            "stone_shape_master",
            sa.column("name", sa.String()),
            sa.column("sort_order", sa.Integer()),
        ),
        rows,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stone_shape_master_archived_at", table_name="stone_shape_master"
    )
    op.drop_index("uq_stone_shape_master_name", table_name="stone_shape_master")
    op.drop_table("stone_shape_master")
