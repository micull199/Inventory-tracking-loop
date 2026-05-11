"""Unit tests for ``app.sku``.

Covers the public surface:

- ``ancestor_chain`` returns top-down ancestors.
- ``node_depth`` returns 0/1/2 with a defensive cap on cycles.
- ``effective_archetype`` walks to the root + falls back to ``BULK`` for
  legacy rows with a null archetype.
- ``compose_sku`` produces the right shape per archetype, including the
  unique-variant special case (leaf prefix IS the sequence segment).
- ``allocate_sequence`` is atomic + monotonic per allocator; two distinct
  allocators don't collide.
- ``create_unique_variant_leaf`` enforces depth + archetype guards.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.models import Archetype, TaxonomyNode
from app.sku import (
    allocate_sequence,
    ancestor_chain,
    compose_sku,
    create_unique_variant_leaf,
    effective_archetype,
    node_depth,
)

# ---------------------------------------------------------------------------
# Fixtures: small builder helpers
# ---------------------------------------------------------------------------


def _make_top(
    db: Session,
    *,
    name: str = "Top",
    archetype: Archetype | None = Archetype.BULK,
    sku_prefix: str = "TOP",
) -> TaxonomyNode:
    node = TaxonomyNode(
        name=name,
        parent_id=None,
        archetype=archetype,
        sku_prefix=sku_prefix,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_child(db: Session, parent: TaxonomyNode, *, name: str, sku_prefix: str) -> TaxonomyNode:
    node = TaxonomyNode(name=name, parent_id=parent.id, sku_prefix=sku_prefix)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


# ---------------------------------------------------------------------------
# compose_sku
# ---------------------------------------------------------------------------


class TestComposeSku:
    def test_bulk_depth_0_appends_4_digit_sequence(self) -> None:
        assert compose_sku(["TOOL"], 1, Archetype.BULK) == "TOOL-0001"

    def test_bulk_depth_1_two_segments_plus_sequence(self) -> None:
        assert compose_sku(["RAW", "SIL"], 8, Archetype.BULK) == "RAW-SIL-0008"

    def test_bulk_depth_2_three_segments_plus_sequence(self) -> None:
        assert compose_sku(["RAW", "SIL", "925"], 42, Archetype.BULK) == "RAW-SIL-925-0042"

    def test_unique_uses_4_digit_sequence(self) -> None:
        assert compose_sku(["RING"], 7, Archetype.UNIQUE) == "RING-0007"

    def test_unique_variant_three_segments_no_extra_sequence(self) -> None:
        """Unique-variant trees: the leaf prefix already equals
        ``f"{sequence:03d}"`` so the composed SKU is simply the joined
        chain. The sequence is implicit in the trailing segment.
        """
        assert compose_sku(["RTS", "EM", "001"], 1, Archetype.UNIQUE_VARIANT) == "RTS-EM-001"

    def test_unique_variant_higher_sequence(self) -> None:
        assert compose_sku(["RTS", "EM", "042"], 42, Archetype.UNIQUE_VARIANT) == "RTS-EM-042"

    def test_bulk_zero_pads_sequence_to_four_digits(self) -> None:
        assert compose_sku(["X"], 5, Archetype.BULK) == "X-0005"
        assert compose_sku(["X"], 9999, Archetype.BULK) == "X-9999"

    def test_bulk_does_not_truncate_large_sequence(self) -> None:
        # 5+ digits stay intact (no silent truncation). The convention is
        # "at least 4 digits" rather than "exactly 4".
        assert compose_sku(["X"], 12345, Archetype.BULK) == "X-12345"


# ---------------------------------------------------------------------------
# ancestor_chain + node_depth
# ---------------------------------------------------------------------------


class TestAncestorChain:
    def test_top_level_chain_has_one_entry(self, db_session: Session) -> None:
        top = _make_top(db_session, name="Top", sku_prefix="TOP")
        chain = ancestor_chain(db_session, top)
        assert [n.id for n in chain] == [top.id]

    def test_depth_1_chain_in_top_down_order(self, db_session: Session) -> None:
        top = _make_top(db_session, name="Top", sku_prefix="TOP")
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        chain = ancestor_chain(db_session, sub)
        assert [n.id for n in chain] == [top.id, sub.id]

    def test_depth_2_chain(self, db_session: Session) -> None:
        top = _make_top(db_session, name="Top", sku_prefix="TOP")
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        leaf = _make_child(db_session, sub, name="Leaf", sku_prefix="LEA")
        chain = ancestor_chain(db_session, leaf)
        assert [n.id for n in chain] == [top.id, sub.id, leaf.id]


class TestNodeDepth:
    def test_top_level_depth_zero(self, db_session: Session) -> None:
        top = _make_top(db_session)
        assert node_depth(db_session, top) == 0

    def test_sub_cat_depth_one(self, db_session: Session) -> None:
        top = _make_top(db_session)
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        assert node_depth(db_session, sub) == 1

    def test_sub_sub_cat_depth_two(self, db_session: Session) -> None:
        top = _make_top(db_session)
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        leaf = _make_child(db_session, sub, name="Leaf", sku_prefix="LEA")
        assert node_depth(db_session, leaf) == 2


# ---------------------------------------------------------------------------
# effective_archetype
# ---------------------------------------------------------------------------


class TestEffectiveArchetype:
    def test_top_level_returns_own_archetype(self, db_session: Session) -> None:
        top = _make_top(db_session, archetype=Archetype.UNIQUE_VARIANT)
        assert effective_archetype(db_session, top) == Archetype.UNIQUE_VARIANT

    def test_sub_cat_inherits_root_archetype(self, db_session: Session) -> None:
        top = _make_top(db_session, archetype=Archetype.UNIQUE)
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        assert effective_archetype(db_session, sub) == Archetype.UNIQUE

    def test_sub_sub_cat_inherits_root_archetype(self, db_session: Session) -> None:
        top = _make_top(db_session, archetype=Archetype.UNIQUE_VARIANT)
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        leaf = _make_child(db_session, sub, name="Leaf", sku_prefix="LEA")
        assert effective_archetype(db_session, leaf) == Archetype.UNIQUE_VARIANT

    def test_top_level_with_null_archetype_falls_back_to_bulk(self, db_session: Session) -> None:
        """Legacy fixtures + seed rows that pre-date the refinement may
        have ``archetype IS NULL`` at the root. The helper falls back to
        ``BULK`` (matching the migration backfill) so the items create
        route doesn't 400 on those rows.
        """
        top = _make_top(db_session, archetype=None)
        assert effective_archetype(db_session, top) == Archetype.BULK


# ---------------------------------------------------------------------------
# allocate_sequence
# ---------------------------------------------------------------------------


class TestAllocateSequence:
    def test_first_call_returns_one_and_bumps_next_to_two(self, db_session: Session) -> None:
        top = _make_top(db_session)
        assert top.next_sequence == 1
        seq = allocate_sequence(db_session, top)
        db_session.refresh(top)
        assert seq == 1
        assert top.next_sequence == 2

    def test_sequential_calls_return_increasing_values(self, db_session: Session) -> None:
        top = _make_top(db_session)
        seqs = [allocate_sequence(db_session, top) for _ in range(5)]
        assert seqs == [1, 2, 3, 4, 5]
        db_session.refresh(top)
        assert top.next_sequence == 6

    def test_two_allocators_do_not_collide(self, db_session: Session) -> None:
        a = _make_top(db_session, name="A", sku_prefix="A")
        b = _make_top(db_session, name="B", sku_prefix="B")
        # Interleave allocations across two allocator rows. Each row's
        # sequence advances independently.
        s_a1 = allocate_sequence(db_session, a)
        s_b1 = allocate_sequence(db_session, b)
        s_a2 = allocate_sequence(db_session, a)
        s_b2 = allocate_sequence(db_session, b)
        assert (s_a1, s_a2) == (1, 2)
        assert (s_b1, s_b2) == (1, 2)


# ---------------------------------------------------------------------------
# create_unique_variant_leaf
# ---------------------------------------------------------------------------


class TestCreateUniqueVariantLeaf:
    def test_mints_leaf_with_zero_padded_name_and_prefix(self, db_session: Session) -> None:
        top = _make_top(
            db_session,
            name="RTS",
            archetype=Archetype.UNIQUE_VARIANT,
            sku_prefix="RTS",
        )
        sub = _make_child(db_session, top, name="Emma", sku_prefix="EM")
        leaf = create_unique_variant_leaf(db_session, sub, 7)
        assert leaf.parent_id == sub.id
        assert leaf.name == "007"
        assert leaf.sku_prefix == "007"
        # Archetype is left NULL on the new row — inherited at read time.
        assert leaf.archetype is None

    def test_rejects_top_level_sub_cat(self, db_session: Session) -> None:
        top = _make_top(db_session, archetype=Archetype.UNIQUE_VARIANT, sku_prefix="X")
        with pytest.raises(ValueError, match="must be at depth 1"):
            create_unique_variant_leaf(db_session, top, 1)

    def test_rejects_non_unique_variant_archetype(self, db_session: Session) -> None:
        top = _make_top(db_session, archetype=Archetype.BULK, sku_prefix="TOP")
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SUB")
        with pytest.raises(ValueError, match="effective archetype must be"):
            create_unique_variant_leaf(db_session, sub, 1)

    def test_three_digit_zero_pad(self, db_session: Session) -> None:
        top = _make_top(
            db_session,
            archetype=Archetype.UNIQUE_VARIANT,
            sku_prefix="UV",
        )
        sub = _make_child(db_session, top, name="Sub", sku_prefix="SU")
        leaf = create_unique_variant_leaf(db_session, sub, 999)
        assert leaf.name == "999"
        assert leaf.sku_prefix == "999"
