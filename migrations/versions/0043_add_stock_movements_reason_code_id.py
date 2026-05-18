"""add stock_movements.reason_code_id FK column

Revision ID: 0043_add_stock_movements_reason_code_id
Revises: 0042_create_reason_codes
Create Date: 2026-05-15

Final S5 migration. ``stock_movements.reason_code_id`` references
``reason_codes.id`` nullable RESTRICT. The legacy freetext
``stock_movements.reason`` column survives for the long tail.

Same batch_alter_table dance as the rest of the FK-adding migrations
(0012, 0019, 0029, 0032, 0041) for the SQLite ``ALTER TABLE … ADD
CONSTRAINT`` limitation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0043_add_stock_movements_reason_code_id"
down_revision = "0042_create_reason_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.add_column(
            sa.Column("reason_code_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_stock_movements_reason_code_id",
            "reason_codes",
            ["reason_code_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_stock_movements_reason_code_id", ["reason_code_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.drop_index("ix_stock_movements_reason_code_id")
        batch_op.drop_constraint(
            "fk_stock_movements_reason_code_id", type_="foreignkey"
        )
        batch_op.drop_column("reason_code_id")
