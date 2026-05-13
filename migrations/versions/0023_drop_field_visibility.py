"""drop taxonomy_nodes.field_visibility_json

Revision ID: 0023_drop_field_visibility
Revises: 0022_backfill_catalog_key
Create Date: 2026-05-13

Slice 6 of the catalog-driven taxonomy refactor removed the per-leaf
"built-in field visibility" override mechanism. Items always use the
defaults declared in ``app.field_visibility._DEFAULT_VISIBILITY`` (name +
unit required, everything else optional), so the JSON column that stored
per-node overrides is no longer read by any code path.

The column added in 0017 is dropped here. The downgrade re-adds it as
nullable; we don't attempt to backfill, because the override system is
gone from the application layer — any column re-added on rollback would
just sit empty.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_drop_field_visibility"
down_revision = "0022_backfill_catalog_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.drop_column("field_visibility_json")


def downgrade() -> None:
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.add_column(
            sa.Column("field_visibility_json", sa.JSON(), nullable=True)
        )
