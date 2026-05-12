"""create transfer_orders and transfer_order_lines

Revision ID: 0019_transfer_orders
Revises: 0018_lifecycle_stages
Create Date: 2026-05-12

Slice 2 of the in-transit / stages scope addition (see PROGRESS.md). A
Transfer Order is a document representing stock moving between two UC
locations with separate ship + receive events. While shipped but not yet
received, each line's item has ``location_id = NULL`` and the TO is visible
in the in-transit listing.

The cost engine is **not** invoked on the resulting TRANSFER movements
(matches the existing instant-flip behaviour). The two-event TRANSFERs are
linked to the parent TO via the new ``stock_movements.transfer_order_id``
column added here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0019_transfer_orders"
down_revision = "0018_lifecycle_stages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transfer_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "destination_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expected_arrival", sa.Date(), nullable=True),
        sa.Column("carrier", sa.String(length=128), nullable=True),
        sa.Column("tracking_number", sa.String(length=128), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "shipped_by",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "received_by",
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
        "ix_transfer_orders_source_location_id",
        "transfer_orders",
        ["source_location_id"],
    )
    op.create_index(
        "ix_transfer_orders_destination_location_id",
        "transfer_orders",
        ["destination_location_id"],
    )
    op.create_index("ix_transfer_orders_status", "transfer_orders", ["status"])
    op.create_index(
        "ix_transfer_orders_created_at", "transfer_orders", ["created_at"]
    )

    op.create_table(
        "transfer_order_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "transfer_order_id",
            sa.Integer(),
            sa.ForeignKey("transfer_orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("qty", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "ship_movement_id",
            sa.Integer(),
            sa.ForeignKey("stock_movements.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "receive_movement_id",
            sa.Integer(),
            sa.ForeignKey("stock_movements.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "uq_transfer_order_line_item",
        "transfer_order_lines",
        ["transfer_order_id", "item_id"],
        unique=True,
    )
    op.create_index(
        "ix_transfer_order_lines_item_id", "transfer_order_lines", ["item_id"]
    )

    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.add_column(
            sa.Column("transfer_order_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_stock_movements_transfer_order_id",
            "transfer_orders",
            ["transfer_order_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.drop_constraint(
            "fk_stock_movements_transfer_order_id", type_="foreignkey"
        )
        batch_op.drop_column("transfer_order_id")

    op.drop_index(
        "ix_transfer_order_lines_item_id", table_name="transfer_order_lines"
    )
    op.drop_index(
        "uq_transfer_order_line_item", table_name="transfer_order_lines"
    )
    op.drop_table("transfer_order_lines")

    op.drop_index("ix_transfer_orders_created_at", table_name="transfer_orders")
    op.drop_index("ix_transfer_orders_status", table_name="transfer_orders")
    op.drop_index(
        "ix_transfer_orders_destination_location_id", table_name="transfer_orders"
    )
    op.drop_index(
        "ix_transfer_orders_source_location_id", table_name="transfer_orders"
    )
    op.drop_table("transfer_orders")
