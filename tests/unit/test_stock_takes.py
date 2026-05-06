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
from app.stock_takes import (
    _compute_variance,
    _format_variance,
    _resolve_scope_items,
    _scope_label,
    _status_label,
    _variance_sign,
)

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


# ---------------------------------------------------------------------------
# Variance helpers
# ---------------------------------------------------------------------------


class TestComputeVariance:
    def test_excess(self) -> None:
        assert _compute_variance(Decimal("12"), Decimal("10")) == Decimal("2")

    def test_shrinkage(self) -> None:
        assert _compute_variance(Decimal("5"), Decimal("10")) == Decimal("-5")

    def test_zero(self) -> None:
        assert _compute_variance(Decimal("10"), Decimal("10")) == Decimal("0")

    def test_uncounted(self) -> None:
        assert _compute_variance(None, Decimal("10")) is None


class TestFormatVariance:
    def test_positive(self) -> None:
        assert _format_variance(Decimal("2.5000")) == "+2.5000"

    def test_negative(self) -> None:
        assert _format_variance(Decimal("-2.0000")) == "-2.0000"

    def test_zero(self) -> None:
        assert _format_variance(Decimal("0.0000")) == "0.0000"

    def test_none(self) -> None:
        assert _format_variance(None) == ""


class TestVarianceSign:
    def test_positive(self) -> None:
        assert _variance_sign(Decimal("2")) == "pos"

    def test_negative(self) -> None:
        assert _variance_sign(Decimal("-2")) == "neg"

    def test_zero(self) -> None:
        assert _variance_sign(Decimal("0")) == "zero"

    def test_none(self) -> None:
        assert _variance_sign(None) == ""


# ---------------------------------------------------------------------------
# Scope item resolution (DB-backed)
# ---------------------------------------------------------------------------


def _make_archived_item(
    db: Session, leaf: TaxonomyNode, sku: str = "ARCHIVED"
) -> Item:
    item = Item(
        sku=sku,
        name="Archived",
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.QTY,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestResolveScopeItems:
    def test_all_scope_returns_all_active_items(
        self, db_session: Session
    ) -> None:
        leaf = _make_node(db_session)
        a = _make_item(db_session, leaf, sku="A-1")
        b = _make_item(db_session, leaf, sku="B-1")
        st = StockTake(scheduled_for=date(2026, 6, 1))
        db_session.add(st)
        db_session.commit()
        items = _resolve_scope_items(db_session, st)
        assert {i.id for i in items} == {a.id, b.id}

    def test_all_scope_excludes_archived(self, db_session: Session) -> None:
        leaf = _make_node(db_session)
        active = _make_item(db_session, leaf, sku="ACTIVE")
        _make_archived_item(db_session, leaf, sku="ARCHIVED")
        st = StockTake(scheduled_for=date(2026, 6, 1))
        db_session.add(st)
        db_session.commit()
        items = _resolve_scope_items(db_session, st)
        assert {i.id for i in items} == {active.id}

    def test_node_scope_returns_node_items(self, db_session: Session) -> None:
        leaf_a = _make_node(db_session, name="A")
        leaf_b = _make_node(db_session, name="B")
        in_a = _make_item(db_session, leaf_a, sku="IN-A")
        _make_item(db_session, leaf_b, sku="IN-B")
        st = StockTake(scheduled_for=date(2026, 6, 1), scope_node_id=leaf_a.id)
        db_session.add(st)
        db_session.commit()
        items = _resolve_scope_items(db_session, st)
        assert {i.id for i in items} == {in_a.id}

    def test_node_scope_includes_descendant_items(
        self, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Parent")
        child = TaxonomyNode(name="Child", parent_id=parent.id)
        db_session.add(child)
        db_session.commit()
        db_session.refresh(child)
        in_parent = _make_item(db_session, parent, sku="P-1")
        in_child = _make_item(db_session, child, sku="C-1")
        st = StockTake(scheduled_for=date(2026, 6, 1), scope_node_id=parent.id)
        db_session.add(st)
        db_session.commit()
        items = _resolve_scope_items(db_session, st)
        assert {i.id for i in items} == {in_parent.id, in_child.id}

    def test_location_scope_returns_location_items(
        self, db_session: Session
    ) -> None:
        leaf = _make_node(db_session)
        loc_a = _make_location(db_session, name="A")
        loc_b = _make_location(db_session, name="B")
        item_a = Item(
            sku="A-1",
            name="A",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.QTY,
            location_id=loc_a.id,
        )
        item_b = Item(
            sku="B-1",
            name="B",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.QTY,
            location_id=loc_b.id,
        )
        db_session.add_all([item_a, item_b])
        db_session.commit()
        st = StockTake(
            scheduled_for=date(2026, 6, 1), scope_location_id=loc_a.id
        )
        db_session.add(st)
        db_session.commit()
        items = _resolve_scope_items(db_session, st)
        assert {i.sku for i in items} == {"A-1"}

    def test_ordering_by_sku(self, db_session: Session) -> None:
        leaf = _make_node(db_session)
        _make_item(db_session, leaf, sku="C-3")
        _make_item(db_session, leaf, sku="A-1")
        _make_item(db_session, leaf, sku="B-2")
        st = StockTake(scheduled_for=date(2026, 6, 1))
        db_session.add(st)
        db_session.commit()
        items = _resolve_scope_items(db_session, st)
        assert [i.sku for i in items] == ["A-1", "B-2", "C-3"]
