"""create stock_takes + stock_take_lines and activate stock_movements.stock_take_id FK

Revision ID: 0014_create_stock_takes
Revises: 0013_create_checkouts
Create Date: 2026-05-07

ST1 lays the foundation for DoD #5 (Office user runs a stock take end-to-end).

Schema mirrors MISSION §6 exactly:
- ``stock_takes``: id, scope_node_id?, scope_location_id?, scheduled_for,
  started_at?, completed_at?, notes?, created_by?, timestamps. No status enum;
  the lifecycle is derived from the timestamps.
- ``stock_take_lines``: id, stock_take_id, item_id, system_qty, counted_qty?,
  variance?, committed, notes?. ST1 doesn't populate this table; ST2 will.

The third change activates the FK on ``stock_movements.stock_take_id`` (M1
deferred this until ST1 landed). The column was previously a plain Integer
nullable; this migration adds the RESTRICT FK via ``op.batch_alter_table`` so
the SQLite path works (Postgres does the same dance via the same batch op,
which falls through to a direct ``ALTER TABLE``). Same SQLite-compat path as
``0012_add_stock_movements_po_id_fk.py``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0014_create_stock_takes"
down_revision = "0013_create_checkouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stock_takes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "scope_node_id",
            sa.Integer(),
            sa.ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "scope_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("scheduled_for", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
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
        "ix_stock_takes_scope_node_id", "stock_takes", ["scope_node_id"]
    )
    op.create_index(
        "ix_stock_takes_scope_location_id",
        "stock_takes",
        ["scope_location_id"],
    )
    op.create_index(
        "ix_stock_takes_scheduled_for", "stock_takes", ["scheduled_for"]
    )
    op.create_index(
        "ix_stock_takes_completed_at", "stock_takes", ["completed_at"]
    )

    op.create_table(
        "stock_take_lines",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "stock_take_id",
            sa.Integer(),
            sa.ForeignKey("stock_takes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("system_qty", sa.Numeric(14, 4), nullable=False),
        sa.Column("counted_qty", sa.Numeric(14, 4), nullable=True),
        sa.Column("variance", sa.Numeric(14, 4), nullable=True),
        sa.Column(
            "committed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("notes", sa.String(length=2000), nullable=True),
    )
    op.create_index(
        "ix_stock_take_lines_stock_take_id",
        "stock_take_lines",
        ["stock_take_id"],
    )
    op.create_index(
        "ix_stock_take_lines_item_id", "stock_take_lines", ["item_id"]
    )

    # Activate the FK on stock_movements.stock_take_id. SQLite cannot
    # ALTER TABLE … ADD CONSTRAINT, so batch_alter_table recreates the table
    # and copies the data; on Postgres this falls through to a direct
    # ALTER TABLE.
    with op.batch_alter_table("stock_movements", recreate="auto") as batch_op:
        batch_op.create_foreign_key(
            "fk_stock_movements_stock_take_id",
            "stock_takes",
            ["stock_take_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_movements", recreate="auto") as batch_op:
        batch_op.drop_constraint(
            "fk_stock_movements_stock_take_id", type_="foreignkey"
        )

    op.drop_index(
        "ix_stock_take_lines_item_id", table_name="stock_take_lines"
    )
    op.drop_index(
        "ix_stock_take_lines_stock_take_id", table_name="stock_take_lines"
    )
    op.drop_table("stock_take_lines")

    op.drop_index("ix_stock_takes_completed_at", table_name="stock_takes")
    op.drop_index("ix_stock_takes_scheduled_for", table_name="stock_takes")
    op.drop_index(
        "ix_stock_takes_scope_location_id", table_name="stock_takes"
    )
    op.drop_index("ix_stock_takes_scope_node_id", table_name="stock_takes")
    op.drop_table("stock_takes")
