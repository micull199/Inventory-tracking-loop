"""backfill TaxonomyFieldDef.catalog_key from pre-catalog rows

Revision ID: 0022_backfill_catalog_key
Revises: 0021_taxonomy_field_def_catalog_key
Create Date: 2026-05-13

Slice 4 of the catalog-driven taxonomy refactor. Migration 0021 added a
nullable ``catalog_key`` column; this one fills it in for every existing
``TaxonomyFieldDef`` row that pre-dates the catalog flow.

Matching strategy, in order:

1. ``key.lower()`` exact match against a catalog entry's ``key``.
2. ``name.lower()`` exact match against a catalog entry's ``label.lower()``.
3. No match: archive the row (set ``archived_at = now``) and write an audit
   row with ``actor_id = NULL`` (system event), ``action =
   "taxonomy_field_def.archived_during_catalog_backfill"``. The same
   system-actor convention is already used for bootstrap admin promotion.

Items pointing at archived defs keep their ``ItemFieldValue`` rows;
``field_def.archived_at`` does not cascade (see ``app/models.py`` docstring
on ``ItemFieldValue``).

Why archive rather than DROP: ``item_field_values.field_def_id`` is an FK,
so deleting the def would either cascade-delete history (bad) or fail
(blocking the migration). Archiving keeps history intact and visible to
the "Historical fields" block in slice 5's item detail view.

This migration is idempotent: re-running on an already-backfilled DB is a
no-op because every row will have ``catalog_key`` populated.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

from app.field_catalog import FIELD_CATALOG

revision = "0022_backfill_catalog_key"
down_revision = "0021_taxonomy_field_def_catalog_key"
branch_labels = None
depends_on = None


def _catalog_lookup_by_key() -> dict[str, str]:
    return {entry.key.lower(): entry.key for entry in FIELD_CATALOG}


def _catalog_lookup_by_label() -> dict[str, str]:
    return {entry.label.lower(): entry.key for entry in FIELD_CATALOG}


def upgrade() -> None:
    bind = op.get_bind()
    by_key = _catalog_lookup_by_key()
    by_label = _catalog_lookup_by_label()
    now = datetime.now(UTC).isoformat()

    rows = bind.execute(
        sa.text(
            "SELECT id, node_id, name, key "
            "FROM taxonomy_field_defs "
            "WHERE catalog_key IS NULL"
        )
    ).fetchall()

    matched = 0
    archived = 0
    for row in rows:
        fd_id, node_id, name, key = row.id, row.node_id, row.name, row.key
        match = by_key.get((key or "").lower()) or by_label.get((name or "").lower())
        if match is not None:
            bind.execute(
                sa.text(
                    "UPDATE taxonomy_field_defs "
                    "SET catalog_key = :catalog_key "
                    "WHERE id = :id"
                ),
                {"catalog_key": match, "id": fd_id},
            )
            matched += 1
            continue

        # No catalog match — archive and audit. Skip already-archived rows
        # so a re-run is idempotent.
        bind.execute(
            sa.text(
                "UPDATE taxonomy_field_defs "
                "SET archived_at = :now "
                "WHERE id = :id AND archived_at IS NULL"
            ),
            {"now": now, "id": fd_id},
        )
        bind.execute(
            sa.text(
                "INSERT INTO audit_log "
                "(actor_id, action, entity_type, entity_id, "
                " before_json, after_json) "
                "VALUES (NULL, :action, 'taxonomy_field_def', :id, "
                "        :before, :after)"
            ),
            {
                "action": "taxonomy_field_def.archived_during_catalog_backfill",
                "id": fd_id,
                "before": _json({"key": key, "name": name, "node_id": node_id}),
                "after": _json(
                    {
                        "archived_at": now,
                        "reason": "no catalog match",
                    }
                ),
            },
        )
        archived += 1

    # Stats are informational only — Alembic's standard logger picks them up.
    print(  # noqa: T201 — migration progress is fine to print
        f"[0022_backfill_catalog_key] matched={matched} "
        f"archived_unmatched={archived} total_seen={len(rows)}"
    )


def downgrade() -> None:
    # We do not attempt to un-archive rows the backfill archived — the
    # audit log row is immutable, and "unarchive" without context would
    # leave the system inconsistent. Clearing ``catalog_key`` is enough
    # for slice 6 to skip its NOT NULL tightening if rolled back.
    bind = op.get_bind()
    bind.execute(sa.text("UPDATE taxonomy_field_defs SET catalog_key = NULL"))


def _json(payload: dict[str, object]) -> str:
    """Stable JSON encoding — sort keys so audit diffs are reproducible."""

    import json

    return json.dumps(payload, sort_keys=True, default=str)
