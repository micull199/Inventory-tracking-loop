"""create purchase_orders, purchase_order_lines tables

Revision ID: 0011_create_purchase_orders
Revises: 0010_create_cost_layers_and_movements
Create Date: 2026-05-07

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0011_create_purchase_orders"
down_revision = "0010_create_cost_layers_and_movements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # purchase_orders first — purchase_order_lines FKs into it.
    #
    # ``stock_movements.po_id`` is intentionally NOT being upgraded to a real
    # FK in this migration. The column is currently always NULL (no PO5 yet)
    # and adding the constraint is PO5's responsibility — the receive path is
    # what activates the link.
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "supplier_id",
            sa.Integer(),
            sa.ForeignKey("suppliers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("expected_date", sa.Date(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
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
        "ix_purchase_orders_supplier_id", "purchase_orders", ["supplier_id"]
    )
    op.create_index("ix_purchase_orders_status", "purchase_orders", ["status"])
    op.create_index(
        "ix_purchase_orders_created_at", "purchase_orders", ["created_at"]
    )

    # purchase_order_lines — CASCADE on po_id so a hard-deleted PO drops its
    # lines. v1 doesn't expose a hard-delete path; the cascade is for parity
    # with future "destroy a never-sent draft" UX.
    op.create_table(
        "purchase_order_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "po_id",
            sa.Integer(),
            sa.ForeignKey("purchase_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("qty_ordered", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "qty_received",
            sa.Numeric(14, 4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("expected_unit_cost", sa.Numeric(14, 4), nullable=True),
    )
    op.create_index(
        "ix_purchase_order_lines_po_id", "purchase_order_lines", ["po_id"]
    )
    op.create_index(
        "ix_purchase_order_lines_item_id", "purchase_order_lines", ["item_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_purchase_order_lines_item_id", table_name="purchase_order_lines"
    )
    op.drop_index(
        "ix_purchase_order_lines_po_id", table_name="purchase_order_lines"
    )
    op.drop_table("purchase_order_lines")

    op.drop_index(
        "ix_purchase_orders_created_at", table_name="purchase_orders"
    )
    op.drop_index("ix_purchase_orders_status", table_name="purchase_orders")
    op.drop_index(
        "ix_purchase_orders_supplier_id", table_name="purchase_orders"
    )
    op.drop_table("purchase_orders")
