"""create taxonomy_field_defs table

Revision ID: 0006_create_taxonomy_field_defs
Revises: 0005_create_taxonomy_nodes
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0006_create_taxonomy_field_defs"
down_revision = "0005_create_taxonomy_nodes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taxonomy_field_defs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "node_id",
            sa.Integer(),
            sa.ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("type", sa.String(length=16), nullable=False),
        sa.Column("options_json", sa.JSON(), nullable=True),
        sa.Column(
            "required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")
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
    op.create_index(
        "ix_taxonomy_field_defs_node_id", "taxonomy_field_defs", ["node_id"]
    )
    op.create_index(
        "ix_taxonomy_field_defs_archived_at",
        "taxonomy_field_defs",
        ["archived_at"],
    )
    # Both indexes span active *and* archived rows: archiving a field def must
    # not free its name or key, because items will reference these by id (and
    # likely by key for cross-version stability) and re-using the name later
    # would silently overload the historical record.
    op.create_index(
        "uq_taxonomy_field_defs_node_name",
        "taxonomy_field_defs",
        ["node_id", "name"],
        unique=True,
    )
    op.create_index(
        "uq_taxonomy_field_defs_node_key",
        "taxonomy_field_defs",
        ["node_id", "key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_taxonomy_field_defs_node_key", table_name="taxonomy_field_defs")
    op.drop_index("uq_taxonomy_field_defs_node_name", table_name="taxonomy_field_defs")
    op.drop_index(
        "ix_taxonomy_field_defs_archived_at", table_name="taxonomy_field_defs"
    )
    op.drop_index("ix_taxonomy_field_defs_node_id", table_name="taxonomy_field_defs")
    op.drop_table("taxonomy_field_defs")
