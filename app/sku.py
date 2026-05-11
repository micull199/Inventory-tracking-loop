"""SKU composition + sequence allocation helpers for the taxonomy refinement.

The taxonomy refinement (see ``docs/taxonomy-refinement-plan.md``) introduces
three archetypes that govern an item's SKU shape and where its sequence
allocator lives:

- ``bulk`` / ``unique``       — leaf-anchored. The user-picked leaf owns the
                                 sequence; the SKU is
                                 ``<ancestor-prefixes>-<NNNN>``.
- ``unique_variant``           — sub-cat-anchored. Each item lives on its own
                                 auto-created depth-2 leaf; the depth-1 sub-cat
                                 owns the sequence; the SKU is
                                 ``<root>-<sub>-<NNN>``.

This module is small + focused. The route layer in ``app/items.py`` is the
only consumer for create flow; ``app/taxonomy.py`` consumes ``node_depth`` and
the ancestor helpers for validation.

Cross-dialect locking: ``allocate_sequence`` uses a single
``UPDATE ... RETURNING next_sequence`` statement. SQLite (>= 3.35) and
Postgres both support this, so the same code path serialises concurrent
allocators on the same row without a separate SELECT-FOR-UPDATE round trip.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Archetype, TaxonomyNode

# Defensive depth cap on ancestor / node-depth walks. The taxonomy is at most
# 3 levels deep (depth 0..2); cap at 10 so a corrupted parent_id cycle does
# not spin forever.
_MAX_DEPTH_WALK = 10


def ancestor_chain(db: Session, node: TaxonomyNode) -> list[TaxonomyNode]:
    """Return ``[root, ..., node]`` in top-down order.

    Length is 1 (top-level), 2 (sub-category), or 3 (sub-sub-category) under
    the post-refinement taxonomy. A corrupted parent_id cycle is broken at
    ``_MAX_DEPTH_WALK`` to avoid infinite loops; the partial chain is returned
    truncated rather than raising so callers degrade gracefully.
    """

    chain: list[TaxonomyNode] = [node]
    current = node
    for _ in range(_MAX_DEPTH_WALK):
        if current.parent_id is None:
            break
        parent = db.get(TaxonomyNode, current.parent_id)
        if parent is None:
            break
        chain.append(parent)
        current = parent
    chain.reverse()
    return chain


def node_depth(db: Session, node: TaxonomyNode) -> int:
    """Return the node's depth: 0 (top-level), 1 (sub-cat), 2 (sub-sub-cat).

    Walks the parent_id chain. Capped at ``_MAX_DEPTH_WALK`` to defend against
    a corrupted cycle; under the route-layer invariants the depth is always
    0, 1, or 2.
    """

    depth = 0
    current = node
    for _ in range(_MAX_DEPTH_WALK):
        if current.parent_id is None:
            return depth
        parent = db.get(TaxonomyNode, current.parent_id)
        if parent is None:
            return depth
        depth += 1
        current = parent
    return depth


def effective_archetype(db: Session, node: TaxonomyNode) -> Archetype | None:
    """Return the archetype inherited from the root of this node's tree.

    Archetype is stored only on depth-0 rows; depth-1 + depth-2 rows leave
    it NULL and inherit it at read time. Returns ``None`` only for orphaned
    chains (defensive).

    Safety net: when the resolved depth-0 row has ``archetype IS NULL``
    (legacy fixtures or seed rows that pre-date the refinement and slipped
    in without the explicit assignment), fall back to ``Archetype.BULK``.
    This mirrors the migration 0016 backfill rule and stops the items
    create route from 400ing on rows the rest of the app is happy to read.
    """

    current = node
    for _ in range(_MAX_DEPTH_WALK):
        if current.parent_id is None:
            return current.archetype or Archetype.BULK
        parent = db.get(TaxonomyNode, current.parent_id)
        if parent is None:
            return current.archetype or Archetype.BULK
        current = parent
    return current.archetype or Archetype.BULK


def compose_sku(prefixes: list[str], sequence: int, archetype: Archetype) -> str:
    """Compose the final SKU from the ancestor prefixes + the sequence.

    Behaviour by archetype:

    - ``unique_variant`` — ``prefixes`` already includes the auto-leaf's
      ``sku_prefix`` (which equals ``f"{sequence:03d}"``). Just join them
      with ``"-"`` and return; the sequence is implicit in the trailing
      segment.
    - ``bulk`` / ``unique`` — ``prefixes`` is the ancestor chain of the
      user-picked leaf; append a 4-digit zero-padded ``sequence``.

    Examples (post-refinement)::

        compose_sku(["RTS", "EM", "001"], 1, UNIQUE_VARIANT)  -> "RTS-EM-001"
        compose_sku(["RAW", "SIL", "925"], 8, BULK)            -> "RAW-SIL-925-0008"
        compose_sku(["TOOL"], 1, UNIQUE)                       -> "TOOL-0001"
    """

    base = "-".join(prefixes)
    if archetype == Archetype.UNIQUE_VARIANT:
        return base
    return f"{base}-{sequence:04d}"


def allocate_sequence(db: Session, allocator: TaxonomyNode) -> int:
    """Atomically allocate the next sequence on ``allocator``.

    Semantics: ``next_sequence`` is "the next number to use". The function
    returns the *current* value (the number being allocated) and increments
    the column for the next caller. Implemented as a single
    ``UPDATE ... RETURNING next_sequence`` so the read-and-write is one
    round-trip and atomic at the row level on both SQLite (>= 3.35) and
    Postgres.

    The caller chooses the allocator:

    - bulk / unique items: pass the leaf the item will live on.
    - unique_variant items: pass the depth-1 sub-cat (parent of the auto-leaf).

    Raises ``RuntimeError`` if the UPDATE matched a row count other than 1
    (defensive — under normal operation the ``where id == allocator.id``
    clause matches exactly one row).
    """

    stmt = (
        sa.update(TaxonomyNode)
        .where(TaxonomyNode.id == allocator.id)
        .values(next_sequence=TaxonomyNode.next_sequence + 1)
        .returning(TaxonomyNode.next_sequence)
    )
    result = db.execute(stmt)
    rows = result.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            f"allocate_sequence: expected 1 affected row, got {len(rows)} "
            f"on allocator id={allocator.id}"
        )
    new_next = int(rows[0][0])
    # ``new_next`` is the post-increment value. The allocated sequence is the
    # pre-increment value (what callers see in the SKU). Refresh the in-memory
    # ORM instance so subsequent reads in the same session see the new value.
    db.refresh(allocator)
    return new_next - 1


def create_unique_variant_leaf(db: Session, sub_cat: TaxonomyNode, sequence: int) -> TaxonomyNode:
    """Create + flush a depth-2 auto-leaf under ``sub_cat`` for a unique-variant item.

    The leaf's ``name`` and ``sku_prefix`` both equal ``f"{sequence:03d}"``
    (e.g. ``"001"``). ``archetype`` is left NULL — inherited from the root.
    Caller is responsible for committing.

    Defensive checks:

    - ``sub_cat`` must be at depth 1 (its parent must be a top-level node).
    - The effective archetype of ``sub_cat`` must be ``unique_variant``.

    Raises ``ValueError`` on either violation so the route layer surfaces a
    clear 400 rather than a deferred constraint error.
    """

    if sub_cat.parent_id is None:
        raise ValueError(
            "create_unique_variant_leaf: sub_cat must be at depth 1 "
            "(have a parent), got a top-level node"
        )
    if node_depth(db, sub_cat) != 1:
        raise ValueError(
            "create_unique_variant_leaf: sub_cat must be at depth 1, got "
            f"depth {node_depth(db, sub_cat)}"
        )
    if effective_archetype(db, sub_cat) != Archetype.UNIQUE_VARIANT:
        raise ValueError(
            "create_unique_variant_leaf: effective archetype must be "
            f"unique_variant, got {effective_archetype(db, sub_cat)!r}"
        )

    prefix = f"{sequence:03d}"
    leaf = TaxonomyNode(
        parent_id=sub_cat.id,
        name=prefix,
        sku_prefix=prefix,
        sort_order=sequence,
    )
    db.add(leaf)
    db.flush()
    return leaf
