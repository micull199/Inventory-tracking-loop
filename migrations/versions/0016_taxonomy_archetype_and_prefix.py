"""add archetype, sku_prefix, next_sequence to taxonomy_nodes; assigned_sequence to items

Revision ID: 0016_taxonomy_archetype_and_prefix
Revises: 0015_taxonomy_defaults_json
Create Date: 2026-05-11

Schema-level slice of the taxonomy refinement (see
``docs/taxonomy-refinement-plan.md``).

Adds:

- ``taxonomy_nodes.archetype`` — nullable String(16). Stored only on depth-0
  rows after backfill (NULL at depth 1+2; the application code resolves the
  effective archetype by walking up to the root).
- ``taxonomy_nodes.sku_prefix`` — String(8), NOT NULL after the backfill.
  Uppercase 1-8 alphanumeric chars. Composed with ancestor prefixes to build
  an item's SKU.
- ``taxonomy_nodes.next_sequence`` — Integer NOT NULL default 1. Per-leaf
  SKU sequence allocator; seeded by parsing existing item SKUs.
- ``items.assigned_sequence`` — nullable Integer. Set on new items going
  forward; existing items get NULL.

Plus two partial unique indexes mirroring the name-uniqueness pair:

- ``uq_taxonomy_sku_prefix_top``  — unique ``(sku_prefix)`` where
  ``parent_id IS NULL``.
- ``uq_taxonomy_sku_prefix_child`` — unique ``(parent_id, sku_prefix)`` where
  ``parent_id IS NOT NULL``.

Backfill rules:

1. ``sku_prefix`` — derived from ``name``. First try the first 3 alphabetic
   chars uppercased; fall back to first 3 alphanumeric chars; fall back to
   ``"CAT"``. Truncated to 8 chars. Sibling collisions are disambiguated by
   appending ``2``, ``3``, … (still capped at 8 chars).
2. ``archetype`` — every depth-0 row gets ``"bulk"`` (the safe default
   matching today's quantity-tracked behaviour). Depth-1+ rows stay NULL.
3. ``next_sequence`` — for each row whose ``sku_prefix`` matches the leading
   segment of any ``items.sku``, set ``next_sequence = max(numeric suffix) +
   1``. Rows with no matching items keep the server default ``1``.

Migrations operate on raw SQL via ``op.get_bind()`` and ``sqlalchemy.text()``;
they deliberately do not import the live ORM models (which would be
brittle to future model changes).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "0016_taxonomy_archetype_and_prefix"
down_revision = "0015_taxonomy_defaults_json"
branch_labels = None
depends_on = None


# Maximum length allowed by the ``sku_prefix`` column. Kept in sync with the
# ORM model's ``String(8)`` declaration.
_MAX_PREFIX_LEN = 8


def _candidate_prefix(name: str | None) -> str:
    """Derive a candidate ``sku_prefix`` from a node name.

    Mirrors ``app.models._derive_sku_prefix``. Kept in-file (rather than
    imported) so the migration is independent of future model refactors.
    """

    raw = name or ""
    alpha = "".join(ch for ch in raw if ch.isalpha())[:3]
    if alpha:
        candidate = alpha.upper()
    else:
        alnum = "".join(ch for ch in raw if ch.isalnum())[:3]
        candidate = alnum.upper() if alnum else "CAT"
    return candidate[:_MAX_PREFIX_LEN]


def _disambiguate(base: str, taken: set[str]) -> str:
    """Append a numeric suffix until ``base`` is unique within ``taken``.

    Cap at ``_MAX_PREFIX_LEN``: the base shrinks from the right as the
    numeric suffix grows so the final prefix never exceeds the column width.
    """

    if base not in taken:
        return base
    n = 2
    while True:
        suffix = str(n)
        allowed = _MAX_PREFIX_LEN - len(suffix)
        trimmed = base[:allowed] if allowed > 0 else ""
        candidate = (trimmed + suffix)[:_MAX_PREFIX_LEN]
        # If somehow trimming produces a collision-free candidate that's already
        # in ``taken`` (extreme cases like very short ``_MAX_PREFIX_LEN``),
        # keep incrementing.
        if candidate not in taken:
            return candidate
        n += 1


def _parse_int_suffix(sku: str, prefix: str) -> int | None:
    """Return the integer suffix of ``sku`` after ``"<prefix>-"``, or None.

    Treats the trailing dash-separated segment as the sequence; if it isn't
    a pure integer, returns None. Examples (prefix=``RAW``):

    - ``"RAW-0001"`` → 1
    - ``"RAW-SIL-0012"`` → 12 (last segment parses)
    - ``"RAW-FOO"`` → None
    """

    expected = f"{prefix}-"
    if not sku.startswith(expected):
        return None
    last = sku.rsplit("-", 1)[-1]
    try:
        return int(last)
    except (TypeError, ValueError):
        return None


def upgrade() -> None:
    # Step 1: add the new columns. ``sku_prefix`` lands nullable so the
    # backfill can populate it before we tighten the constraint.
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.add_column(sa.Column("archetype", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("sku_prefix", sa.String(length=8), nullable=True))
        batch_op.add_column(
            sa.Column(
                "next_sequence",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            )
        )

    with op.batch_alter_table("items") as batch_op:
        batch_op.add_column(
            sa.Column("assigned_sequence", sa.Integer(), nullable=True)
        )

    connection = op.get_bind()

    # Step 2: backfill ``sku_prefix`` for every taxonomy node, in
    # ``(parent_id NULLS FIRST, id)`` order. Disambiguate sibling collisions
    # in-memory: the partial unique indexes don't exist yet during the
    # backfill, so we maintain a per-parent ``set`` of prefixes already
    # assigned this run.
    rows = connection.execute(
        sa.text(
            "SELECT id, parent_id, name FROM taxonomy_nodes "
            "ORDER BY (parent_id IS NULL) DESC, parent_id ASC, id ASC"
        )
    ).fetchall()

    # parent_id (None for top-level) → set of assigned prefixes.
    seen: dict[int | None, set[str]] = {}
    for row_id, parent_id, name in rows:
        scope = seen.setdefault(parent_id, set())
        base = _candidate_prefix(name)
        prefix = _disambiguate(base, scope)
        scope.add(prefix)
        connection.execute(
            sa.text("UPDATE taxonomy_nodes SET sku_prefix = :prefix WHERE id = :id"),
            {"prefix": prefix, "id": row_id},
        )

    # Step 3: backfill ``archetype`` — every depth-0 row gets ``bulk``.
    # Depth-1+ stays NULL (inherited at read time).
    connection.execute(
        sa.text(
            "UPDATE taxonomy_nodes SET archetype = 'bulk' WHERE parent_id IS NULL"
        )
    )

    # Step 4: backfill ``next_sequence`` from existing item SKUs per node.
    # Re-read the rows after the prefix backfill so we have the assigned
    # prefix in-memory.
    refreshed = connection.execute(
        sa.text("SELECT id, sku_prefix FROM taxonomy_nodes")
    ).fetchall()
    for node_id, prefix in refreshed:
        item_skus = connection.execute(
            sa.text(
                "SELECT sku FROM items WHERE taxonomy_node_id = :node_id"
            ),
            {"node_id": node_id},
        ).fetchall()
        if not item_skus:
            continue
        max_seq = 0
        for (sku,) in item_skus:
            parsed = _parse_int_suffix(sku or "", prefix or "")
            if parsed is not None and parsed > max_seq:
                max_seq = parsed
        if max_seq > 0:
            connection.execute(
                sa.text(
                    "UPDATE taxonomy_nodes SET next_sequence = :seq WHERE id = :id"
                ),
                {"seq": max_seq + 1, "id": node_id},
            )

    # Step 5: tighten ``sku_prefix`` to NOT NULL now that every row is
    # populated.
    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.alter_column("sku_prefix", existing_type=sa.String(length=8), nullable=False)

    # Step 6: create the two partial unique indexes. Same shape as the
    # name-uniqueness pair (one for top-level, one for children).
    op.create_index(
        "uq_taxonomy_sku_prefix_top",
        "taxonomy_nodes",
        ["sku_prefix"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NULL"),
        postgresql_where=sa.text("parent_id IS NULL"),
    )
    op.create_index(
        "uq_taxonomy_sku_prefix_child",
        "taxonomy_nodes",
        ["parent_id", "sku_prefix"],
        unique=True,
        sqlite_where=sa.text("parent_id IS NOT NULL"),
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Reverse order: drop indexes, then ``items`` column, then taxonomy
    # columns in reverse-add order.
    op.drop_index("uq_taxonomy_sku_prefix_child", table_name="taxonomy_nodes")
    op.drop_index("uq_taxonomy_sku_prefix_top", table_name="taxonomy_nodes")

    with op.batch_alter_table("items") as batch_op:
        batch_op.drop_column("assigned_sequence")

    with op.batch_alter_table("taxonomy_nodes") as batch_op:
        batch_op.drop_column("next_sequence")
        batch_op.drop_column("sku_prefix")
        batch_op.drop_column("archetype")
