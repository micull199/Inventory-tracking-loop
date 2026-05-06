"""create items table

Revision ID: 0007_create_items
Revises: 0006_create_taxonomy_field_defs
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007_create_items"
down_revision = "0006_create_taxonomy_field_defs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "taxonomy_node_id",
            sa.Integer(),
            sa.ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("tracking_mode", sa.String(length=16), nullable=False),
        sa.Column(
            "requires_checkout",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "current_qty",
            sa.Numeric(14, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reorder_threshold",
            sa.Numeric(14, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "reorder_qty",
            sa.Numeric(14, 4),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "supplier_id",
            sa.Integer(),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("qr_code", sa.String(length=128), nullable=True),
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
    op.create_index("ix_items_taxonomy_node_id", "items", ["taxonomy_node_id"])
    op.create_index("ix_items_supplier_id", "items", ["supplier_id"])
    op.create_index("ix_items_location_id", "items", ["location_id"])
    op.create_index("ix_items_archived_at", "items", ["archived_at"])
    # SKU is unique across active *and* archived rows: archiving must not free
    # a SKU because purchase orders, audit rows, and FIFO layers reference an
    # item by id but humans reference by SKU — re-using one would silently
    # point operators at the wrong row.
    op.create_index("uq_items_sku", "items", ["sku"], unique=True)
    # QR codes are unique only where set: nullable, so multiple items can
    # legitimately have no printed label, but every printed code maps to one
    # item.
    op.create_index(
        "uq_items_qr_code",
        "items",
        ["qr_code"],
        unique=True,
        sqlite_where=sa.text("qr_code IS NOT NULL"),
        postgresql_where=sa.text("qr_code IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_items_qr_code", table_name="items")
    op.drop_index("uq_items_sku", table_name="items")
    op.drop_index("ix_items_archived_at", table_name="items")
    op.drop_index("ix_items_location_id", table_name="items")
    op.drop_index("ix_items_supplier_id", table_name="items")
    op.drop_index("ix_items_taxonomy_node_id", table_name="items")
    op.drop_table("items")
