"""Unit tests for S5 free-text-to-lookup additions.

Covers:
- ``Unit`` (``unit_master``): defaults, code uniqueness (active + archived).
- ``ReasonCode`` (``reason_codes``): (movement_type, code) uniqueness.
- ``Item.unit_id`` + ``StockMovement.reason_code_id`` FK round-trip.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    Archetype,
    Item,
    MovementType,
    ReasonCode,
    StockMovement,
    TaxonomyNode,
    TrackingMode,
    Unit,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _make_unit(db: Session, code: str = "g") -> Unit:
    u = Unit(code=code, name=f"Test {code}")
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_node(db: Session, name: str = "Cat", prefix: str = "CT") -> TaxonomyNode:
    node = TaxonomyNode(name=name, sku_prefix=prefix, archetype=Archetype.BULK)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_item(db: Session, node: TaxonomyNode, name: str = "Bar") -> Item:
    item = Item(
        sku=f"{node.sku_prefix}-{name}",
        name=name,
        taxonomy_node_id=node.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestUnit:
    def test_minimal_unit(self, db: Session) -> None:
        u = _make_unit(db)
        assert u.id is not None
        assert u.code == "g"
        assert u.sort_order == 0
        assert u.archived_at is None

    def test_code_unique_across_archived(self, db: Session) -> None:
        archived = Unit(
            code="g", name="g-archived", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db.add(archived)
        db.commit()
        db.add(Unit(code="g", name="g-new"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_item_unit_id_round_trip(self, db: Session) -> None:
        u = _make_unit(db, code="kg")
        node = _make_node(db)
        item = _make_item(db, node)
        item.unit_id = u.id
        db.commit()
        db.refresh(item)
        assert item.unit_id == u.id
        # The legacy freetext column is unaffected — S5 is additive.
        assert item.unit == "g"


class TestReasonCode:
    def test_minimal_reason(self, db: Session) -> None:
        r = ReasonCode(movement_type="out", code="sale", label="Sale")
        db.add(r)
        db.commit()
        db.refresh(r)
        assert r.id is not None

    def test_type_code_uniqueness_covers_archived(self, db: Session) -> None:
        archived = ReasonCode(
            movement_type="out",
            code="sale",
            label="Sale (old)",
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(archived)
        db.commit()
        # Same (type, code) as archived row — must fail.
        db.add(ReasonCode(movement_type="out", code="sale", label="Sale (new)"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_same_code_different_movement_type(self, db: Session) -> None:
        # ``damaged`` can exist on both ``out`` and ``adjustment`` — same
        # code, different movement type, unique constraint scoped per type.
        db.add(ReasonCode(movement_type="out", code="damaged", label="Damaged (out)"))
        db.add(
            ReasonCode(
                movement_type="adjustment", code="damaged", label="Damaged (adj)"
            )
        )
        db.commit()

    def test_stock_movement_reason_code_id_round_trip(self, db: Session) -> None:
        node = _make_node(db)
        item = _make_item(db, node)
        reason = ReasonCode(movement_type="out", code="sale", label="Sale")
        db.add(reason)
        db.commit()
        db.refresh(reason)

        mv = StockMovement(
            item_id=item.id,
            type=MovementType.OUT,
            qty=Decimal("1"),
            reason_code_id=reason.id,
            reason="customer purchase, special order",  # freetext long tail
        )
        db.add(mv)
        db.commit()
        db.refresh(mv)
        assert mv.reason_code_id == reason.id
        # Freetext stays alongside the structured code.
        assert mv.reason == "customer purchase, special order"
