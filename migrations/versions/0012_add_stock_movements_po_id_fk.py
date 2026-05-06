"""add FK constraint on stock_movements.po_id

Revision ID: 0012_add_stock_movements_po_id_fk
Revises: 0011_create_purchase_orders
Create Date: 2026-05-07

PO5 activates the link between a stock-in movement and the PO it received
against. Migration 0010 deferred this FK because ``purchase_orders`` did not
yet exist; PO5 is the slice that writes ``po_id`` on a ``StockMovement`` row,
so this is the right time to add the constraint.

SQLite cannot ``ALTER TABLE … ADD CONSTRAINT``; ``op.batch_alter_table``
recreates the table and copies the data, which is the standard Alembic
SQLite-compat path. Postgres handles a plain ``ADD CONSTRAINT`` natively;
``batch_alter_table`` falls through on non-SQLite backends.
"""
from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "0012_add_stock_movements_po_id_fk"
down_revision = "0011_create_purchase_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.create_foreign_key(
            "fk_stock_movements_po_id",
            "purchase_orders",
            ["po_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.drop_constraint(
            "fk_stock_movements_po_id", type_="foreignkey"
        )
