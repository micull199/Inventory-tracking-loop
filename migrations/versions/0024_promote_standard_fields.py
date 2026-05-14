"""promote standard fields; drop custom-field storage

Revision ID: 0024_promote_standard_fields
Revises: 0023_drop_field_visibility
Create Date: 2026-05-14

Collapses the dual-storage "custom field" path. Every catalog entry is now
column-backed on ``items``. The three previously field-value-backed keys
in actual use — ``ring_size``, ``weight_grams``, ``stone_shape`` — gain
dedicated nullable columns on ``items``. The remaining seven legacy
field_value catalog entries (``karat``, ``unit_cost``, ``material``,
``purity_pct``, ``hallmark``, ``gem_type``, ``finishes``, ``expiry_date``)
are dropped from the catalog (code change in ``app/field_catalog.py``);
they were not in use in the dev DB.

``item_field_values`` is wiped and dropped.

``taxonomy_field_defs`` is wiped (per-leaf picks get re-created via the
picker UI after migration) and slimmed: ``name``, ``catalog_key``,
``type``, ``options_json``, ``archived_at`` columns are dropped. The
table now exists purely as a visibility selector — a list of
``(node_id, key)`` pairs identifying which catalog fields show on the
items form/list/CSV for that node and its descendants.

The downgrade is destructive in spirit (the dropped data is gone), but
re-creates the table shapes so the schema is structurally restored.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_promote_standard_fields"
down_revision = "0023_drop_field_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add three new nullable columns to ``items`` for the promoted fields.
    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(sa.Column("ring_size", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column("weight_grams", sa.Numeric(14, 4), nullable=True)
        )
        batch_op.add_column(sa.Column("stone_shape", sa.String(64), nullable=True))

    # 2. Wipe + drop the field-value storage table entirely.
    op.execute(sa.text("DELETE FROM item_field_values"))
    op.drop_table("item_field_values")

    # 3. Wipe + slim the per-leaf field-defs table.
    op.execute(sa.text("DELETE FROM taxonomy_field_defs"))
    with op.batch_alter_table("taxonomy_field_defs") as batch_op:
        batch_op.drop_index("uq_taxonomy_field_defs_node_name")
        batch_op.drop_index("ix_taxonomy_field_defs_archived_at")
        batch_op.drop_index("ix_taxonomy_field_defs_catalog_key")
        batch_op.drop_column("name")
        batch_op.drop_column("catalog_key")
        batch_op.drop_column("type")
        batch_op.drop_column("options_json")
        batch_op.drop_column("archived_at")


def downgrade() -> None:
    # Re-create the dropped columns + index shapes on taxonomy_field_defs.
    # Existing rows survive but lose the visibility-only data we wrote here.
    with op.batch_alter_table("taxonomy_field_defs") as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(255), nullable=True))
        batch_op.add_column(sa.Column("catalog_key", sa.String(64), nullable=True))
        batch_op.add_column(
            sa.Column(
                "type",
                sa.Enum(
                    "text",
                    "number",
                    "decimal",
                    "date",
                    "boolean",
                    "select",
                    "multiselect",
                    name="taxonomy_field_type",
                    native_enum=False,
                    length=16,
                ),
                nullable=True,
            )
        )
        batch_op.add_column(sa.Column("options_json", sa.JSON(), nullable=True))
        batch_op.add_column(
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "uq_taxonomy_field_defs_node_name", ["node_id", "name"], unique=True
        )
        batch_op.create_index("ix_taxonomy_field_defs_archived_at", ["archived_at"])
        batch_op.create_index("ix_taxonomy_field_defs_catalog_key", ["catalog_key"])

    # Re-create the field-value table.
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
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_number", sa.Integer(), nullable=True),
        sa.Column("value_decimal", sa.Numeric(14, 4), nullable=True),
        sa.Column("value_date", sa.Date(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column("value_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Index(
            "uq_item_field_values_item_field_def",
            "item_id",
            "field_def_id",
            unique=True,
        ),
        sa.Index("ix_item_field_values_item_id", "item_id"),
        sa.Index("ix_item_field_values_field_def_id", "field_def_id"),
    )

    # Drop the three promoted columns.
    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_column("stone_shape")
        batch_op.drop_column("weight_grams")
        batch_op.drop_column("ring_size")
