"""add nullable catalog_key to taxonomy_field_defs

Revision ID: 0021_taxonomy_field_def_catalog_key
Revises: 0020_po_in_transit
Create Date: 2026-05-13

First slice of the catalog-driven taxonomy refactor: adds a nullable
``catalog_key`` column to ``taxonomy_field_defs`` so subsequent slices can
start writing it (slice 3) and backfilling it (slice 4). Migration 0023
tightens the column to NOT NULL and drops the now-redundant ``name`` /
``type`` / ``options_json`` columns once every row has been mapped.

The column is intentionally not a real foreign key — the catalog lives in
Python (``app.field_catalog.FIELD_CATALOG``), so referential integrity is
enforced at write time by the route layer.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_taxonomy_field_def_catalog_key"
down_revision = "0020_po_in_transit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("taxonomy_field_defs") as batch_op:
        batch_op.add_column(
            sa.Column("catalog_key", sa.String(length=64), nullable=True)
        )
        batch_op.create_index(
            "ix_taxonomy_field_defs_catalog_key",
            ["catalog_key"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("taxonomy_field_defs") as batch_op:
        batch_op.drop_index("ix_taxonomy_field_defs_catalog_key")
        batch_op.drop_column("catalog_key")
