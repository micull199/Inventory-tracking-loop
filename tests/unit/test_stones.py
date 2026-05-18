"""Unit tests for the S1 stones additions.

Covers:
- The new ORM models (``StoneShape``, ``Stone``, ``StoneEvent``,
  ``ItemStone``, ``SequenceCounter``) — defaults and DB-level constraints.
- The new stone-related columns on ``Item``.
- ``app.stones.allocate_stone_code`` — atomic ``UPDATE ... RETURNING``
  allocator that mints ``STN-NNNNNN`` codes off a single global counter.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    Archetype,
    Item,
    ItemStone,
    SequenceCounter,
    Stone,
    StoneEvent,
    StoneOrigin,
    StoneOwnership,
    StonePosition,
    StoneShape,
    StoneStatus,
    StoneType,
    TaxonomyNode,
    TrackingMode,
)
from app.stones import allocate_stone_code


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        # Seed the stone_code counter — the migration does this for the
        # production DB, but ``create_all`` doesn't run migration data.
        session.add(SequenceCounter(name="stone_code", next_value=1))
        session.commit()
        yield session


def _make_shape(db: Session, name: str = "round") -> StoneShape:
    shape = StoneShape(name=name)
    db.add(shape)
    db.commit()
    db.refresh(shape)
    return shape


_NEXT_NODE_PREFIX_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _make_node(db: Session, name: str = "Rings", prefix: str = "RNG") -> TaxonomyNode:
    node = TaxonomyNode(name=name, sku_prefix=prefix, archetype=Archetype.UNIQUE)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_item(db: Session, name: str = "Solitaire 1") -> Item:
    # Each call gets its own depth-0 node so item creation never collides on
    # the unique-name constraint.
    existing = db.execute(select(TaxonomyNode)).scalars().all()
    suffix = _NEXT_NODE_PREFIX_CHARS[len(existing) % len(_NEXT_NODE_PREFIX_CHARS)]
    node = _make_node(db, name=f"Rings-{name}", prefix=f"RN{suffix}")
    item = Item(
        sku=f"RN{suffix}-{name}",
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestStoneShape:
    def test_minimal_shape(self, db: Session) -> None:
        shape = StoneShape(name="round")
        db.add(shape)
        db.commit()
        db.refresh(shape)
        assert shape.id is not None
        assert shape.name == "round"
        assert shape.sort_order == 0
        assert shape.archived_at is None

    def test_name_uniqueness_covers_archived(self, db: Session) -> None:
        # Mirrors Supplier / Location convention: archiving doesn't free the
        # name.
        archived = StoneShape(name="round", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(archived)
        db.commit()

        db.add(StoneShape(name="round"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestStone:
    def test_minimal_stone(self, db: Session) -> None:
        shape = _make_shape(db)
        stone = Stone(
            stone_code="STN-000001",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.50"),
        )
        db.add(stone)
        db.commit()
        db.refresh(stone)

        assert stone.id is not None
        assert stone.origin == StoneOrigin.NATURAL  # server default
        assert stone.ownership == StoneOwnership.OWNED  # server default
        assert stone.status == StoneStatus.AVAILABLE  # server default
        assert stone.archived_at is None

    def test_stone_code_unique(self, db: Session) -> None:
        shape = _make_shape(db)
        db.add(
            Stone(
                stone_code="STN-000001",
                stone_type=StoneType.DIAMOND,
                shape_id=shape.id,
                carat_weight=Decimal("1.00"),
            )
        )
        db.commit()

        db.add(
            Stone(
                stone_code="STN-000001",
                stone_type=StoneType.RUBY,
                shape_id=shape.id,
                carat_weight=Decimal("0.50"),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_partial_cert_uniqueness(self, db: Session) -> None:
        # (lab, cert_number) is unique when both are set. Multiple uncertificated
        # stones (lab/cert NULL) coexist.
        shape = _make_shape(db)
        db.add(
            Stone(
                stone_code="STN-000001",
                stone_type=StoneType.DIAMOND,
                shape_id=shape.id,
                carat_weight=Decimal("1.00"),
                lab="gia",
                cert_number="ABC-123",
            )
        )
        db.add(
            Stone(
                stone_code="STN-000002",
                stone_type=StoneType.DIAMOND,
                shape_id=shape.id,
                carat_weight=Decimal("0.80"),
            )  # no cert — fine
        )
        db.add(
            Stone(
                stone_code="STN-000003",
                stone_type=StoneType.DIAMOND,
                shape_id=shape.id,
                carat_weight=Decimal("0.70"),
            )  # also no cert — also fine
        )
        db.commit()

        # Second stone with same (lab, cert) — must fail.
        db.add(
            Stone(
                stone_code="STN-000004",
                stone_type=StoneType.DIAMOND,
                shape_id=shape.id,
                carat_weight=Decimal("1.20"),
                lab="gia",
                cert_number="ABC-123",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

        # Same cert_number under a DIFFERENT lab — allowed.
        db.add(
            Stone(
                stone_code="STN-000005",
                stone_type=StoneType.DIAMOND,
                shape_id=shape.id,
                carat_weight=Decimal("1.10"),
                lab="igi",
                cert_number="ABC-123",
            )
        )
        db.commit()


class TestStoneEvent:
    def test_minimal_event(self, db: Session) -> None:
        shape = _make_shape(db)
        stone = Stone(
            stone_code="STN-000001",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
        )
        db.add(stone)
        db.commit()

        event = StoneEvent(
            stone_id=stone.id,
            event_type="created",
            to_status=StoneStatus.AVAILABLE,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        assert event.id is not None
        assert event.event_type == "created"
        assert event.to_status == StoneStatus.AVAILABLE


class TestItemStone:
    def test_one_active_stone_per_slot(self, db: Session) -> None:
        # The partial unique on (item_id, position, position_index) where
        # unset_at IS NULL — only one stone occupies a slot at a time.
        shape = _make_shape(db)
        item = _make_item(db)
        stone_a = Stone(
            stone_code="STN-000001",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
        )
        stone_b = Stone(
            stone_code="STN-000002",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("0.80"),
        )
        db.add_all([stone_a, stone_b])
        db.commit()

        db.add(
            ItemStone(
                item_id=item.id,
                stone_id=stone_a.id,
                position=StonePosition.CENTRE,
            )
        )
        db.commit()

        # Same slot, second stone — must fail (both active).
        db.add(
            ItemStone(
                item_id=item.id,
                stone_id=stone_b.id,
                position=StonePosition.CENTRE,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_stone_set_in_at_most_one_item(self, db: Session) -> None:
        shape = _make_shape(db)
        item_a = _make_item(db, name="ring-a")
        item_b = _make_item(db, name="ring-b")
        stone = Stone(
            stone_code="STN-000001",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
        )
        db.add(stone)
        db.commit()

        db.add(
            ItemStone(
                item_id=item_a.id, stone_id=stone.id, position=StonePosition.CENTRE
            )
        )
        db.commit()

        # Same stone in a different item — must fail (only one active link).
        db.add(
            ItemStone(
                item_id=item_b.id, stone_id=stone.id, position=StonePosition.CENTRE
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_unset_then_reset_allowed(self, db: Session) -> None:
        # After unset_at fills in, the partial unique no longer matches —
        # the stone can be re-set into a different item.
        shape = _make_shape(db)
        item_a = _make_item(db, name="ring-a")
        item_b = _make_item(db, name="ring-b")
        stone = Stone(
            stone_code="STN-000001",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
        )
        db.add(stone)
        db.commit()

        link_a = ItemStone(
            item_id=item_a.id, stone_id=stone.id, position=StonePosition.CENTRE
        )
        db.add(link_a)
        db.commit()

        # Unset the first link.
        link_a.unset_at = datetime(2026, 5, 1, tzinfo=UTC)
        db.commit()

        # Now the stone is free — re-set into a different item.
        db.add(
            ItemStone(
                item_id=item_b.id, stone_id=stone.id, position=StonePosition.CENTRE
            )
        )
        db.commit()


class TestItemStoneColumns:
    def test_item_has_new_columns_with_defaults(self, db: Session) -> None:
        item = _make_item(db)
        # Refresh from DB so server defaults are populated.
        db.expire(item)
        assert item.centre_stone_id is None
        assert item.total_carat_weight is None
        assert item.melee_count == 0
        assert item.melee_total_ct == Decimal("0")
        assert item.melee_stone_type is None


class TestComputeItemStoneCosts:
    """Spec §10.3 Strategy A — loaded + owned cost helpers."""

    def test_empty_item_returns_zeros(self, db: Session) -> None:
        from app.stones import compute_item_stone_costs

        node = _make_node(db, name="Plain ring", prefix="PLN")
        item = Item(
            sku="PLN-0001",
            name="Plain",
            taxonomy_node_id=node.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
        )
        db.add(item)
        db.commit()
        result = compute_item_stone_costs(db, item)
        assert result["mount_cost"] == Decimal("0")
        assert result["stone_count"] == 0
        assert result["loaded_cost"] == Decimal("0")
        assert result["owned_cost"] == Decimal("0")

    def test_loaded_excludes_memo_only_in_owned(self, db: Session) -> None:
        """Memo stones contribute to loaded_cost but not owned_cost."""
        from app.stones import _set_stone_into_item, compute_item_stone_costs

        node = _make_node(db, name="Ring", prefix="RG2")
        item = Item(
            sku="RG2-0001",
            name="Set ring",
            taxonomy_node_id=node.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
        )
        db.add(item)
        db.commit()
        shape = _make_shape(db)
        owned = Stone(
            stone_code="STN-OWN",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
            acquisition_cost=Decimal("1000.00"),
            ownership=StoneOwnership.OWNED,
        )
        memo = Stone(
            stone_code="STN-MEM",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("0.20"),
            acquisition_cost=Decimal("250.00"),
            ownership=StoneOwnership.MEMO,
        )
        db.add_all([owned, memo])
        db.commit()
        _set_stone_into_item(
            db, owned, item,
            position=StonePosition.CENTRE, position_index=0, actor=None,
        )
        _set_stone_into_item(
            db, memo, item,
            position=StonePosition.ACCENT_LEFT, position_index=0, actor=None,
        )
        db.commit()
        result = compute_item_stone_costs(db, item)
        assert result["stone_count"] == 2
        # Loaded includes BOTH stones' acquisition_cost.
        assert result["loaded_stones_cost"] == Decimal("1250.00")
        assert result["loaded_cost"] == Decimal("1250.00")
        # Owned excludes the memo stone.
        assert result["owned_stones_cost"] == Decimal("1000.00")
        assert result["owned_cost"] == Decimal("1000.00")


class TestStoneCodeAllocator:
    def test_allocates_sequential_codes(self, db: Session) -> None:
        first = allocate_stone_code(db)
        second = allocate_stone_code(db)
        third = allocate_stone_code(db)
        assert first == "STN-000001"
        assert second == "STN-000002"
        assert third == "STN-000003"

    def test_persists_counter(self, db: Session) -> None:
        allocate_stone_code(db)
        allocate_stone_code(db)
        db.commit()
        counter = db.execute(
            select(SequenceCounter).where(SequenceCounter.name == "stone_code")
        ).scalar_one()
        # next_value advanced past the two allocations (1 → 3).
        assert counter.next_value == 3

    def test_raises_when_counter_missing(self, db: Session) -> None:
        # Remove the seeded counter row to simulate a missing migration.
        db.execute(
            SequenceCounter.__table__.delete().where(
                SequenceCounter.name == "stone_code"
            )
        )
        db.commit()
        with pytest.raises(RuntimeError, match="counter row missing"):
            allocate_stone_code(db)
