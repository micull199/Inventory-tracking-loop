"""add items.unit_id FK column

Revision ID: 0041_add_items_unit_id
Revises: 0040_create_unit_master
Create Date: 2026-05-15

S5 of the architectural additions spec. ``items.unit_id`` references
``unit_master.id`` nullable RESTRICT. The legacy freetext ``items.unit``
column survives during migration; backfill of existing values to
``unit_id`` is deferred per the spec's posture.

SQLite cannot ``ALTER TABLE … ADD CONSTRAINT``; ``batch_alter_table``
recreates the table on SQLite and falls through to a direct ALTER on
Postgres.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0041_add_items_unit_id"
down_revision = "0040_create_unit_master"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(sa.Column("unit_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_items_unit_id",
            "unit_master",
            ["unit_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_index("ix_items_unit_id", ["unit_id"])


def downgrade() -> None:
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_index("ix_items_unit_id")
        batch_op.drop_constraint("fk_items_unit_id", type_="foreignkey")
        batch_op.drop_column("unit_id")
