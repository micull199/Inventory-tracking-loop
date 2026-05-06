"""Unit tests for the ``ItemUnit`` ORM model (I3).

Cover defaults, status enum round-trip, location FK, and DB-level uniqueness
of ``(item_id, serial_or_label)`` across active + archived rows. Route-level
tests live in ``tests/integration/test_item_units_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Item, ItemUnit, ItemUnitStatus, Location, TaxonomyNode, TrackingMode


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    with SessionLocal() as session:
        yield session


def _node(db: Session, name: str = "Tools") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _item(
    db: Session,
    *,
    sku: str = "T-001",
    name: str = "Mould",
    tracking_mode: TrackingMode = TrackingMode.UNIQUE,
) -> Item:
    node = _node(db, name=f"Cat-{sku}")
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=tracking_mode,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestItemUnitDefaults:
    def test_minimal_unit_has_required_fields_only(self, db: Session) -> None:
        item = _item(db)
        unit = ItemUnit(
            item_id=item.id,
            serial_or_label="SN-0001",
            status=ItemUnitStatus.AVAILABLE,
        )
        db.add(unit)
        db.commit()
        db.refresh(unit)

        assert unit.id is not None
        assert unit.item_id == item.id
        assert unit.serial_or_label == "SN-0001"
        assert unit.status is ItemUnitStatus.AVAILABLE
        assert unit.location_id is None
        assert unit.archived_at is None
        assert unit.created_at is not None
        assert unit.updated_at is not None

    def test_status_lost_round_trips(self, db: Session) -> None:
        item = _item(db)
        unit = ItemUnit(
            item_id=item.id,
            serial_or_label="SN-LOST",
            status=ItemUnitStatus.LOST,
        )
        db.add(unit)
        db.commit()
        db.refresh(unit)
        assert unit.status is ItemUnitStatus.LOST

    def test_location_fk_round_trips(self, db: Session) -> None:
        item = _item(db)
        loc = Location(name="Workshop bench")
        db.add(loc)
        db.commit()
        db.refresh(loc)

        unit = ItemUnit(
            item_id=item.id,
            serial_or_label="SN-LOC",
            status=ItemUnitStatus.AVAILABLE,
            location_id=loc.id,
        )
        db.add(unit)
        db.commit()
        db.refresh(unit)
        assert unit.location_id == loc.id


class TestSerialUniqueness:
    def test_serial_unique_within_item(self, db: Session) -> None:
        item = _item(db)
        db.add(
            ItemUnit(
                item_id=item.id,
                serial_or_label="SN-0001",
                status=ItemUnitStatus.AVAILABLE,
            )
        )
        db.commit()
        db.add(
            ItemUnit(
                item_id=item.id,
                serial_or_label="SN-0001",
                status=ItemUnitStatus.AVAILABLE,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_serial_uniqueness_spans_archived(self, db: Session) -> None:
        """Archiving must not free the serial. Operator renames or unarchives."""
        item = _item(db)
        db.add(
            ItemUnit(
                item_id=item.id,
                serial_or_label="SN-0001",
                status=ItemUnitStatus.AVAILABLE,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.commit()
        db.add(
            ItemUnit(
                item_id=item.id,
                serial_or_label="SN-0001",
                status=ItemUnitStatus.AVAILABLE,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_two_items_can_share_serial(self, db: Session) -> None:
        """Different items can have units with the same label — labels are item-scoped."""
        item_a = _item(db, sku="A", name="A")
        item_b = _item(db, sku="B", name="B")
        db.add_all(
            [
                ItemUnit(
                    item_id=item_a.id,
                    serial_or_label="SHARED",
                    status=ItemUnitStatus.AVAILABLE,
                ),
                ItemUnit(
                    item_id=item_b.id,
                    serial_or_label="SHARED",
                    status=ItemUnitStatus.AVAILABLE,
                ),
            ]
        )
        db.commit()  # should succeed.

    def test_different_serials_on_same_item_coexist(self, db: Session) -> None:
        item = _item(db)
        db.add_all(
            [
                ItemUnit(
                    item_id=item.id,
                    serial_or_label="SN-001",
                    status=ItemUnitStatus.AVAILABLE,
                ),
                ItemUnit(
                    item_id=item.id,
                    serial_or_label="SN-002",
                    status=ItemUnitStatus.AVAILABLE,
                ),
                ItemUnit(
                    item_id=item.id,
                    serial_or_label="SN-003",
                    status=ItemUnitStatus.LOST,
                ),
            ]
        )
        db.commit()


class TestItemUnitRequiredColumns:
    def test_item_id_is_required(self, db: Session) -> None:
        unit = ItemUnit(  # type: ignore[call-arg]
            serial_or_label="X",
            status=ItemUnitStatus.AVAILABLE,
        )
        db.add(unit)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_serial_or_label_is_required(self, db: Session) -> None:
        item = _item(db)
        unit = ItemUnit(  # type: ignore[call-arg]
            item_id=item.id,
            status=ItemUnitStatus.AVAILABLE,
        )
        db.add(unit)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
