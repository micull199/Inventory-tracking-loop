"""Integration tests for the manual PO creation route.

The reorder dashboard's auto-draft flow (POST ``/admin/reorder/draft-po``)
remains the default for the common case (one-click PO from low-stock items
per supplier). This route adds a manual alternative: pick a supplier, fill
in arbitrary lines, save as DRAFT. Same audit shape as the auto-draft path
plus a ``source: "manual"`` marker for traceability.

Coverage:
- Role enforcement: workshop 403; office + manager 200/303; anon 401.
- Happy path: creates DRAFT PO with the supplied lines + correct audit row.
- Validation: archived supplier rejected, duplicate item rejected, missing
  qty rejected, non-numeric qty rejected, negative cost rejected, archived
  item on a line rejected, bad expected_date rejected.
- Zero-line POs are allowed (you can draft an empty PO and edit lines in).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

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
    u = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        status=status,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


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


def _make_supplier(db: Session, name: str = "ACME", *, archived: bool = False) -> Supplier:
    s = Supplier(
        name=name,
        email="supplier@example.test",
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


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
    sku: str = "ITM-A",
    archived: bool = False,
) -> Item:
    i = Item(
        sku=sku,
        name="Test item",
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(i)
    db.commit()
    db.refresh(i)
    return i


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestManualPORoleEnforcement:
    def test_anonymous_get_form_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/purchase-orders/new")
        assert resp.status_code == 401

    def test_workshop_get_form_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/purchase-orders/new")
        assert resp.status_code == 403

    def test_workshop_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        sup = _make_supplier(db_session)
        _login_as(client, ws)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_office_can_create(self, client: TestClient, db_session: Session) -> None:
        sup = _make_supplier(db_session)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/purchase-orders/new")
        assert resp.status_code == 200
        resp = client.post(
            "/admin/purchase-orders/new",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_manager_can_create(self, client: TestClient, db_session: Session) -> None:
        sup = _make_supplier(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/purchase-orders/new")
        assert resp.status_code == 200
        resp = client.post(
            "/admin/purchase-orders/new",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestManualPOCreate:
    def test_creates_draft_with_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session, "ACME")
        leaf = _make_leaf(db_session)
        item_a = _make_item(db_session, leaf=leaf, sku="A-1")
        item_b = _make_item(db_session, leaf=leaf, sku="B-1")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "expected_date": "2026-06-01",
                "notes": "fake po for demo",
                "item_id_0": str(item_a.id),
                "qty_0": "10",
                "cost_0": "2.50",
                "item_id_1": str(item_b.id),
                "qty_1": "5",
                "cost_1": "",  # blank cost allowed
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text

        po = db_session.execute(select(PurchaseOrder)).scalar_one()
        assert po.supplier_id == sup.id
        assert po.status == POStatus.DRAFT
        assert po.expected_date == date(2026, 6, 1)
        assert po.notes == "fake po for demo"
        assert po.created_by == mgr.id

        lines = list(
            db_session.execute(
                select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id).order_by(PurchaseOrderLine.id)
            ).scalars().all()
        )
        assert len(lines) == 2
        assert (lines[0].item_id, lines[0].qty_ordered, lines[0].expected_unit_cost) == (
            item_a.id,
            Decimal("10"),
            Decimal("2.50"),
        )
        assert (lines[1].item_id, lines[1].qty_ordered, lines[1].expected_unit_cost) == (
            item_b.id,
            Decimal("5"),
            None,
        )

        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "purchase_order.created")
        ).scalar_one()
        assert audit.after_json["source"] == "manual"
        assert audit.after_json["supplier_id"] == sup.id
        assert audit.after_json["status"] == "draft"
        assert len(audit.after_json["lines"]) == 2

    def test_zero_lines_creates_empty_draft(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Empty drafts are useful: create the shell, add lines via the edit form later."""
        sup = _make_supplier(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        po = db_session.execute(select(PurchaseOrder)).scalar_one()
        assert po.status == POStatus.DRAFT
        assert db_session.execute(
            select(PurchaseOrderLine).where(PurchaseOrderLine.po_id == po.id)
        ).first() is None


# ---------------------------------------------------------------------------
# Validation rejects
# ---------------------------------------------------------------------------


class TestManualPOValidation:
    def test_archived_supplier_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session, archived=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_missing_supplier_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={"supplier_id": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_duplicate_item_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "item_id_0": str(item.id),
                "qty_0": "1",
                "item_id_1": str(item.id),
                "qty_1": "2",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_missing_qty_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "item_id_0": str(item.id),
                "qty_0": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_zero_qty_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "item_id_0": str(item.id),
                "qty_0": "0",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_non_numeric_qty_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "item_id_0": str(item.id),
                "qty_0": "abc",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_negative_cost_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "item_id_0": str(item.id),
                "qty_0": "1",
                "cost_0": "-1.00",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_archived_item_line_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "item_id_0": str(item.id),
                "qty_0": "1",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_bad_expected_date_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = _make_supplier(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/purchase-orders/new",
            data={
                "supplier_id": str(sup.id),
                "expected_date": "not-a-date",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Detail-route precedence (sanity check that /new doesn't collide with /{po_id})
# ---------------------------------------------------------------------------


class TestNewRouteDoesNotCollideWithDetail:
    def test_new_route_resolves_to_form_not_detail(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/purchase-orders/new")
        assert resp.status_code == 200
        # The literal /new is matched before /{po_id}, so we get the create
        # form rather than a 404 on po_id="new".
        assert "po-new-form" in resp.text
