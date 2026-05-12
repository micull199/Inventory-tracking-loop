"""Integration tests for inbound PO in-transit visibility (Slice 3 of the
in-transit / stages scope addition).

Adds:
- ``shipped_at`` column on ``purchase_orders``.
- ``IN_TRANSIT`` value on ``POStatus`` enum.
- ``POST /admin/purchase-orders/{id}/mark-shipped`` — manager+office only;
  flips a ``SENT`` PO to ``IN_TRANSIT`` with optional ``expected_date``.
- Receive route accepts ``IN_TRANSIT`` as a valid starting status, alongside
  ``SENT`` and ``PARTIALLY_RECEIVED`` (backwards compat preserved).
- Dashboard widget for in-transit POs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.dashboard import _in_transit_pos
from app.models import (
    AuditLog,
    Item,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    Supplier,
    TaxonomyNode,
    TrackingMode,
    User,
    UserStatus,
)

# ---------------------------------------------------------------------------
# Scaffolding
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


def _csrf(client: TestClient) -> str:
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _make_supplier(db: Session, name: str = "ACME") -> Supplier:
    s = Supplier(name=name, email="supplier@example.test")
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_leaf(db: Session) -> TaxonomyNode:
    n = TaxonomyNode(name="Raw")
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_item(
    db: Session, *, leaf: TaxonomyNode, supplier: Supplier, sku: str = "ITM-1"
) -> Item:
    item = Item(
        sku=sku,
        name="Silver wire",
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
        supplier_id=supplier.id,
        reorder_threshold=Decimal("10"),
        reorder_qty=Decimal("100"),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_sent_po(
    db: Session,
    *,
    supplier: Supplier,
    item: Item,
    qty_ordered: Decimal = Decimal("10"),
    expected_unit_cost: Decimal = Decimal("2.50"),
    expected_date: date | None = None,
) -> PurchaseOrder:
    po = PurchaseOrder(
        supplier_id=supplier.id,
        status=POStatus.SENT,
        sent_at=datetime.now(UTC),
        expected_date=expected_date,
    )
    db.add(po)
    db.flush()
    db.add(
        PurchaseOrderLine(
            po_id=po.id,
            item_id=item.id,
            qty_ordered=qty_ordered,
            expected_unit_cost=expected_unit_cost,
        )
    )
    db.commit()
    db.refresh(po)
    return po


# ---------------------------------------------------------------------------
# mark-shipped route
# ---------------------------------------------------------------------------


class TestMarkShippedRoleEnforcement:
    def test_anonymous_is_401(self, client: TestClient, db_session: Session) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(db_session, supplier=supplier, item=item)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_workshop_is_403(self, client: TestClient, db_session: Session) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(db_session, supplier=supplier, item=item)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_office_can_mark(self, client: TestClient, db_session: Session) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(db_session, supplier=supplier, item=item)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestMarkShippedHappyPath:
    def test_flips_to_in_transit(
        self, client: TestClient, db_session: Session
    ) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(db_session, supplier=supplier, item=item)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        db_session.refresh(po)
        assert po.status == POStatus.IN_TRANSIT
        assert po.shipped_at is not None

        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "purchase_order.shipped")
        ).scalar_one()
        assert audit.entity_id == po.id
        assert audit.before_json["status"] == "sent"
        assert audit.after_json["status"] == "in_transit"

    def test_overrides_expected_date(
        self, client: TestClient, db_session: Session
    ) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(
            db_session, supplier=supplier, item=item, expected_date=date(2026, 1, 1)
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={
                "expected_date": "2026-05-20",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(po)
        assert po.expected_date == date(2026, 5, 20)


class TestMarkShippedStatusGuard:
    def test_only_sent(self, client: TestClient, db_session: Session) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(db_session, supplier=supplier, item=item)
        # Flip to in_transit so the second mark-shipped is rejected.
        po.status = POStatus.IN_TRANSIT
        po.shipped_at = datetime.now(UTC)
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_404_for_unknown(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/9999999/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_bad_expected_date_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(db_session, supplier=supplier, item=item)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={
                "expected_date": "not-a-date",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.refresh(po)
        assert po.status == POStatus.SENT  # unchanged


# ---------------------------------------------------------------------------
# Receive accepts IN_TRANSIT
# ---------------------------------------------------------------------------


class TestReceiveFromInTransit:
    def test_receive_from_in_transit_works(
        self, client: TestClient, db_session: Session
    ) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(
            db_session, supplier=supplier, item=item, qty_ordered=Decimal("10")
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        # Mark shipped first.
        client.post(
            f"/admin/purchase-orders/{po.id}/mark-shipped",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        db_session.refresh(po)
        assert po.status == POStatus.IN_TRANSIT

        # Receive form should render (200) — same form, now reachable from IN_TRANSIT.
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200

        # And the POST should succeed.
        line = db_session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id)
        ).scalar_one()
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/receive",
            data={
                f"received_{line.id}": "10",
                f"cost_{line.id}": "2.50",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        db_session.refresh(po)
        assert po.status == POStatus.RECEIVED

    def test_receive_from_sent_still_works(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Backwards compat: SENT POs that skip the mark-shipped step can still be received."""
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)
        po = _make_sent_po(
            db_session, supplier=supplier, item=item, qty_ordered=Decimal("10")
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        line = db_session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id)
        ).scalar_one()
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/receive",
            data={
                f"received_{line.id}": "10",
                f"cost_{line.id}": "2.50",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(po)
        assert po.status == POStatus.RECEIVED


# ---------------------------------------------------------------------------
# Dashboard widget helper
# ---------------------------------------------------------------------------


class TestInTransitPosDashboardHelper:
    def test_counts_only_in_transit(self, db_session: Session) -> None:
        supplier = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, supplier=supplier)

        # Mix of statuses; only IN_TRANSIT should count.
        sent_po = _make_sent_po(db_session, supplier=supplier, item=item)
        in_transit_po = _make_sent_po(
            db_session,
            supplier=supplier,
            item=_make_item(db_session, leaf=leaf, supplier=supplier, sku="ITM-2"),
            expected_date=date(2026, 6, 1),
        )
        in_transit_po.status = POStatus.IN_TRANSIT
        in_transit_po.shipped_at = datetime.now(UTC)

        earlier_in_transit_po = _make_sent_po(
            db_session,
            supplier=supplier,
            item=_make_item(db_session, leaf=leaf, supplier=supplier, sku="ITM-3"),
            expected_date=date(2026, 5, 15),
        )
        earlier_in_transit_po.status = POStatus.IN_TRANSIT
        earlier_in_transit_po.shipped_at = datetime.now(UTC)

        db_session.commit()
        _ = sent_po  # keep mypy happy about unused-var

        result = _in_transit_pos(db_session)
        assert result["count"] == 2
        assert result["earliest_expected"] == date(2026, 5, 15)

    def test_empty_dashboard(self, db_session: Session) -> None:
        result = _in_transit_pos(db_session)
        assert result == {"count": 0, "earliest_expected": None}
