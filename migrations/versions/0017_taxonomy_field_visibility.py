"""add field_visibility_json to taxonomy_nodes

Revision ID: 0017_taxonomy_field_visibility
Revises: 0016_taxonomy_archetype_and_prefix
Create Date: 2026-05-12

Per-leaf control over which built-in item-form fields are required, optional,
or hidden. Stored as a JSON dict keyed by item-form field name (``name``,
``unit``, ``tracking_mode``, ``requires_checkout``, ``reorder_threshold``,
``reorder_qty``, ``supplier_id``, ``location_id``, ``qr_code``) mapped to one
of ``"required" | "optional" | "hidden"``. Absent / null column means
"use defaults" — see ``app.taxonomy.effective_field_visibility``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017_taxonomy_field_visibility"
down_revision = "0016_taxonomy_archetype_and_prefix"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.add_column(
            sa.Column("field_visibility_json", sa.JSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.drop_column("field_visibility_json")
