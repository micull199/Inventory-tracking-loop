"""create reason_codes lookup + seed 'out' reasons

Revision ID: 0042_create_reason_codes
Revises: 0041_add_items_unit_id
Create Date: 2026-05-15

S5 of the architectural additions spec. Movement-type-scoped reason
vocabulary that replaces a slice of the freetext ``StockMovement.reason``.
The freetext column survives for the long tail (one-off explanations
that don't deserve a code).

Seed values match the spec's worked example for the ``out`` movement
type. Codes for other movement types can be seeded later as operators
identify which reasons recur often enough to deserve a lookup row.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0042_create_reason_codes"
down_revision = "0041_add_items_unit_id"
branch_labels = None
depends_on = None


# (movement_type, code, label). The spec lists the ``out`` reasons by
# name; the labels are short human-facing strings sized for a select
# control. Order matches the spec.
_SEED_REASON_CODES: tuple[tuple[str, str, str], ...] = (
    ("out", "sale", "Sale"),
    ("out", "customer_pickup", "Customer pickup"),
    ("out", "bench_consumption", "Bench consumption"),
    ("out", "casting_consumption", "Casting consumption"),
    ("out", "wastage", "Wastage"),
    ("out", "internal_use", "Internal use"),
    ("out", "damaged", "Damaged"),
)


def upgrade() -> None:
    op.create_table(
        "reason_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("movement_type", sa.String(length=16), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
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
        "uq_reason_codes_type_code",
        "reason_codes",
        ["movement_type", "code"],
        unique=True,
    )
    op.create_index(
        "ix_reason_codes_movement_type", "reason_codes", ["movement_type"]
    )
    op.create_index(
        "ix_reason_codes_archived_at", "reason_codes", ["archived_at"]
    )

    rows = [
        {
            "movement_type": mtype,
            "code": code,
            "label": label,
            "sort_order": idx,
        }
        for idx, (mtype, code, label) in enumerate(_SEED_REASON_CODES)
    ]
    op.bulk_insert(
        sa.table(
            "reason_codes",
            sa.column("movement_type", sa.String()),
            sa.column("code", sa.String()),
            sa.column("label", sa.String()),
            sa.column("sort_order", sa.Integer()),
        ),
        rows,
    )


def downgrade() -> None:
    op.drop_index("ix_reason_codes_archived_at", table_name="reason_codes")
    op.drop_index("ix_reason_codes_movement_type", table_name="reason_codes")
    op.drop_index("uq_reason_codes_type_code", table_name="reason_codes")
    op.drop_table("reason_codes")
