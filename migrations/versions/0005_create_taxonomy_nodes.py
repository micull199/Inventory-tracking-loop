"""create taxonomy_nodes table

Revision ID: 0005_create_taxonomy_nodes
Revises: 0004_create_locations
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0005_create_taxonomy_nodes"
down_revision = "0004_create_locations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taxonomy_nodes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "parent_id",
            sa.Integer(),
            sa.ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("name", sa.String(length=255), nullable=False),
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
        "ix_taxonomy_nodes_parent_id", "taxonomy_nodes", ["parent_id"]
    )
    op.create_index(
        "ix_taxonomy_nodes_archived_at", "taxonomy_nodes", ["archived_at"]
    )
    # Partial unique indexes — sibling-scoped uniqueness across active *and*
    # archived rows. The two-shape split lets the same migration support both
    # S3 (top-level only, parent_id IS NULL) and S4 (sub-categories,
    # parent_id IS NOT NULL) without re-issuing schema later. Both SQLite
    # (3.8.0+) and Postgres support partial indexes.
    op.create_index(
        "uq_taxonomy_top_name",
        "taxonomy_nodes",
        ["name"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL"),
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "uq_taxonomy_child_name",
        "taxonomy_nodes",
        ["parent_id", "name"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NOT NULL"),
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_taxonomy_child_name", table_name="taxonomy_nodes")
    op.drop_index("uq_taxonomy_top_name", table_name="taxonomy_nodes")
    op.drop_index("ix_taxonomy_nodes_archived_at", table_name="taxonomy_nodes")
    op.drop_index("ix_taxonomy_nodes_parent_id", table_name="taxonomy_nodes")
    op.drop_table("taxonomy_nodes")
