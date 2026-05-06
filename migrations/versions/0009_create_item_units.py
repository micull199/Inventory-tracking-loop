"""create item_units table

Revision ID: 0009_create_item_units
Revises: 0008_create_item_field_values
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0009_create_item_units"
down_revision = "0008_create_item_field_values"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_units",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("serial_or_label", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=True,
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
    op.create_index("ix_item_units_item_id", "item_units", ["item_id"])
    op.create_index("ix_item_units_location_id", "item_units", ["location_id"])
    op.create_index("ix_item_units_archived_at", "item_units", ["archived_at"])
    # serial_or_label is unique within an item across active *and* archived
    # rows. Different items can legitimately share a serial (labels are
    # item-scoped). Same archive-doesn't-free-the-name reasoning as
    # Supplier.name and Item.sku.
    op.create_index(
        "uq_item_units_item_serial",
        "item_units",
        ["item_id", "serial_or_label"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_item_units_item_serial", table_name="item_units")
    op.drop_index("ix_item_units_archived_at", table_name="item_units")
    op.drop_index("ix_item_units_location_id", table_name="item_units")
    op.drop_index("ix_item_units_item_id", table_name="item_units")
    op.drop_table("item_units")
