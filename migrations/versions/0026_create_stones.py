"""create stones table

Revision ID: 0026_create_stones
Revises: 0025_create_stone_shape_master
Create Date: 2026-05-15

S1 of the architectural additions spec. The load-bearing addition:
``stones`` is the master table for any stone with a grading report (or any
stone we elect to track individually). Melee continues to live as
aggregate count + carat weight on the parent item — see migration 0029.

``status``, ``current_item_id``, ``current_location_id`` are denormalised
from ``stone_events`` (migration 0027) — every set/unset/sell/relocate
writes a ledger row AND updates the denormalised fields in one transaction
(same posture as ``items.current_qty`` from ``cost_layers``).

Co-creates ``sequence_counters`` — a tiny lookup keyed by counter name —
and seeds it with ``stone_code`` so the stone-code allocator (single
global ``STN-NNNNNN`` sequence per the S1 spec recommendation) has a row
to spin on day one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0026_create_stones"
down_revision = "0025_create_stone_shape_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Generic counter table for any non-per-leaf sequence allocator the app
    # needs. S1 seeds it with ``stone_code`` (STN-NNNNNN); a future slice may
    # add ``design_code`` (DSG-...) or similar — each as its own row, no
    # schema change required.
    op.create_table(
        "sequence_counters",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "next_value",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
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
        "uq_sequence_counters_name",
        "sequence_counters",
        ["name"],
        unique=True,
    )
    op.bulk_insert(
        sa.table(
            "sequence_counters",
            sa.column("name", sa.String()),
            sa.column("next_value", sa.Integer()),
        ),
        [{"name": "stone_code", "next_value": 1}],
    )

    op.create_table(
        "stones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("stone_code", sa.String(length=32), nullable=False),
        sa.Column("stone_type", sa.String(length=16), nullable=False),
        sa.Column(
            "shape_id",
            sa.Integer(),
            sa.ForeignKey("stone_shape_master.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("length_mm", sa.Numeric(8, 3), nullable=True),
        sa.Column("width_mm", sa.Numeric(8, 3), nullable=True),
        sa.Column("depth_mm", sa.Numeric(8, 3), nullable=True),
        sa.Column("carat_weight", sa.Numeric(8, 4), nullable=False),
        sa.Column("colour_grade", sa.String(length=8), nullable=True),
        sa.Column("clarity_grade", sa.String(length=8), nullable=True),
        sa.Column("cut_grade", sa.String(length=16), nullable=True),
        sa.Column("polish", sa.String(length=16), nullable=True),
        sa.Column("symmetry", sa.String(length=16), nullable=True),
        sa.Column("fluorescence", sa.String(length=16), nullable=True),
        sa.Column("lab", sa.String(length=16), nullable=True),
        sa.Column("cert_number", sa.String(length=64), nullable=True),
        sa.Column("cert_url", sa.String(length=512), nullable=True),
        sa.Column(
            "origin",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'natural'"),
        ),
        sa.Column("treatment", sa.String(length=64), nullable=True),
        sa.Column(
            "supplier_id",
            sa.Integer(),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "ownership",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'owned'"),
        ),
        sa.Column("memo_due_date", sa.Date(), nullable=True),
        sa.Column("acquisition_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("acquisition_date", sa.Date(), nullable=True),
        sa.Column(
            "current_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "status",
            sa.String(length=24),
            nullable=False,
            server_default=sa.text("'available'"),
        ),
        sa.Column(
            "current_item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("notes", sa.String(length=2000), nullable=True),
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

    # ``stone_code`` is unique across active + archived rows — re-using a
    # code would silently misattribute history. Same posture as ``Item.sku``.
    op.create_index("uq_stones_stone_code", "stones", ["stone_code"], unique=True)
    # Partial unique on (lab, cert_number): a given certificate number is
    # unique within an issuing lab. Multiple uncertificated stones (lab/cert
    # NULL) coexist; matched only when both columns are set.
    op.create_index(
        "uq_stones_cert",
        "stones",
        ["lab", "cert_number"],
        unique=True,
        sqlite_where=sa.text("cert_number IS NOT NULL AND lab IS NOT NULL"),
        postgresql_where=sa.text("cert_number IS NOT NULL AND lab IS NOT NULL"),
    )
    op.create_index("ix_stones_supplier_id", "stones", ["supplier_id"])
    op.create_index(
        "ix_stones_current_location_id", "stones", ["current_location_id"]
    )
    op.create_index(
        "ix_stones_current_item_id", "stones", ["current_item_id"]
    )
    op.create_index("ix_stones_status", "stones", ["status"])
    op.create_index("ix_stones_archived_at", "stones", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_stones_archived_at", table_name="stones")
    op.drop_index("ix_stones_status", table_name="stones")
    op.drop_index("ix_stones_current_item_id", table_name="stones")
    op.drop_index("ix_stones_current_location_id", table_name="stones")
    op.drop_index("ix_stones_supplier_id", table_name="stones")
    op.drop_index("uq_stones_cert", table_name="stones")
    op.drop_index("uq_stones_stone_code", table_name="stones")
    op.drop_table("stones")
    op.drop_index("uq_sequence_counters_name", table_name="sequence_counters")
    op.drop_table("sequence_counters")
