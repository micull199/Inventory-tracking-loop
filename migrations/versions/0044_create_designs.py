"""create designs table + seed design_code counter

Revision ID: 0044_create_designs
Revises: 0043_add_stock_movements_reason_code_id
Create Date: 2026-05-15

Spec §3 / ADR-003. Splits design IP from the taxonomy tree. Today's
"Emma" is a depth-1 ``TaxonomyNode`` doing triple duty (design IP,
production grouping, SKU prefix); this migration adds the dedicated
home for the design-IP concern.

**Modified scope per ADR-003**:
- ``items.design_id`` FK is *not* added here — held for a follow-up
  slice once operator-entered design metadata is rich enough to
  backfill from.
- Backfill of existing depth-1 unique-variant nodes into ``designs``
  rows is *not* run here. The table starts empty; operators populate
  it via the admin route.
- Two additions over the spec: ``cad_version`` (String) and
  ``cad_updated_at`` (DateTime) — versioning signal for the AU/TH
  multi-location CAD-pull world.

Seeds a ``design_code`` row in ``sequence_counters`` so the
``DSG-NNNN`` allocator (``app.designs.allocate_design_code``) has a
counter to spin on day one — same pattern as ``stone_code``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0044_create_designs"
down_revision = "0043_add_stock_movements_reason_code_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "designs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("design_code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("collection", sa.String(length=64), nullable=True),
        sa.Column("style_family", sa.String(length=16), nullable=True),
        sa.Column("designer", sa.String(length=128), nullable=True),
        sa.Column("cad_file_path", sa.String(length=512), nullable=True),
        sa.Column("cad_version", sa.String(length=32), nullable=True),
        sa.Column("cad_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "default_metal_id",
            sa.Integer(),
            sa.ForeignKey("metal_master.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("intro_date", sa.Date(), nullable=True),
        sa.Column("discontinued_date", sa.Date(), nullable=True),
        sa.Column("standard_cost", sa.Numeric(14, 4), nullable=True),
        sa.Column("notes", sa.String(length=2000), nullable=True),
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
        "uq_designs_design_code", "designs", ["design_code"], unique=True
    )
    op.create_index(
        "ix_designs_default_metal_id", "designs", ["default_metal_id"]
    )
    op.create_index(
        "ix_designs_archived_at", "designs", ["archived_at"]
    )
    op.create_index(
        "ix_designs_discontinued_date", "designs", ["discontinued_date"]
    )

    # Seed the global ``design_code`` counter so ``DSG-NNNN`` allocation
    # has a row to spin on day one. Reuses the ``sequence_counters``
    # infrastructure created in 0026 for ``stone_code``.
    op.bulk_insert(
        sa.table(
            "sequence_counters",
            sa.column("name", sa.String()),
            sa.column("next_value", sa.Integer()),
        ),
        [{"name": "design_code", "next_value": 1}],
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            "DELETE FROM sequence_counters WHERE name = 'design_code'"
        )
    )
    op.drop_index("ix_designs_discontinued_date", table_name="designs")
    op.drop_index("ix_designs_archived_at", table_name="designs")
    op.drop_index("ix_designs_default_metal_id", table_name="designs")
    op.drop_index("uq_designs_design_code", table_name="designs")
    op.drop_table("designs")
