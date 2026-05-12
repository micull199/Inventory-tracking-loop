"""add shipped_at to purchase_orders for IN_TRANSIT visibility

Revision ID: 0020_po_in_transit
Revises: 0019_transfer_orders
Create Date: 2026-05-12

Slice 3 of the in-transit / stages scope addition. Adds ``shipped_at`` to
``purchase_orders`` so the dashboard can show what suppliers have confirmed
dispatch on. The new ``in_transit`` value on ``POStatus`` does not require a
column change — the existing ``status`` column is stored as a 20-char string
(see migration 0011), which already accommodates the new value.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_po_in_transit"
down_revision = "0019_transfer_orders"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("purchase_orders") as batch_op:
        batch_op.add_column(
            sa.Column("shipped_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("purchase_orders") as batch_op:
        batch_op.drop_column("shipped_at")
