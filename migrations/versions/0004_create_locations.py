"""create locations table

Revision ID: 0004_create_locations
Revises: 0003_create_suppliers
Create Date: 2026-05-06

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0004_create_locations"
down_revision = "0003_create_suppliers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
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
        sa.UniqueConstraint("name", name="uq_locations_name"),
    )
    op.create_index("ix_locations_archived_at", "locations", ["archived_at"])


def downgrade() -> None:
    op.drop_index("ix_locations_archived_at", table_name="locations")
    op.drop_table("locations")
