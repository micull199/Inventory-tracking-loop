"""Unit tests for ST1 — StockTake / StockTakeLine models and pure helpers.

Covers:
- ``StockTake`` round-trips with the minimal column set (scope=all).
- ``StockTakeLine`` round-trips with system_qty + nullable counted_qty.
- ``_status_label`` derives ``scheduled`` / ``in_progress`` / ``completed``
  from the timestamps with no enum column involved.
- ``_scope_label`` produces the user-facing label for each of the three
  scope shapes.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models import (
    Item,
    Location,
    StockTake,
    StockTakeLine,
    TaxonomyNode,
    TrackingMode,
)
from app.stock_takes import _scope_label, _status_label

# ---------------------------------------------------------------------------
# Model round-trips
# ---------------------------------------------------------------------------


def _make_node(db: Session, name: str = "Tools") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_location(db: Session, name: str = "Bench") -> Location:
    loc = Location(name=name)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def _make_item(db: Session, leaf: TaxonomyNode, sku: str = "SKU-1") -> Item:
    item = Item(
        sku=sku,
        name="Item",
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.QTY,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestStockTakeModel:
    def test_minimal_insert_with_scope_all(
        self, db_session: Session
    ) -> None:
        st = StockTake(scheduled_for=date(2026, 6, 1))
        db_session.add(st)
        db_session.commit()
        db_session.refresh(st)
        assert st.id is not None
        assert st.scope_node_id is None
        assert st.scope_location_id is None
        assert st.started_at is None
        assert st.completed_at is None
        assert st.notes is None
        assert st.scheduled_for == date(2026, 6, 1)

    def test_with_node_scope(self, db_session: Session) -> None:
        node = _make_node(db_session, name="Raw")
        st = StockTake(
            scheduled_for=date(2026, 6, 1),
            scope_node_id=node.id,
        )
        db_session.add(st)
        db_session.commit()
        db_session.refresh(st)
        assert st.scope_node_id == node.id

    def test_with_location_scope(self, db_session: Session) -> None:
        loc = _make_location(db_session, name="Vault")
        st = StockTake(
            scheduled_for=date(2026, 6, 1),
            scope_location_id=loc.id,
        )
        db_session.add(st)
        db_session.commit()
        db_session.refresh(st)
        assert st.scope_location_id == loc.id


class TestStockTakeLineModel:
    def test_minimal_line(self, db_session: Session) -> None:
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = StockTake(scheduled_for=date(2026, 6, 1))
        db_session.add(st)
        db_session.commit()
        line = StockTakeLine(
            stock_take_id=st.id,
            item_id=item.id,
            system_qty=Decimal("10.0000"),
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(line)
        assert line.id is not None
        assert line.counted_qty is None
        assert line.variance is None
        assert line.committed is False

    def test_completed_line(self, db_session: Session) -> None:
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = StockTake(scheduled_for=date(2026, 6, 1))
        db_session.add(st)
        db_session.commit()
        line = StockTakeLine(
            stock_take_id=st.id,
            item_id=item.id,
            system_qty=Decimal("10.0000"),
            counted_qty=Decimal("8.0000"),
            variance=Decimal("-2.0000"),
            committed=True,
        )
        db_session.add(line)
        db_session.commit()
        db_session.refresh(line)
        assert line.counted_qty == Decimal("8.0000")
        assert line.variance == Decimal("-2.0000")
        assert line.committed is True


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestStatusLabel:
    def test_scheduled(self) -> None:
        st = StockTake(scheduled_for=date(2026, 6, 1))
        assert _status_label(st) == "scheduled"

    def test_in_progress(self) -> None:
        st = StockTake(
            scheduled_for=date(2026, 6, 1),
            started_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
        )
        assert _status_label(st) == "in_progress"

    def test_completed(self) -> None:
        st = StockTake(
            scheduled_for=date(2026, 6, 1),
            started_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
            completed_at=datetime(2026, 6, 1, 11, tzinfo=UTC),
        )
        assert _status_label(st) == "completed"


class TestScopeLabel:
    def test_all_items(self) -> None:
        st = StockTake(scheduled_for=date(2026, 6, 1))
        assert _scope_label(st, node=None, location=None) == "All items"

    def test_category(self) -> None:
        node = TaxonomyNode(name="Tools")
        node.id = 1
        st = StockTake(scheduled_for=date(2026, 6, 1), scope_node_id=1)
        assert _scope_label(st, node=node, location=None) == "Category: Tools"

    def test_location(self) -> None:
        loc = Location(name="Vault")
        loc.id = 1
        st = StockTake(scheduled_for=date(2026, 6, 1), scope_location_id=1)
        assert _scope_label(st, node=None, location=loc) == "Location: Vault"
