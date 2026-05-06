"""create stock_movements, cost_layers, cost_layer_consumptions tables

Revision ID: 0010_create_cost_layers_and_movements
Revises: 0009_create_item_units
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0010_create_cost_layers_and_movements"
down_revision = "0009_create_item_units"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # stock_movements first — cost_layers and cost_layer_consumptions both
    # FK into it.
    #
    # po_id and stock_take_id are plain integer columns (no FK) because the
    # purchase_orders and stock_takes tables don't exist yet. The FK
    # constraint will be added in a follow-up migration when PO2 / ST1 land.
    op.create_table(
        "stock_movements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "item_unit_id",
            sa.Integer(),
            sa.ForeignKey("item_units.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("qty", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("reason", sa.String(length=255), nullable=True),
        sa.Column("note", sa.String(length=2000), nullable=True),
        sa.Column("po_id", sa.Integer(), nullable=True),
        sa.Column("stock_take_id", sa.Integer(), nullable=True),
        sa.Column("total_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_stock_movements_item_id", "stock_movements", ["item_id"])
    op.create_index(
        "ix_stock_movements_item_unit_id", "stock_movements", ["item_unit_id"]
    )
    op.create_index("ix_stock_movements_user_id", "stock_movements", ["user_id"])
    op.create_index("ix_stock_movements_type", "stock_movements", ["type"])
    op.create_index(
        "ix_stock_movements_created_at", "stock_movements", ["created_at"]
    )

    # cost_layers: FIFO buckets. qty_remaining is decremented by consumptions;
    # qty_received and unit_cost are immutable. The composite index covers the
    # FIFO ORDER BY: (item_id, received_at ASC, id ASC).
    op.create_table(
        "cost_layers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("qty_received", sa.Numeric(14, 4), nullable=False),
        sa.Column("qty_remaining", sa.Numeric(14, 4), nullable=False),
        sa.Column("unit_cost", sa.Numeric(14, 4), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column(
            "source_movement_id",
            sa.Integer(),
            sa.ForeignKey("stock_movements.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_cost_layers_item_id", "cost_layers", ["item_id"])
    op.create_index(
        "ix_cost_layers_source_movement_id",
        "cost_layers",
        ["source_movement_id"],
    )
    op.create_index(
        "ix_cost_layers_item_received",
        "cost_layers",
        ["item_id", "received_at", "id"],
    )

    # cost_layer_consumptions: one row per (layer, movement) tap. The composite
    # (movement_id, layer_id) index lets the item-detail page (M6) fetch a
    # movement's full consumption breakdown in one query.
    op.create_table(
        "cost_layer_consumptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "layer_id",
            sa.Integer(),
            sa.ForeignKey("cost_layers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "movement_id",
            sa.Integer(),
            sa.ForeignKey("stock_movements.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("qty_consumed", sa.Numeric(14, 4), nullable=False),
        sa.Column(
            "unit_cost_at_consumption", sa.Numeric(14, 4), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_cost_layer_consumptions_layer_id",
        "cost_layer_consumptions",
        ["layer_id"],
    )
    op.create_index(
        "ix_cost_layer_consumptions_movement_id",
        "cost_layer_consumptions",
        ["movement_id"],
    )
    op.create_index(
        "ix_cost_layer_consumptions_movement_layer",
        "cost_layer_consumptions",
        ["movement_id", "layer_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cost_layer_consumptions_movement_layer",
        table_name="cost_layer_consumptions",
    )
    op.drop_index(
        "ix_cost_layer_consumptions_movement_id",
        table_name="cost_layer_consumptions",
    )
    op.drop_index(
        "ix_cost_layer_consumptions_layer_id",
        table_name="cost_layer_consumptions",
    )
    op.drop_table("cost_layer_consumptions")

    op.drop_index("ix_cost_layers_item_received", table_name="cost_layers")
    op.drop_index(
        "ix_cost_layers_source_movement_id", table_name="cost_layers"
    )
    op.drop_index("ix_cost_layers_item_id", table_name="cost_layers")
    op.drop_table("cost_layers")

    op.drop_index("ix_stock_movements_created_at", table_name="stock_movements")
    op.drop_index("ix_stock_movements_type", table_name="stock_movements")
    op.drop_index("ix_stock_movements_user_id", table_name="stock_movements")
    op.drop_index(
        "ix_stock_movements_item_unit_id", table_name="stock_movements"
    )
    op.drop_index("ix_stock_movements_item_id", table_name="stock_movements")
    op.drop_table("stock_movements")
