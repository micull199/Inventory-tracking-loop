"""create taxonomy_stages and wire lifecycle stage FKs

Revision ID: 0018_lifecycle_stages
Revises: 0017_taxonomy_field_visibility
Create Date: 2026-05-12

Lifecycle stages (Slice 1 of the "proposed scope changes" for stage tracking +
in-transit transfers — see PROGRESS.md). Each top-level taxonomy node defines
its own ordered list of stages. Items in that category carry a
``current_stage_id``; transitions are recorded as ``stage_change`` movements
with both ``from_stage_id`` and ``to_stage_id`` populated. The cost engine is
never invoked on a stage change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0018_lifecycle_stages"
down_revision = "0017_taxonomy_field_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "taxonomy_stages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "top_level_node_id",
            sa.Integer(),
            sa.ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "sort_order",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "is_initial",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
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
        "ix_taxonomy_stages_top_level_node_id",
        "taxonomy_stages",
        ["top_level_node_id"],
    )
    op.create_index(
        "ix_taxonomy_stages_archived_at",
        "taxonomy_stages",
        ["archived_at"],
    )
    # ``(top_level_node_id, name)`` uniqueness spans active + archived rows,
    # matching the ``TaxonomyNode`` convention.
    op.create_index(
        "uq_taxonomy_stage_name",
        "taxonomy_stages",
        ["top_level_node_id", "name"],
        unique=True,
    )
    # Partial unique: at most one ``is_initial = TRUE`` per top-level node
    # while active. SQLite stores booleans as 0/1; Postgres uses a boolean
    # expression.
    op.create_index(
        "uq_taxonomy_stage_initial_active",
        "taxonomy_stages",
        ["top_level_node_id"],
        unique=True,
        sqlite_where=sa.text("is_initial = 1 AND archived_at IS NULL"),
        postgresql_where=sa.text("is_initial AND archived_at IS NULL"),
    )

    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(sa.Column("current_stage_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_items_current_stage_id",
            "taxonomy_stages",
            ["current_stage_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index(
            "ix_items_current_stage_id",
            ["current_stage_id"],
        )

    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.add_column(sa.Column("from_stage_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("to_stage_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_stock_movements_from_stage_id",
            "taxonomy_stages",
            ["from_stage_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_stock_movements_to_stage_id",
            "taxonomy_stages",
            ["to_stage_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("stock_movements") as batch_op:
        batch_op.drop_constraint("fk_stock_movements_to_stage_id", type_="foreignkey")
        batch_op.drop_constraint("fk_stock_movements_from_stage_id", type_="foreignkey")
        batch_op.drop_column("to_stage_id")
        batch_op.drop_column("from_stage_id")

    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_index("ix_items_current_stage_id")
        batch_op.drop_constraint("fk_items_current_stage_id", type_="foreignkey")
        batch_op.drop_column("current_stage_id")

    op.drop_index("uq_taxonomy_stage_initial_active", table_name="taxonomy_stages")
    op.drop_index("uq_taxonomy_stage_name", table_name="taxonomy_stages")
    op.drop_index("ix_taxonomy_stages_archived_at", table_name="taxonomy_stages")
    op.drop_index(
        "ix_taxonomy_stages_top_level_node_id", table_name="taxonomy_stages"
    )
    op.drop_table("taxonomy_stages")
