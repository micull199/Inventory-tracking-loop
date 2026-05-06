"""create item_field_values table

Revision ID: 0008_create_item_field_values
Revises: 0007_create_items
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008_create_item_field_values"
down_revision = "0007_create_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_field_values",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "field_def_id",
            sa.Integer(),
            sa.ForeignKey("taxonomy_field_defs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Sparse: exactly one of these is populated per row (per the field's
        # type). Select stores the chosen option in value_text; multiselect
        # stores the list in value_json.
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_number", sa.Integer(), nullable=True),
        sa.Column("value_decimal", sa.Numeric(14, 4), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column("value_json", sa.JSON(), nullable=True),
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
        "ix_item_field_values_item_id", "item_field_values", ["item_id"]
    )
    op.create_index(
        "ix_item_field_values_field_def_id",
        "item_field_values",
        ["field_def_id"],
    )
    # One row per (item, field def) — items inherit each leaf's schema once,
    # not multiple times. The route layer enforces presence; this index makes
    # double-writes impossible at the storage layer.
    op.create_index(
        "uq_item_field_values_item_field_def",
        "item_field_values",
        ["item_id", "field_def_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_item_field_values_item_field_def", table_name="item_field_values"
    )
    op.drop_index(
        "ix_item_field_values_field_def_id", table_name="item_field_values"
    )
    op.drop_index("ix_item_field_values_item_id", table_name="item_field_values")
    op.drop_table("item_field_values")
