"""Unit tests for the FIFO cost engine (M1).

Three public functions: ``record_receipt``, ``consume_fifo``, ``open_value``.
The engine is pure — it operates on SQLAlchemy sessions but never makes HTTP
calls or writes audit-log rows. Each test below exercises one rule of the
contract documented in ``app/cost_engine.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from freezegun import freeze_time
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.cost_engine import (
    InsufficientStockError,
    consume_fifo,
    open_value,
    record_receipt,
)
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
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _item(db: Session, *, sku: str = "RM-001") -> Item:
    # ``sku_prefix`` derives from ``name`` by default — pass an explicit
    # value scoped on ``sku`` so two ``_item`` calls in the same test get
    # distinct prefixes and don't collide on the partial unique index.
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


def _make_movement(db: Session, *, item: Item, type_: MovementType, qty: Decimal) -> StockMovement:
    """Mimic a route handler: create a movement, flush so it has an id, then
    pass it to the engine. The route layer would commit on success."""
    m = StockMovement(item_id=item.id, type=type_, qty=qty)
    db.add(m)
    db.flush()
    return m


# ---------------------------------------------------------------------------
# record_receipt
# ---------------------------------------------------------------------------


class TestRecordReceipt:
    def test_creates_layer_with_correct_fields(self, db: Session) -> None:
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("10"))
        received = datetime(2026, 5, 6, 9, 0, 0, tzinfo=UTC)

        layer = record_receipt(
            db,
            item=item,
            qty=Decimal("10"),
            unit_cost=Decimal("2.50"),
            source=CostLayerSource.MANUAL_IN,
            movement=movement,
            received_at=received,
        )
        db.commit()

        assert layer.id is not None
        assert layer.item_id == item.id
        assert layer.qty_received == Decimal("10")
        assert layer.qty_remaining == Decimal("10")
        assert layer.unit_cost == Decimal("2.50")
        # SQLite drops tzinfo on round-trip; compare naive forms.
        assert layer.received_at.replace(tzinfo=None) == received.replace(tzinfo=None)
        assert layer.source is CostLayerSource.MANUAL_IN
        assert layer.source_movement_id == movement.id

    def test_increments_item_current_qty(self, db: Session) -> None:
        item = _item(db)
        assert item.current_qty == Decimal("0")
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("7"))

        record_receipt(
            db,
            item=item,
            qty=Decimal("7"),
            unit_cost=Decimal("3"),
            source=CostLayerSource.MANUAL_IN,
            movement=movement,
        )
        db.commit()
        db.refresh(item)
        assert item.current_qty == Decimal("7")

    def test_sets_movement_total_cost(self, db: Session) -> None:
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("4"))

        record_receipt(
            db,
            item=item,
            qty=Decimal("4"),
            unit_cost=Decimal("12.5"),
            source=CostLayerSource.MANUAL_IN,
            movement=movement,
        )
        db.commit()
        db.refresh(movement)
        assert movement.total_cost == Decimal("50")

    def test_received_at_defaults_to_now(self, db: Session) -> None:
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("1"))
        before = datetime.now(UTC).replace(tzinfo=None)

        layer = record_receipt(
            db,
            item=item,
            qty=Decimal("1"),
            unit_cost=Decimal("1"),
            source=CostLayerSource.MANUAL_IN,
            movement=movement,
        )
        db.commit()
        after = datetime.now(UTC).replace(tzinfo=None)
        # SQLite drops tzinfo on round-trip; compare against naive bounds.
        received_naive = layer.received_at.replace(tzinfo=None)
        assert before - timedelta(seconds=1) <= received_naive <= after + timedelta(seconds=1)

    def test_rejects_zero_qty(self, db: Session) -> None:
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("0"))
        with pytest.raises(ValueError, match="qty must be positive"):
            record_receipt(
                db,
                item=item,
                qty=Decimal("0"),
                unit_cost=Decimal("1"),
                source=CostLayerSource.MANUAL_IN,
                movement=movement,
            )

    def test_rejects_negative_qty(self, db: Session) -> None:
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("-1"))
        with pytest.raises(ValueError, match="qty must be positive"):
            record_receipt(
                db,
                item=item,
                qty=Decimal("-1"),
                unit_cost=Decimal("1"),
                source=CostLayerSource.MANUAL_IN,
                movement=movement,
            )

    def test_rejects_negative_unit_cost(self, db: Session) -> None:
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("1"))
        with pytest.raises(ValueError, match="unit_cost cannot be negative"):
            record_receipt(
                db,
                item=item,
                qty=Decimal("1"),
                unit_cost=Decimal("-1"),
                source=CostLayerSource.MANUAL_IN,
                movement=movement,
            )

    def test_zero_unit_cost_is_allowed(self, db: Session) -> None:
        """Gifted / sample stock has a real qty but zero cost basis."""
        item = _item(db)
        movement = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("5"))

        layer = record_receipt(
            db,
            item=item,
            qty=Decimal("5"),
            unit_cost=Decimal("0"),
            source=CostLayerSource.MANUAL_IN,
            movement=movement,
        )
        db.commit()
        assert layer.unit_cost == Decimal("0")
        db.refresh(movement)
        assert movement.total_cost == Decimal("0")

    def test_requires_flushed_movement(self, db: Session) -> None:
        item = _item(db)
        movement = StockMovement(item_id=item.id, type=MovementType.IN, qty=Decimal("1"))
        # Not flushed: movement.id is None.
        with pytest.raises(ValueError, match="movement must be flushed"):
            record_receipt(
                db,
                item=item,
                qty=Decimal("1"),
                unit_cost=Decimal("1"),
                source=CostLayerSource.MANUAL_IN,
                movement=movement,
            )


# ---------------------------------------------------------------------------
# consume_fifo
# ---------------------------------------------------------------------------


class TestConsumeFifoSingleLayer:
    def test_partial_consumption(self, db: Session) -> None:
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("10"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("10"),
            unit_cost=Decimal("3"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        db.commit()

        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("4"))
        total = consume_fifo(db, item=item, qty=Decimal("4"), movement=out_mvt)
        db.commit()

        assert total == Decimal("12")
        db.refresh(item)
        assert item.current_qty == Decimal("6")
        layer = db.query(CostLayer).one()
        assert layer.qty_received == Decimal("10")
        assert layer.qty_remaining == Decimal("6")
        consumptions = db.query(CostLayerConsumption).all()
        assert len(consumptions) == 1
        c = consumptions[0]
        assert c.layer_id == layer.id
        assert c.movement_id == out_mvt.id
        assert c.qty_consumed == Decimal("4")
        assert c.unit_cost_at_consumption == Decimal("3")
        db.refresh(out_mvt)
        assert out_mvt.total_cost == Decimal("12")

    def test_exact_layer_consumption_leaves_zero_remaining(self, db: Session) -> None:
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("5"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("5"),
            unit_cost=Decimal("1"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        db.commit()

        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("5"))
        total = consume_fifo(db, item=item, qty=Decimal("5"), movement=out_mvt)
        db.commit()

        assert total == Decimal("5")
        db.refresh(item)
        assert item.current_qty == Decimal("0")
        layer = db.query(CostLayer).one()
        # Layer is NOT deleted — part of audit history.
        assert layer.qty_remaining == Decimal("0")


class TestConsumeFifoMultiLayer:
    def test_spans_two_layers_oldest_first(self, db: Session) -> None:
        item = _item(db)
        # Two receipts at different times, different unit costs.
        in_a = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("4"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("4"),
            unit_cost=Decimal("2"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_a,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        in_b = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("6"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("6"),
            unit_cost=Decimal("5"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_b,
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db.commit()

        # Consume 7: takes all 4 from layer A (@ 2 = 8) + 3 from layer B (@ 5 = 15) → 23.
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("7"))
        total = consume_fifo(db, item=item, qty=Decimal("7"), movement=out_mvt)
        db.commit()

        assert total == Decimal("23")
        db.refresh(item)
        assert item.current_qty == Decimal("3")

        layers = db.query(CostLayer).order_by(CostLayer.received_at.asc()).all()
        assert layers[0].qty_remaining == Decimal("0")
        assert layers[1].qty_remaining == Decimal("3")

        consumptions = db.query(CostLayerConsumption).order_by(CostLayerConsumption.id.asc()).all()
        assert len(consumptions) == 2
        assert consumptions[0].layer_id == layers[0].id
        assert consumptions[0].qty_consumed == Decimal("4")
        assert consumptions[0].unit_cost_at_consumption == Decimal("2")
        assert consumptions[1].layer_id == layers[1].id
        assert consumptions[1].qty_consumed == Decimal("3")
        assert consumptions[1].unit_cost_at_consumption == Decimal("5")

    def test_exhausted_layer_is_skipped(self, db: Session) -> None:
        """A layer with qty_remaining=0 must not appear in the FIFO walk."""
        item = _item(db)
        # Layer A entirely consumed.
        in_a = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("3"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("3"),
            unit_cost=Decimal("10"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_a,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        out_drain = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("3"))
        consume_fifo(db, item=item, qty=Decimal("3"), movement=out_drain)
        db.commit()

        # New layer B at a different cost.
        in_b = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("4"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("4"),
            unit_cost=Decimal("7"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_b,
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db.commit()

        # Consume 2: must come from B at 7, not A (which is exhausted).
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("2"))
        total = consume_fifo(db, item=item, qty=Decimal("2"), movement=out_mvt)
        db.commit()
        assert total == Decimal("14")

        # The exhausted layer must NOT have a consumption row written for this
        # movement — only the active one was tapped.
        consumptions = (
            db.query(CostLayerConsumption)
            .filter(CostLayerConsumption.movement_id == out_mvt.id)
            .all()
        )
        assert len(consumptions) == 1
        assert consumptions[0].qty_consumed == Decimal("2")
        assert consumptions[0].unit_cost_at_consumption == Decimal("7")

    def test_ties_broken_by_id_when_received_at_identical(self, db: Session) -> None:
        """Two layers at the exact same instant — older id is consumed first."""
        item = _item(db)
        with freeze_time("2026-05-06T12:00:00Z"):
            in_a = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("2"))
            layer_a = record_receipt(
                db,
                item=item,
                qty=Decimal("2"),
                unit_cost=Decimal("1"),
                source=CostLayerSource.MANUAL_IN,
                movement=in_a,
            )
            in_b = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("2"))
            layer_b = record_receipt(
                db,
                item=item,
                qty=Decimal("2"),
                unit_cost=Decimal("9"),
                source=CostLayerSource.MANUAL_IN,
                movement=in_b,
            )
            db.commit()
            assert layer_a.received_at == layer_b.received_at
            assert layer_a.id < layer_b.id

        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("2"))
        total = consume_fifo(db, item=item, qty=Decimal("2"), movement=out_mvt)
        db.commit()
        # If id-tiebreak works, we drain layer_a (@ 1) entirely → cost 2.
        # If it didn't, we'd drain whichever the DB returns first; the assertion
        # keeps the contract honest.
        assert total == Decimal("2")
        db.refresh(layer_a)
        db.refresh(layer_b)
        assert layer_a.qty_remaining == Decimal("0")
        assert layer_b.qty_remaining == Decimal("2")


class TestConsumeFifoErrors:
    def test_over_consumption_raises(self, db: Session) -> None:
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("5"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("5"),
            unit_cost=Decimal("2"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        db.commit()

        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("10"))
        with pytest.raises(InsufficientStockError) as exc:
            consume_fifo(db, item=item, qty=Decimal("10"), movement=out_mvt)
        assert exc.value.requested == Decimal("10")
        assert exc.value.available == Decimal("5")
        assert exc.value.item_id == item.id

    def test_over_consumption_is_atomic(self, db: Session) -> None:
        """No layers / qtys / consumptions touched after the raise."""
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("5"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("5"),
            unit_cost=Decimal("2"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        db.commit()
        db.refresh(item)
        original_qty = item.current_qty
        layer = db.query(CostLayer).one()
        original_remaining = layer.qty_remaining

        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("99"))
        with pytest.raises(InsufficientStockError):
            consume_fifo(db, item=item, qty=Decimal("99"), movement=out_mvt)

        db.refresh(item)
        db.refresh(layer)
        assert item.current_qty == original_qty
        assert layer.qty_remaining == original_remaining
        assert (
            db.query(CostLayerConsumption)
            .filter(CostLayerConsumption.movement_id == out_mvt.id)
            .count()
            == 0
        )
        assert out_mvt.total_cost is None

    def test_no_layers_at_all_raises(self, db: Session) -> None:
        item = _item(db)
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("1"))
        with pytest.raises(InsufficientStockError) as exc:
            consume_fifo(db, item=item, qty=Decimal("1"), movement=out_mvt)
        assert exc.value.available == Decimal("0")

    def test_zero_qty_raises(self, db: Session) -> None:
        item = _item(db)
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("0"))
        with pytest.raises(ValueError, match="qty must be positive"):
            consume_fifo(db, item=item, qty=Decimal("0"), movement=out_mvt)

    def test_negative_qty_raises(self, db: Session) -> None:
        item = _item(db)
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("-1"))
        with pytest.raises(ValueError, match="qty must be positive"):
            consume_fifo(db, item=item, qty=Decimal("-1"), movement=out_mvt)

    def test_requires_flushed_movement(self, db: Session) -> None:
        item = _item(db)
        movement = StockMovement(item_id=item.id, type=MovementType.OUT, qty=Decimal("1"))
        with pytest.raises(ValueError, match="movement must be flushed"):
            consume_fifo(db, item=item, qty=Decimal("1"), movement=movement)


class TestConsumeFifoIsolation:
    def test_sibling_items_do_not_cross_consume(self, db: Session) -> None:
        item_a = _item(db, sku="A")
        item_b = _item(db, sku="B")

        in_a = _make_movement(db, item=item_a, type_=MovementType.IN, qty=Decimal("10"))
        record_receipt(
            db,
            item=item_a,
            qty=Decimal("10"),
            unit_cost=Decimal("1"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_a,
        )
        db.commit()

        # Item B has no stock — consume should fail without touching A's.
        out_b = _make_movement(db, item=item_b, type_=MovementType.OUT, qty=Decimal("1"))
        with pytest.raises(InsufficientStockError):
            consume_fifo(db, item=item_b, qty=Decimal("1"), movement=out_b)

        db.refresh(item_a)
        assert item_a.current_qty == Decimal("10")
        layer_a = db.query(CostLayer).filter(CostLayer.item_id == item_a.id).one()
        assert layer_a.qty_remaining == Decimal("10")


# ---------------------------------------------------------------------------
# open_value
# ---------------------------------------------------------------------------


class TestOpenValue:
    def test_no_layers_returns_zero(self, db: Session) -> None:
        item = _item(db)
        assert open_value(db, item) == Decimal("0")

    def test_single_layer(self, db: Session) -> None:
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("4"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("4"),
            unit_cost=Decimal("2.5"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        db.commit()
        assert open_value(db, item) == Decimal("10.0")

    def test_multi_layer_sums(self, db: Session) -> None:
        item = _item(db)
        in_a = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("3"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("3"),
            unit_cost=Decimal("2"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_a,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        in_b = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("5"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("5"),
            unit_cost=Decimal("4"),
            source=CostLayerSource.PO_RECEIPT,
            movement=in_b,
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db.commit()
        # 3 * 2 + 5 * 4 = 6 + 20 = 26
        assert open_value(db, item) == Decimal("26")

    def test_exhausted_layer_excluded(self, db: Session) -> None:
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("3"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("3"),
            unit_cost=Decimal("10"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("3"))
        consume_fifo(db, item=item, qty=Decimal("3"), movement=out_mvt)
        db.commit()
        # Layer is exhausted (qty_remaining=0) → excluded from open_value.
        assert open_value(db, item) == Decimal("0")

    def test_partial_layer_included(self, db: Session) -> None:
        item = _item(db)
        in_mvt = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("10"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("10"),
            unit_cost=Decimal("3"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_mvt,
        )
        out_mvt = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("4"))
        consume_fifo(db, item=item, qty=Decimal("4"), movement=out_mvt)
        db.commit()
        # 6 remaining at 3 each = 18.
        assert open_value(db, item) == Decimal("18")

    def test_isolation_between_items(self, db: Session) -> None:
        item_a = _item(db, sku="A")
        item_b = _item(db, sku="B")
        in_a = _make_movement(db, item=item_a, type_=MovementType.IN, qty=Decimal("2"))
        record_receipt(
            db,
            item=item_a,
            qty=Decimal("2"),
            unit_cost=Decimal("100"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_a,
        )
        in_b = _make_movement(db, item=item_b, type_=MovementType.IN, qty=Decimal("3"))
        record_receipt(
            db,
            item=item_b,
            qty=Decimal("3"),
            unit_cost=Decimal("1"),
            source=CostLayerSource.MANUAL_IN,
            movement=in_b,
        )
        db.commit()
        assert open_value(db, item_a) == Decimal("200")
        assert open_value(db, item_b) == Decimal("3")


# ---------------------------------------------------------------------------
# Combined scenarios
# ---------------------------------------------------------------------------


class TestCombinedScenarios:
    def test_receipt_consume_receipt_consume(self, db: Session) -> None:
        """End-to-end inventory cycle — verifies running net + open_value."""
        item = _item(db)

        # In: 10 @ 2 → qty=10, value=20.
        m1 = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("10"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("10"),
            unit_cost=Decimal("2"),
            source=CostLayerSource.MANUAL_IN,
            movement=m1,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.commit()
        db.refresh(item)
        assert item.current_qty == Decimal("10")
        assert open_value(db, item) == Decimal("20")

        # Out: 4 → qty=6, cost=8, value=12 (6 @ 2).
        m2 = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("4"))
        cost_out = consume_fifo(db, item=item, qty=Decimal("4"), movement=m2)
        db.commit()
        db.refresh(item)
        assert cost_out == Decimal("8")
        assert item.current_qty == Decimal("6")
        assert open_value(db, item) == Decimal("12")

        # In: 5 @ 5 → qty=11, value=12 + 25 = 37.
        m3 = _make_movement(db, item=item, type_=MovementType.IN, qty=Decimal("5"))
        record_receipt(
            db,
            item=item,
            qty=Decimal("5"),
            unit_cost=Decimal("5"),
            source=CostLayerSource.PO_RECEIPT,
            movement=m3,
            received_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
        db.commit()
        db.refresh(item)
        assert item.current_qty == Decimal("11")
        assert open_value(db, item) == Decimal("37")

        # Out: 8 → drains all 6 of layer A (cost 12) + 2 from layer B at 5
        # (cost 10) = 22. qty=3 remaining (all from layer B at 5 = 15).
        m4 = _make_movement(db, item=item, type_=MovementType.OUT, qty=Decimal("8"))
        cost_out2 = consume_fifo(db, item=item, qty=Decimal("8"), movement=m4)
        db.commit()
        db.refresh(item)
        assert cost_out2 == Decimal("22")
        assert item.current_qty == Decimal("3")
        assert open_value(db, item) == Decimal("15")

    def test_positive_adjustment_then_negative_adjustment(self, db: Session) -> None:
        """Adjustments use the same engine paths as in/out movements."""
        item = _item(db)

        # Positive adjustment: 7 @ 4 → layer of source positive_adjustment.
        m_pos = _make_movement(db, item=item, type_=MovementType.ADJUSTMENT, qty=Decimal("7"))
        layer = record_receipt(
            db,
            item=item,
            qty=Decimal("7"),
            unit_cost=Decimal("4"),
            source=CostLayerSource.POSITIVE_ADJUSTMENT,
            movement=m_pos,
        )
        db.commit()
        assert layer.source is CostLayerSource.POSITIVE_ADJUSTMENT
        db.refresh(item)
        assert item.current_qty == Decimal("7")

        # Negative adjustment: 2 → consume FIFO.
        m_neg = _make_movement(db, item=item, type_=MovementType.ADJUSTMENT, qty=Decimal("-2"))
        cost = consume_fifo(db, item=item, qty=Decimal("2"), movement=m_neg)
        db.commit()
        assert cost == Decimal("8")
        db.refresh(item)
        assert item.current_qty == Decimal("5")
