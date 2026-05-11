"""Unit tests for the M1 cost / movement ORM models.

Cover defaults, enum round-trips, FK requirements, and that ``qty_remaining``
can be decremented independently of ``qty_received``. The engine logic itself
lives in ``test_cost_engine.py``; these tests are about the storage layer.
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
    CostLayer,
    CostLayerConsumption,
    CostLayerSource,
    Item,
    MovementType,
    StockMovement,
    TaxonomyNode,
    TrackingMode,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    with SessionLocal() as session:
        yield session


def _item(db: Session, *, sku: str = "RM-001") -> Item:
    # Explicit ``sku_prefix`` keyed off ``sku`` so sibling factories don't
    # share the name-derived default ``CAT`` prefix and clash on the
    # partial unique index.
    alnum = "".join(c for c in sku if c.isalnum())[:8] or "TST"
    node = TaxonomyNode(name=f"Cat-{sku}", sku_prefix=alnum)
    db.add(node)
    db.commit()
    db.refresh(node)
    item = Item(
        sku=sku,
        name=f"Item {sku}",
        taxonomy_node_id=node.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _movement(
    db: Session,
    *,
    item: Item,
    type_: MovementType = MovementType.IN,
    qty: Decimal = Decimal("10"),
) -> StockMovement:
    m = StockMovement(item_id=item.id, type=type_, qty=qty)
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


class TestStockMovementDefaults:
    def test_minimal_movement_has_required_fields_only(self, db: Session) -> None:
        item = _item(db)
        m = StockMovement(item_id=item.id, type=MovementType.IN, qty=Decimal("5"))
        db.add(m)
        db.commit()
        db.refresh(m)

        assert m.id is not None
        assert m.item_id == item.id
        assert m.type is MovementType.IN
        assert m.qty == Decimal("5")
        assert m.item_unit_id is None
        assert m.user_id is None
        assert m.reason is None
        assert m.note is None
        assert m.po_id is None
        assert m.stock_take_id is None
        assert m.total_cost is None
        assert m.created_at is not None

    def test_each_movement_type_round_trips(self, db: Session) -> None:
        item = _item(db)
        for t in MovementType:
            m = StockMovement(item_id=item.id, type=t, qty=Decimal("1"))
            db.add(m)
        db.commit()

        types = {m.type for m in db.query(StockMovement).all()}
        assert types == set(MovementType)

    def test_item_id_is_required(self, db: Session) -> None:
        m = StockMovement(  # type: ignore[call-arg]
            type=MovementType.IN, qty=Decimal("1")
        )
        db.add(m)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_qty_is_required(self, db: Session) -> None:
        item = _item(db)
        m = StockMovement(item_id=item.id, type=MovementType.IN)  # type: ignore[call-arg]
        db.add(m)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_total_cost_round_trips_decimal(self, db: Session) -> None:
        item = _item(db)
        m = StockMovement(
            item_id=item.id,
            type=MovementType.IN,
            qty=Decimal("3"),
            total_cost=Decimal("123.4567"),
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        assert m.total_cost == Decimal("123.4567")

    def test_po_and_stock_take_ids_are_plain_integers(self, db: Session) -> None:
        """No FK constraint in M1 — those tables don't exist yet."""
        item = _item(db)
        m = StockMovement(
            item_id=item.id,
            type=MovementType.IN,
            qty=Decimal("1"),
            po_id=999_999,  # arbitrary; would fail an FK but there isn't one
            stock_take_id=999_998,
        )
        db.add(m)
        db.commit()
        db.refresh(m)
        assert m.po_id == 999_999
        assert m.stock_take_id == 999_998


class TestCostLayerDefaults:
    def test_minimal_layer_round_trips(self, db: Session) -> None:
        item = _item(db)
        movement = _movement(db, item=item)
        received = datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC)
        layer = CostLayer(
            item_id=item.id,
            qty_received=Decimal("10"),
            qty_remaining=Decimal("10"),
            unit_cost=Decimal("2.50"),
            received_at=received,
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement.id,
        )
        db.add(layer)
        db.commit()
        db.refresh(layer)

        assert layer.id is not None
        assert layer.qty_received == Decimal("10")
        assert layer.qty_remaining == Decimal("10")
        assert layer.unit_cost == Decimal("2.50")
        # SQLite drops tzinfo on round-trip; compare naive forms.
        assert layer.received_at.replace(tzinfo=None) == received.replace(tzinfo=None)
        assert layer.source is CostLayerSource.MANUAL_IN
        assert layer.source_movement_id == movement.id
        assert layer.created_at is not None

    def test_each_layer_source_round_trips(self, db: Session) -> None:
        item = _item(db)
        movement = _movement(db, item=item)
        for s in CostLayerSource:
            db.add(
                CostLayer(
                    item_id=item.id,
                    qty_received=Decimal("1"),
                    qty_remaining=Decimal("1"),
                    unit_cost=Decimal("1"),
                    received_at=datetime.now(UTC),
                    source=s,
                    source_movement_id=movement.id,
                )
            )
        db.commit()

        sources = {layer.source for layer in db.query(CostLayer).all()}
        assert sources == set(CostLayerSource)

    def test_qty_remaining_independent_of_received(self, db: Session) -> None:
        """Decrementing ``qty_remaining`` must not touch ``qty_received``."""
        item = _item(db)
        movement = _movement(db, item=item)
        layer = CostLayer(
            item_id=item.id,
            qty_received=Decimal("10"),
            qty_remaining=Decimal("10"),
            unit_cost=Decimal("1"),
            received_at=datetime.now(UTC),
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement.id,
        )
        db.add(layer)
        db.commit()

        layer.qty_remaining = Decimal("3")
        db.commit()
        db.refresh(layer)
        assert layer.qty_received == Decimal("10")
        assert layer.qty_remaining == Decimal("3")

    def test_item_id_is_required(self, db: Session) -> None:
        item = _item(db)
        movement = _movement(db, item=item)
        layer = CostLayer(  # type: ignore[call-arg]
            qty_received=Decimal("1"),
            qty_remaining=Decimal("1"),
            unit_cost=Decimal("1"),
            received_at=datetime.now(UTC),
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement.id,
        )
        db.add(layer)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_source_movement_id_is_required(self, db: Session) -> None:
        item = _item(db)
        layer = CostLayer(  # type: ignore[call-arg]
            item_id=item.id,
            qty_received=Decimal("1"),
            qty_remaining=Decimal("1"),
            unit_cost=Decimal("1"),
            received_at=datetime.now(UTC),
            source=CostLayerSource.MANUAL_IN,
        )
        db.add(layer)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestCostLayerConsumptionDefaults:
    def test_minimal_consumption_round_trips(self, db: Session) -> None:
        item = _item(db)
        movement_in = _movement(db, item=item, type_=MovementType.IN)
        movement_out = _movement(
            db, item=item, type_=MovementType.OUT, qty=Decimal("3")
        )
        layer = CostLayer(
            item_id=item.id,
            qty_received=Decimal("10"),
            qty_remaining=Decimal("10"),
            unit_cost=Decimal("2"),
            received_at=datetime.now(UTC),
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement_in.id,
        )
        db.add(layer)
        db.commit()
        db.refresh(layer)

        consumption = CostLayerConsumption(
            layer_id=layer.id,
            movement_id=movement_out.id,
            qty_consumed=Decimal("3"),
            unit_cost_at_consumption=Decimal("2"),
        )
        db.add(consumption)
        db.commit()
        db.refresh(consumption)

        assert consumption.id is not None
        assert consumption.layer_id == layer.id
        assert consumption.movement_id == movement_out.id
        assert consumption.qty_consumed == Decimal("3")
        assert consumption.unit_cost_at_consumption == Decimal("2")
        assert consumption.created_at is not None

    def test_layer_id_is_required(self, db: Session) -> None:
        item = _item(db)
        movement = _movement(db, item=item)
        consumption = CostLayerConsumption(  # type: ignore[call-arg]
            movement_id=movement.id,
            qty_consumed=Decimal("1"),
            unit_cost_at_consumption=Decimal("1"),
        )
        db.add(consumption)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_movement_id_is_required(self, db: Session) -> None:
        item = _item(db)
        movement_in = _movement(db, item=item)
        layer = CostLayer(
            item_id=item.id,
            qty_received=Decimal("1"),
            qty_remaining=Decimal("1"),
            unit_cost=Decimal("1"),
            received_at=datetime.now(UTC),
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement_in.id,
        )
        db.add(layer)
        db.commit()

        consumption = CostLayerConsumption(  # type: ignore[call-arg]
            layer_id=layer.id,
            qty_consumed=Decimal("1"),
            unit_cost_at_consumption=Decimal("1"),
        )
        db.add(consumption)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_two_consumptions_can_share_a_movement(self, db: Session) -> None:
        """A consume crossing N layers writes N rows for the same movement."""
        item = _item(db)
        movement_in = _movement(db, item=item)
        movement_out = _movement(db, item=item, type_=MovementType.OUT)
        layer_a = CostLayer(
            item_id=item.id,
            qty_received=Decimal("5"),
            qty_remaining=Decimal("0"),
            unit_cost=Decimal("1"),
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement_in.id,
        )
        layer_b = CostLayer(
            item_id=item.id,
            qty_received=Decimal("5"),
            qty_remaining=Decimal("3"),
            unit_cost=Decimal("2"),
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
            source=CostLayerSource.MANUAL_IN,
            source_movement_id=movement_in.id,
        )
        db.add_all([layer_a, layer_b])
        db.commit()
        db.refresh(layer_a)
        db.refresh(layer_b)

        db.add_all(
            [
                CostLayerConsumption(
                    layer_id=layer_a.id,
                    movement_id=movement_out.id,
                    qty_consumed=Decimal("5"),
                    unit_cost_at_consumption=Decimal("1"),
                ),
                CostLayerConsumption(
                    layer_id=layer_b.id,
                    movement_id=movement_out.id,
                    qty_consumed=Decimal("2"),
                    unit_cost_at_consumption=Decimal("2"),
                ),
            ]
        )
        db.commit()

        rows = (
            db.query(CostLayerConsumption)
            .filter(CostLayerConsumption.movement_id == movement_out.id)
            .all()
        )
        assert len(rows) == 2
        assert {r.layer_id for r in rows} == {layer_a.id, layer_b.id}
