"""create metal_master lookup + seed canonical alloys

Revision ID: 0030_create_metal_master
Revises: 0029_add_items_stone_columns
Create Date: 2026-05-15

S2 of the architectural additions spec. Lookup of metal alloys — required
for precious-metal accounting (gold-pool reconciliation), costing (gold
price moves daily, see ``MetalSpotPrice``), customer-facing display and
Thailand transfer-pricing declarations.

Seed values are the common UC inventory alloys: 9/14/18 yellow / white /
rose gold, platinum 950, palladium 950, sterling silver. Operators add
more via the future admin route. Same archive-doesn't-free-the-code
convention as ``suppliers`` / ``locations`` / ``stone_shape_master``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0030_create_metal_master"
down_revision = "0029_add_items_stone_columns"
branch_labels = None
depends_on = None


# (metal_code, name, alloy_family, karat, purity_pct, colour, density_g_per_cc,
#  hallmark_stamp). Densities are mid-range alloy values; managers may refine
# them per-supplier later.
_SEED_METALS: tuple[tuple[str, str, str, int | None, str, str, str | None, str | None], ...] = (
    ("9KYG", "9ct Yellow Gold", "gold", 9, "37.500", "yellow", "11.300", "375"),
    ("9KWG", "9ct White Gold", "gold", 9, "37.500", "white", "11.500", "375"),
    ("9KRG", "9ct Rose Gold", "gold", 9, "37.500", "rose", "11.300", "375"),
    ("14KYG", "14ct Yellow Gold", "gold", 14, "58.500", "yellow", "13.000", "585"),
    ("14KWG", "14ct White Gold", "gold", 14, "58.500", "white", "13.000", "585"),
    ("14KRG", "14ct Rose Gold", "gold", 14, "58.500", "rose", "13.000", "585"),
    ("18KYG", "18ct Yellow Gold", "gold", 18, "75.000", "yellow", "15.500", "750"),
    ("18KWG", "18ct White Gold", "gold", 18, "75.000", "white", "15.500", "750"),
    ("18KRG", "18ct Rose Gold", "gold", 18, "75.000", "rose", "15.500", "750"),
    ("22KYG", "22ct Yellow Gold", "gold", 22, "91.700", "yellow", "17.700", "917"),
    ("24KYG", "24ct Yellow Gold", "gold", 24, "99.900", "yellow", "19.300", "999"),
    ("PLAT950", "Platinum 950", "platinum", None, "95.000", "platinum", "21.450", "PLAT950"),
    ("PD950", "Palladium 950", "palladium", None, "95.000", "palladium", "11.500", "PD950"),
    ("SS", "Sterling Silver", "silver", None, "92.500", "silver", "10.490", "925"),
)


def upgrade() -> None:
    op.create_table(
        "metal_master",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("metal_code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("alloy_family", sa.String(length=16), nullable=False),
        sa.Column("karat", sa.Integer(), nullable=True),
        sa.Column("purity_pct", sa.Numeric(6, 3), nullable=False),
        sa.Column("colour", sa.String(length=16), nullable=False),
        sa.Column("density_g_per_cc", sa.Numeric(8, 3), nullable=True),
        sa.Column("hallmark_stamp", sa.String(length=16), nullable=True),
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
        "uq_metal_master_code", "metal_master", ["metal_code"], unique=True
    )
    op.create_index(
        "ix_metal_master_archived_at", "metal_master", ["archived_at"]
    )

    rows = [
        {
            "metal_code": code,
            "name": name,
            "alloy_family": family,
            "karat": karat,
            "purity_pct": purity,
            "colour": colour,
            "density_g_per_cc": density,
            "hallmark_stamp": stamp,
        }
        for (code, name, family, karat, purity, colour, density, stamp) in _SEED_METALS
    ]
    op.bulk_insert(
        sa.table(
            "metal_master",
            sa.column("metal_code", sa.String()),
            sa.column("name", sa.String()),
            sa.column("alloy_family", sa.String()),
            sa.column("karat", sa.Integer()),
            sa.column("purity_pct", sa.Numeric(6, 3)),
            sa.column("colour", sa.String()),
            sa.column("density_g_per_cc", sa.Numeric(8, 3)),
            sa.column("hallmark_stamp", sa.String()),
        ),
        rows,
    )


def downgrade() -> None:
    op.drop_index("ix_metal_master_archived_at", table_name="metal_master")
    op.drop_index("uq_metal_master_code", table_name="metal_master")
    op.drop_table("metal_master")
