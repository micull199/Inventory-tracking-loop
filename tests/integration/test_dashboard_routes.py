"""Integration tests for the reporting dashboard (R1).

Covers:
- Role enforcement: anon 401; pending 403; Workshop 403 (cannot see aggregated
  cost data); Manager / Office / Admin 200.
- Total inventory value: empty state; single layer; multi-item sum; archived
  items excluded; fully-consumed layers contribute zero.
- Low-stock count: zero / positive; archived items excluded.
- Open POs count: zero / positive; counts draft + sent + partially_received
  but not received / cancelled.
- Top consumed: empty when no OUT movements; orders by sum(qty) desc; respects
  ``?top_days=N`` window; bad / out-of-range ``top_days`` coerces silently.
- COGS: zero with no movements; sums OUT total_cost; sums ADJUSTMENT decreases
  (movements with consumption rows); excludes ADJUSTMENT increases + IN +
  TRANSFER; respects date range; bad date format 400.

The dashboard is read-only — no audit, no movement type, no DB mutations.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cost_engine import consume_fifo, record_receipt
from app.models import (
    AuditLog,
    CostLayerSource,
    Item,
    MovementType,
    POStatus,
    PurchaseOrder,
    Role,
    StockMovement,
    Supplier,
    TaxonomyNode,
    TrackingMode,
    User,
    UserStatus,
)

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None = None,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        status=status,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login_as(client: TestClient, user: User) -> None:
    resp = client.post(
        "/auth/_dev-login",
        data={"email": user.email, "sub": user.google_sub},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _make_leaf(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_item(
    db: Session,
    *,
    leaf: TaxonomyNode,
    sku: str = "SKU-1",
    name: str = "Item",
    current_qty: Decimal = Decimal("0"),
    threshold: Decimal = Decimal("0"),
    archived: bool = False,
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
        current_qty=current_qty,
        reorder_threshold=threshold,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_supplier(db: Session, name: str = "ACME") -> Supplier:
    s = Supplier(name=name)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _seed_in(
    db: Session,
    *,
    item: Item,
    qty: str | Decimal,
    unit_cost: str | Decimal,
    actor: User,
    when: datetime | None = None,
    source: CostLayerSource = CostLayerSource.MANUAL_IN,
) -> StockMovement:
    """Seed an IN movement + cost layer via the engine."""
    qd = qty if isinstance(qty, Decimal) else Decimal(qty)
    cd = unit_cost if isinstance(unit_cost, Decimal) else Decimal(unit_cost)
    m = StockMovement(
        item_id=item.id, type=MovementType.IN, qty=qd, user_id=actor.id
    )
    if when is not None:
        m.created_at = when
    db.add(m)
    db.flush()
    record_receipt(
        db,
        item=item,
        qty=qd,
        unit_cost=cd,
        source=source,
        movement=m,
        received_at=when,
    )
    db.commit()
    db.refresh(m)
    db.refresh(item)
    return m


def _seed_out(
    db: Session,
    *,
    item: Item,
    qty: str | Decimal,
    actor: User,
    when: datetime | None = None,
) -> StockMovement:
    """Seed an OUT movement consuming FIFO from the item's open layers."""
    qd = qty if isinstance(qty, Decimal) else Decimal(qty)
    m = StockMovement(
        item_id=item.id, type=MovementType.OUT, qty=qd, user_id=actor.id
    )
    if when is not None:
        m.created_at = when
    db.add(m)
    db.flush()
    consume_fifo(db, item=item, qty=qd, movement=m)
    db.commit()
    db.refresh(m)
    db.refresh(item)
    return m


def _seed_adjust_decrease(
    db: Session,
    *,
    item: Item,
    qty: str | Decimal,
    actor: User,
    when: datetime | None = None,
) -> StockMovement:
    """Seed an ADJUSTMENT (decrease) movement consuming FIFO."""
    qd = qty if isinstance(qty, Decimal) else Decimal(qty)
    m = StockMovement(
        item_id=item.id,
        type=MovementType.ADJUSTMENT,
        qty=qd,
        user_id=actor.id,
        reason="loss",
    )
    if when is not None:
        m.created_at = when
    db.add(m)
    db.flush()
    consume_fifo(db, item=item, qty=qd, movement=m)
    db.commit()
    db.refresh(m)
    db.refresh(item)
    return m


def _seed_adjust_increase(
    db: Session,
    *,
    item: Item,
    qty: str | Decimal,
    unit_cost: str | Decimal,
    actor: User,
    when: datetime | None = None,
) -> StockMovement:
    """Seed an ADJUSTMENT (increase) movement creating a positive layer."""
    qd = qty if isinstance(qty, Decimal) else Decimal(qty)
    cd = unit_cost if isinstance(unit_cost, Decimal) else Decimal(unit_cost)
    m = StockMovement(
        item_id=item.id,
        type=MovementType.ADJUSTMENT,
        qty=qd,
        user_id=actor.id,
        reason="found",
    )
    if when is not None:
        m.created_at = when
    db.add(m)
    db.flush()
    record_receipt(
        db,
        item=item,
        qty=qd,
        unit_cost=cd,
        source=CostLayerSource.POSITIVE_ADJUSTMENT,
        movement=m,
        received_at=when,
    )
    db.commit()
    db.refresh(m)
    db.refresh(item)
    return m


def _seed_transfer(
    db: Session,
    *,
    item: Item,
    qty: str | Decimal,
    actor: User,
    when: datetime | None = None,
) -> StockMovement:
    """Seed a TRANSFER movement (no cost engine)."""
    qd = qty if isinstance(qty, Decimal) else Decimal(qty)
    m = StockMovement(
        item_id=item.id,
        type=MovementType.TRANSFER,
        qty=qd,
        user_id=actor.id,
    )
    if when is not None:
        m.created_at = when
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


def _make_po(
    db: Session,
    *,
    supplier: Supplier,
    status: POStatus = POStatus.DRAFT,
    creator: User | None = None,
) -> PurchaseOrder:
    po = PurchaseOrder(
        supplier_id=supplier.id,
        status=status,
        created_by=creator.id if creator is not None else None,
    )
    db.add(po)
    db.commit()
    db.refresh(po)
    return po


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestDashboardRoleEnforcement:
    def test_anonymous_get_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 401

    def test_pending_user_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 403

    def test_workshop_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Workshop cannot see aggregated cost data (MISSION §3)."""
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 403

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200

    def test_admin_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Total inventory value
# ---------------------------------------------------------------------------


class TestDashboardTotalValue:
    def test_empty_state_is_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200
        assert 'data-testid="dashboard-total-value">0' in resp.text

    def test_single_layer_value(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="10", unit_cost="2.50", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # 10 * 2.50 = 25; the columns are scale-4 so the product is "25.0000".
        assert 'data-testid="dashboard-total-value">25.0000' in resp.text

    def test_multi_item_sum(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A-1")
        b = _make_item(db_session, leaf=leaf, sku="B-1")
        _seed_in(db_session, item=a, qty="10", unit_cost="2", actor=mgr)
        _seed_in(db_session, item=b, qty="5", unit_cost="3", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # 10*2 + 5*3 = 35
        assert 'data-testid="dashboard-total-value">35.0000' in resp.text

    def test_archived_item_layers_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        """An archived item's open layers don't contribute to total value."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A-1")
        b = _make_item(db_session, leaf=leaf, sku="B-1")
        _seed_in(db_session, item=a, qty="10", unit_cost="2", actor=mgr)
        _seed_in(db_session, item=b, qty="5", unit_cost="3", actor=mgr)
        # Archive b after the receipt (its layers stay in the table).
        b.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # Only a contributes: 10 * 2 = 20.
        assert 'data-testid="dashboard-total-value">20.0000' in resp.text

    def test_fully_consumed_layer_contributes_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        """qty_remaining=0 means a layer's contribution is 0 by arithmetic."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="10", unit_cost="2", actor=mgr)
        # Drain the layer entirely.
        _seed_out(db_session, item=item, qty="10", actor=mgr)
        # Re-stock at a different cost.
        _seed_in(db_session, item=item, qty="5", unit_cost="3", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # Drained layer contributes 0; new layer = 5*3 = 15.
        assert 'data-testid="dashboard-total-value">15.0000' in resp.text


# ---------------------------------------------------------------------------
# Low-stock count
# ---------------------------------------------------------------------------


class TestDashboardLowStockCount:
    def test_empty_is_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-low-stock-count">0' in resp.text

    def test_positive_count(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        # Two below-threshold items + one above.
        _make_item(
            db_session,
            leaf=leaf,
            sku="LOW-1",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="LOW-2",
            current_qty=Decimal("3"),
            threshold=Decimal("5"),
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="OK-1",
            current_qty=Decimal("50"),
            threshold=Decimal("10"),
        )
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-low-stock-count">2' in resp.text

    def test_archived_items_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="LOW-1",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="ARCHIVED-1",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
            archived=True,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-low-stock-count">1' in resp.text


# ---------------------------------------------------------------------------
# Open POs count
# ---------------------------------------------------------------------------


class TestDashboardOpenPOsCount:
    def test_empty_is_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-open-pos-count">0' in resp.text

    def test_counts_open_excludes_received_and_cancelled(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session)
        _make_po(db_session, supplier=sup, status=POStatus.DRAFT)
        _make_po(db_session, supplier=sup, status=POStatus.SENT)
        _make_po(db_session, supplier=sup, status=POStatus.PARTIALLY_RECEIVED)
        _make_po(db_session, supplier=sup, status=POStatus.RECEIVED)
        _make_po(db_session, supplier=sup, status=POStatus.CANCELLED)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # 3 of the 5 are open (draft + sent + partially_received).
        assert 'data-testid="dashboard-open-pos-count">3' in resp.text


# ---------------------------------------------------------------------------
# Top consumed items
# ---------------------------------------------------------------------------


class TestDashboardTopConsumed:
    def test_empty_state_when_no_outs(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-top-consumed-empty"' in resp.text
        assert 'data-testid="dashboard-top-consumed-row"' not in resp.text

    def test_orders_by_sum_qty_desc(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A-1", name="Apple")
        b = _make_item(db_session, leaf=leaf, sku="B-1", name="Banana")
        c = _make_item(db_session, leaf=leaf, sku="C-1", name="Cherry")
        _seed_in(db_session, item=a, qty="100", unit_cost="1", actor=mgr)
        _seed_in(db_session, item=b, qty="100", unit_cost="1", actor=mgr)
        _seed_in(db_session, item=c, qty="100", unit_cost="1", actor=mgr)
        _seed_out(db_session, item=a, qty="5", actor=mgr)
        _seed_out(db_session, item=b, qty="20", actor=mgr)
        _seed_out(db_session, item=c, qty="10", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # B (20) > C (10) > A (5)
        b_idx = resp.text.find('data-testid="dashboard-top-consumed-sku">B-1')
        c_idx = resp.text.find('data-testid="dashboard-top-consumed-sku">C-1')
        a_idx = resp.text.find('data-testid="dashboard-top-consumed-sku">A-1')
        assert 0 < b_idx < c_idx < a_idx

    def test_window_excludes_old_movements(
        self, client: TestClient, db_session: Session
    ) -> None:
        """OUTs older than the window don't appear."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="OLD-1")
        _seed_in(db_session, item=item, qty="100", unit_cost="1", actor=mgr)
        # An OUT 100 days ago — outside the default 30-day window.
        old = datetime.now(UTC) - timedelta(days=100)
        _seed_out(db_session, item=item, qty="42", actor=mgr, when=old)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-top-consumed-empty"' in resp.text

    def test_top_days_query_param_widens_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="OLD-1")
        _seed_in(db_session, item=item, qty="100", unit_cost="1", actor=mgr)
        old = datetime.now(UTC) - timedelta(days=100)
        _seed_out(db_session, item=item, qty="42", actor=mgr, when=old)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard?top_days=200")
        assert 'data-testid="dashboard-top-consumed-row"' in resp.text
        assert 'data-testid="dashboard-top-consumed-qty">42' in resp.text

    def test_bad_top_days_coerces_to_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard?top_days=foo")
        assert resp.status_code == 200
        # Default is 30; the form input should pre-fill with it.
        assert 'data-testid="dashboard-top-days-input"' in resp.text
        assert 'value="30"' in resp.text

    def test_out_of_range_top_days_coerces_to_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard?top_days=99999")
        assert resp.status_code == 200
        assert 'value="30"' in resp.text

    def test_transfer_movements_not_counted(
        self, client: TestClient, db_session: Session
    ) -> None:
        """TRANSFER doesn't show in top-consumed."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="T-1")
        _seed_in(db_session, item=item, qty="100", unit_cost="1", actor=mgr)
        _seed_transfer(db_session, item=item, qty="50", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # No OUT was recorded, so the table is empty.
        assert 'data-testid="dashboard-top-consumed-empty"' in resp.text


# ---------------------------------------------------------------------------
# Cost of goods consumed
# ---------------------------------------------------------------------------


class TestDashboardCOGS:
    def test_empty_is_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-cogs-amount">0' in resp.text

    def test_sums_out_total_cost(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="10", unit_cost="2", actor=mgr)
        _seed_out(db_session, item=item, qty="3", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # 3 * 2 = 6 (column-quantised to scale 4 from the engine path).
        assert 'data-testid="dashboard-cogs-amount">6.0000' in resp.text

    def test_sums_adjustment_decreases_excludes_increases(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="10", unit_cost="2", actor=mgr)
        # Decrease consumes layers — counts toward COGS.
        _seed_adjust_decrease(db_session, item=item, qty="2", actor=mgr)
        # Increase creates a layer — must NOT count toward COGS.
        _seed_adjust_increase(
            db_session, item=item, qty="5", unit_cost="9", actor=mgr
        )
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # Only the decrease counts: 2 * 2 = 4.
        assert 'data-testid="dashboard-cogs-amount">4.0000' in resp.text

    def test_in_movements_not_counted(
        self, client: TestClient, db_session: Session
    ) -> None:
        """IN movements have total_cost set but should not appear in COGS."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="10", unit_cost="2.50", actor=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert 'data-testid="dashboard-cogs-amount">0' in resp.text

    def test_date_range_filters_old_movements(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="100", unit_cost="2", actor=mgr)
        # Recent OUT — within default 30-day window.
        _seed_out(db_session, item=item, qty="3", actor=mgr)
        # Old OUT — outside default window.
        old = datetime.now(UTC) - timedelta(days=60)
        _seed_out(db_session, item=item, qty="7", actor=mgr, when=old)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        # Default window picks only the 3-unit OUT: 3 * 2 = 6.
        assert 'data-testid="dashboard-cogs-amount">6.0000' in resp.text

    def test_explicit_date_range_includes_old_movements(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="100", unit_cost="2", actor=mgr)
        old = datetime.now(UTC) - timedelta(days=60)
        _seed_out(db_session, item=item, qty="7", actor=mgr, when=old)
        _login_as(client, mgr)
        # Widen: from 90 days ago through today.
        start = (datetime.now(UTC) - timedelta(days=90)).date().isoformat()
        end = datetime.now(UTC).date().isoformat()
        resp = client.get(
            f"/admin/dashboard?cogs_start={start}&cogs_end={end}"
        )
        # Now the 7-unit OUT counts: 7 * 2 = 14.
        assert 'data-testid="dashboard-cogs-amount">14.0000' in resp.text

    def test_bad_cogs_start_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard?cogs_start=not-a-date")
        assert resp.status_code == 400

    def test_bad_cogs_end_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard?cogs_end=2026-13-99")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# No-audit-no-mutation invariant
# ---------------------------------------------------------------------------


class TestDashboardReadOnly:
    def test_get_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The dashboard is a pure read; no audit row should land."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        _seed_in(db_session, item=item, qty="10", unit_cost="2", actor=mgr)

        before = db_session.execute(
            select(AuditLog).order_by(AuditLog.id.desc())
        ).scalars().all()
        before_ids = [a.id for a in before]

        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200

        after = db_session.execute(
            select(AuditLog).order_by(AuditLog.id.desc())
        ).scalars().all()
        # No new rows — only ones present before the GET.
        assert all(a.id in before_ids for a in after)
