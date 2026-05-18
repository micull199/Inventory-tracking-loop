"""create stone_events ledger

Revision ID: 0027_create_stone_events
Revises: 0026_create_stones
Create Date: 2026-05-15

S1 of the architectural additions spec. Stones have non-quantity lifecycles
(set, unset, sold, returned, lost, relocated, cert_updated,
ownership_changed) so extending ``stock_movements`` would conflate FIFO
qty/value flows with stone state changes. Each transition writes a
``stone_events`` row AND updates the denormalised columns on ``stones`` in
one transaction.

Append-only by mission posture (matches ``stock_movements`` / ``audit_log``):
no ``archived_at``, no update/delete handlers. Corrections are a new event.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0027_create_stone_events"
down_revision = "0026_create_stones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stone_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "stone_id",
            sa.Integer(),
            sa.ForeignKey("stones.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column(
            "from_item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "to_item_id",
            sa.Integer(),
            sa.ForeignKey("items.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "from_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "to_location_id",
            sa.Integer(),
            sa.ForeignKey("locations.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("from_status", sa.String(length=24), nullable=True),
        sa.Column("to_status", sa.String(length=24), nullable=True),
        sa.Column(
            "actor_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("note", sa.String(length=2000), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_stone_events_stone_id", "stone_events", ["stone_id"])
    op.create_index(
        "ix_stone_events_created_at", "stone_events", ["created_at"]
    )
    op.create_index(
        "ix_stone_events_event_type", "stone_events", ["event_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_stone_events_event_type", table_name="stone_events")
    op.drop_index("ix_stone_events_created_at", table_name="stone_events")
    op.drop_index("ix_stone_events_stone_id", table_name="stone_events")
    op.drop_table("stone_events")
