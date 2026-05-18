"""create app_settings + add tracking_trigger to stones + item_stones covering index

Revision ID: 0045_app_settings_and_tracking_trigger
Revises: 0044_create_designs
Create Date: 2026-05-18

Spec §10.1 (melee threshold tightening) + §10.3 (loaded cost reporting),
both confirmed by Michael with modifications:

- **Cost floor**: $500 AUD, stored in ``app_settings`` (not hardcoded) so
  managers can tune the threshold without a redeploy.
- **Coloured stone carat threshold**: 0.50 ct, same posture.
- **``tracking_trigger`` enum** on stones with four legal values:
  ``cert | coloured_stone_threshold | cost_threshold | manual_override``.
  Records *why* the stone is being tracked rather than left as melee.
- **``tracking_override_reason`` String(255)** — required only when
  trigger = ``manual_override``. Enforced in the route layer.
- **Covering partial index** ``ix_item_stones_active_item_id`` on
  ``item_stones(item_id) WHERE unset_at IS NULL`` so the loaded-cost
  per-ring query scans only the active set instead of the full link
  history.

All columns are nullable so legacy stones (created before this slice)
keep working without a backfill. The route layer enforces tracking_trigger
on every fresh ``stone.created``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0045_app_settings_and_tracking_trigger"
down_revision = "0044_create_designs"
branch_labels = None
depends_on = None


_SEED_SETTINGS: tuple[tuple[str, str, str], ...] = (
    (
        "stones.tracking.cost_floor_aud",
        "500",
        "AUD threshold above which an uncertificated stone is tracked "
        "as a stone rather than melee (spec §10.1).",
    ),
    (
        "stones.tracking.coloured_stone_ct_threshold",
        "0.50",
        "Carat threshold above which an uncertificated coloured stone "
        "is tracked rather than left as melee (spec §10.1).",
    ),
)


def upgrade() -> None:
    op.create_table(
        "app_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value", sa.String(length=2000), nullable=False),
        sa.Column("description", sa.String(length=2000), nullable=True),
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
        "uq_app_settings_key", "app_settings", ["key"], unique=True
    )

    # Seed the two thresholds spec §10.1 references. Operators tune via
    # SQL UPDATE (admin UI for app_settings is a follow-up slice).
    rows = [
        {"key": key, "value": value, "description": desc}
        for key, value, desc in _SEED_SETTINGS
    ]
    op.bulk_insert(
        sa.table(
            "app_settings",
            sa.column("key", sa.String()),
            sa.column("value", sa.String()),
            sa.column("description", sa.String()),
        ),
        rows,
    )

    # Stones tracking-trigger columns. Both nullable so existing rows
    # don't need a backfill — they read as "legacy / unmarked" until
    # someone edits them through the form.
    with op.batch_alter_table("stones") as batch_op:
        batch_op.add_column(
            sa.Column("tracking_trigger", sa.String(length=32), nullable=True)
        )
        batch_op.add_column(
            sa.Column("tracking_override_reason", sa.String(length=255), nullable=True)
        )

    # Covering partial index for the active-stones-by-item lookup the
    # loaded-cost / owned-cost reporter and the items detail page both
    # use. SQLite and Postgres both support partial indexes via the
    # ``sqlite_where`` / ``postgresql_where`` knobs alembic surfaces.
    op.create_index(
        "ix_item_stones_active_item_id",
        "item_stones",
        ["item_id"],
        sqlite_where=sa.text("unset_at IS NULL"),
        postgresql_where=sa.text("unset_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_item_stones_active_item_id", table_name="item_stones"
    )
    with op.batch_alter_table("stones") as batch_op:
        batch_op.drop_column("tracking_override_reason")
        batch_op.drop_column("tracking_trigger")
    op.drop_index("uq_app_settings_key", table_name="app_settings")
    op.drop_table("app_settings")
