"""create unit_master lookup + seed canonical units

Revision ID: 0040_create_unit_master
Revises: 0039_create_item_engraving_attrs
Create Date: 2026-05-15

S5 of the architectural additions spec. Lookup for units of measure —
replaces freetext ``Item.unit`` for new entities. The legacy column
survives during migration per the spec's "defer until reporting starts
to bite" posture.

Seed values match the spec verbatim: ``ea``, ``pc``, ``g``, ``kg``,
``ct``, ``mm``, ``cm``, ``m``, ``pair``, ``pack``. Same archive-
doesn't-free-the-code convention as the other lookups.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0040_create_unit_master"
down_revision = "0039_create_item_engraving_attrs"
branch_labels = None
depends_on = None


_SEED_UNITS: tuple[tuple[str, str], ...] = (
    ("ea", "each"),
    ("pc", "piece"),
    ("g", "gram"),
    ("kg", "kilogram"),
    ("ct", "carat"),
    ("mm", "millimetre"),
    ("cm", "centimetre"),
    ("m", "metre"),
    ("pair", "pair"),
    ("pack", "pack"),
)


def upgrade() -> None:
    op.create_table(
        "unit_master",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
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
        "uq_unit_master_code", "unit_master", ["code"], unique=True
    )
    op.create_index(
        "ix_unit_master_archived_at", "unit_master", ["archived_at"]
    )

    rows = [
        {"code": code, "name": name, "sort_order": idx}
        for idx, (code, name) in enumerate(_SEED_UNITS)
    ]
    op.bulk_insert(
        sa.table(
            "unit_master",
            sa.column("code", sa.String()),
            sa.column("name", sa.String()),
            sa.column("sort_order", sa.Integer()),
        ),
        rows,
    )


def downgrade() -> None:
    op.drop_index("ix_unit_master_archived_at", table_name="unit_master")
    op.drop_index("uq_unit_master_code", table_name="unit_master")
    op.drop_table("unit_master")
