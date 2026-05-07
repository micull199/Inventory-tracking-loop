"""add taxonomy_nodes.defaults_json for per-category item defaults

Revision ID: 0015_taxonomy_defaults_json
Revises: 0014_create_stock_takes
Create Date: 2026-05-07

User-facing change: a Manager can set defaults on a leaf category for the
item form's core fields (unit, tracking_mode, requires_checkout,
reorder_threshold, reorder_qty, supplier_id, location_id). When a user
creates an item in that category, those fields pre-fill, saving repeated
typing for items that share the same physical attributes.

Schema: a single JSON column ``defaults_json`` on ``taxonomy_nodes``,
nullable. ``NULL`` (or an empty dict) means "no defaults — start blank".
A populated dict's keys are the form-field names; values are coerced to
the matching type at write time (decimals as strings, FKs as ints,
tracking_mode validated against the enum). Storing as JSON instead of 7
typed columns keeps the migration footprint tiny and lets the schema
evolve (a future custom-field default could land in the same blob)
without another ALTER TABLE.

SQLite stores JSON as TEXT; Postgres uses native JSONB. SQLAlchemy's
``JSON`` type abstracts both. No dialect branching needed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "0015_taxonomy_defaults_json"
down_revision = "0014_create_stock_takes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use batch_alter_table so SQLite (which doesn't support most ALTER ops
    # natively) goes through the table-recreate path; Postgres falls through
    # to a direct ALTER TABLE.
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.add_column(sa.Column("defaults_json", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.drop_column("defaults_json")
