"""Integration tests for the purchase-orders write surface (PO2).

Covers:
- Role enforcement on the three new endpoints (anon 401; pending 403; Workshop
  403; Manager / Office / Admin 200 / 200 / 303).
- ``POST /admin/reorder/draft-po`` validation: blank / non-int / unknown /
  archived supplier all 400; supplier with zero low-stock items 400; failed
  validation writes no PO + no lines + no audit row.
- Happy path: creates a single ``PurchaseOrder`` + one ``PurchaseOrderLine``
  per item below threshold; ordered by SKU; flash + 303 redirect to the new
  PO's detail page; audit row content.
- Default ``qty_ordered`` selection (reorder_qty > 0 → reorder_qty; reorder_qty
  == 0 + threshold > current_qty → deficit; reorder_qty == 0 + deficit == 0 →
  Decimal("1")).
- Default ``expected_unit_cost`` selection (last layer's unit_cost; multi-layer
  picks newest; no-layer → None).
- Filter scope: above-threshold / archived / different-supplier items are NOT
  included in the draft.
- ``GET /admin/purchase-orders``: empty state, populated, status filter
  showing only matching rows.
- ``GET /admin/purchase-orders/{po_id}``: 404 on unknown; populated detail
  renders heading + supplier + status + line cells; null expected_unit_cost
  renders the empty-cost marker.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.cost_engine import record_receipt
from app.email_backend import clear_console_outbox, console_outbox
from app.models import (
    AuditLog,
    CostLayerSource,
    Item,
    MovementType,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
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


def _csrf(client: TestClient) -> str:
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _make_leaf(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_supplier(
    db: Session,
    name: str = "ACME",
    *,
    archived: bool = False,
    email: str | None = None,
) -> Supplier:
    s = Supplier(
        name=name,
        email=email,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_item(
    db: Session,
    *,
    leaf: TaxonomyNode,
    sku: str = "SKU-1",
    name: str = "Item",
    current_qty: Decimal = Decimal("0"),
    threshold: Decimal = Decimal("10"),
    reorder_qty: Decimal = Decimal("100"),
    supplier: Supplier | None = None,
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
        reorder_qty=reorder_qty,
        supplier_id=supplier.id if supplier is not None else None,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _seed_layer(
    db: Session,
    *,
    item: Item,
    actor: User,
    qty: Decimal,
    unit_cost: Decimal,
    received_at: datetime | None = None,
) -> None:
    """Create a layer the canonical way (through ``record_receipt``).

    Used to exercise the ``expected_unit_cost`` lookup path in the create
    handler — the route reads back the most recent layer's ``unit_cost``.
    """
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.IN,
        qty=qty,
        user_id=actor.id,
    )
    db.add(movement)
    db.flush()
    record_receipt(
        db,
        item=item,
        qty=qty,
        unit_cost=unit_cost,
        source=CostLayerSource.MANUAL_IN,
        movement=movement,
        received_at=received_at,
    )
    db.commit()


def _po_audit_rows(
    db: Session, *, action: str | None = None
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "purchase_order")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestPORoleEnforcement:
    def test_anonymous_post_create_is_401(self, client: TestClient) -> None:
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "1", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 401

    def test_anonymous_get_detail_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/purchase-orders/1")
        assert resp.status_code == 401

    def test_pending_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "1", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_workshop_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "1", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 403

    def test_workshop_get_detail_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/purchase-orders/1")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 200

    def test_office_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# POST validation
# ---------------------------------------------------------------------------


class TestDraftPOValidation:
    def _setup(self, db: Session, client: TestClient) -> Supplier:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        return _make_supplier(db, name="ACME")

    def test_blank_supplier_id_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        self._setup(db_session, client)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(PurchaseOrder)).first() is None

    def test_non_int_supplier_id_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        self._setup(db_session, client)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "abc", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unknown_supplier_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        self._setup(db_session, client)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "999", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_archived_supplier_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        sup = _make_supplier(db_session, name="ACME", archived=True)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_supplier_with_no_low_stock_items_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = self._setup(db_session, client)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(PurchaseOrder)).first() is None

    def test_supplier_with_only_above_threshold_items_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup = self._setup(db_session, client)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="OK-1",
            current_qty=Decimal("50"),
            threshold=Decimal("10"),
            supplier=sup,
        )
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_validation_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        self._setup(db_session, client)
        client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": "999", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert _po_audit_rows(db_session) == []
        assert (
            db_session.execute(select(PurchaseOrderLine)).first() is None
        )


# ---------------------------------------------------------------------------
# POST happy path
# ---------------------------------------------------------------------------


class TestDraftPOHappyPath:
    def test_single_low_stock_item_creates_one_po_with_one_line(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="LOW-1",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("100"),
            supplier=sup,
        )
        _login_as(client, u)

        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        po = db_session.execute(select(PurchaseOrder)).scalar_one()
        assert po.supplier_id == sup.id
        assert po.status == POStatus.DRAFT
        assert po.created_by == u.id
        lines = list(
            db_session.execute(select(PurchaseOrderLine)).scalars().all()
        )
        assert len(lines) == 1
        assert lines[0].po_id == po.id
        assert lines[0].qty_ordered == Decimal("100")
        assert lines[0].qty_received == Decimal("0")
        assert lines[0].expected_unit_cost is None
        assert resp.headers["location"] == f"/admin/purchase-orders/{po.id}"

    def test_multi_line_po_ordered_by_sku(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        # Insert in non-SKU order to ensure SKU-order on the lines.
        _make_item(
            db_session,
            leaf=leaf,
            sku="C-3",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            reorder_qty=Decimal("10"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="A-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            reorder_qty=Decimal("20"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="B-2",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            reorder_qty=Decimal("30"),
            supplier=sup,
        )
        _login_as(client, u)

        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Pull lines + their items in SKU order to mirror the route.
        line_rows = list(
            db_session.execute(
                select(PurchaseOrderLine, Item)
                .join(Item, PurchaseOrderLine.item_id == Item.id)
                .order_by(Item.sku)
            ).all()
        )
        assert [item.sku for _line, item in line_rows] == ["A-1", "B-2", "C-3"]
        assert [line.qty_ordered for line, _item in line_rows] == [
            Decimal("20"),
            Decimal("30"),
            Decimal("10"),
        ]

    def test_above_threshold_items_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="LOW",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("100"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="OK",
            current_qty=Decimal("50"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("100"),
            supplier=sup,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        lines = list(
            db_session.execute(
                select(PurchaseOrderLine, Item).join(
                    Item, PurchaseOrderLine.item_id == Item.id
                )
            ).all()
        )
        assert [item.sku for _line, item in lines] == ["LOW"]

    def test_archived_items_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="LIVE",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="DEAD",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
            archived=True,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        lines = list(
            db_session.execute(
                select(PurchaseOrderLine, Item).join(
                    Item, PurchaseOrderLine.item_id == Item.id
                )
            ).all()
        )
        assert [item.sku for _line, item in lines] == ["LIVE"]

    def test_other_suppliers_items_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup_a = _make_supplier(db_session, name="ACME")
        sup_b = _make_supplier(db_session, name="OtherCo")
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="A-MINE",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup_a,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="B-NOTMINE",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup_b,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup_a.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        lines = list(
            db_session.execute(
                select(PurchaseOrderLine, Item).join(
                    Item, PurchaseOrderLine.item_id == Item.id
                )
            ).all()
        )
        assert [item.sku for _line, item in lines] == ["A-MINE"]
        # And the PO is bound to sup_a, not sup_b.
        po = db_session.execute(select(PurchaseOrder)).scalar_one()
        assert po.supplier_id == sup_a.id

    def test_audit_row_carries_full_lines_snapshot(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="AUD-1",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("50"),
            supplier=sup,
        )
        _seed_layer(
            db_session,
            item=item,
            actor=u,
            qty=Decimal("100"),
            unit_cost=Decimal("3.50"),
        )
        # Manually re-mark the item as below threshold (the layer would have
        # bumped current_qty above threshold) so the create path picks it up.
        item.current_qty = Decimal("0")
        db_session.commit()

        _login_as(client, u)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _po_audit_rows(db_session, action="purchase_order.created")
        assert len(rows) == 1
        after = rows[0].after_json
        assert after is not None
        assert after["supplier_id"] == sup.id
        assert after["status"] == "draft"
        assert after["expected_date"] is None
        assert after["notes"] is None
        assert len(after["lines"]) == 1
        line = after["lines"][0]
        assert line["item_id"] == item.id
        # ``reorder_qty`` round-trips through Numeric(14,4) → scale 4.
        assert line["qty_ordered"] == "50.0000"
        assert line["expected_unit_cost"] == "3.5000"

    def test_flash_carries_supplier_and_line_count(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="Bullion Co")
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="F-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="F-2",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        # The follow_redirects=True path lands on the detail page; the flash
        # message includes supplier name + line count.
        assert "Bullion Co" in resp.text
        assert "2 line" in resp.text


# ---------------------------------------------------------------------------
# Default qty_ordered selection
# ---------------------------------------------------------------------------


class TestDefaultQtyOrdered:
    def _setup(self, db: Session, client: TestClient) -> tuple[Supplier, TaxonomyNode]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        _login_as(client, u)
        return sup, leaf

    def test_uses_reorder_qty_when_positive(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup, leaf = self._setup(db_session, client)
        _make_item(
            db_session,
            leaf=leaf,
            sku="Q-1",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("75"),
            supplier=sup,
        )
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        line = db_session.execute(select(PurchaseOrderLine)).scalar_one()
        assert line.qty_ordered == Decimal("75")

    def test_falls_back_to_deficit_when_reorder_qty_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        sup, leaf = self._setup(db_session, client)
        _make_item(
            db_session,
            leaf=leaf,
            sku="Q-2",
            current_qty=Decimal("3"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("0"),
            supplier=sup,
        )
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        line = db_session.execute(select(PurchaseOrderLine)).scalar_one()
        assert line.qty_ordered == Decimal("7")  # 10 - 3

    def test_falls_back_to_one_when_zero_qty_and_zero_deficit(
        self, client: TestClient, db_session: Session
    ) -> None:
        """At-threshold-zero-reorder cohort: order at least one."""
        sup, leaf = self._setup(db_session, client)
        _make_item(
            db_session,
            leaf=leaf,
            sku="Q-3",
            current_qty=Decimal("0"),
            threshold=Decimal("0"),
            reorder_qty=Decimal("0"),
            supplier=sup,
        )
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        line = db_session.execute(select(PurchaseOrderLine)).scalar_one()
        assert line.qty_ordered == Decimal("1")


# ---------------------------------------------------------------------------
# Default expected_unit_cost selection
# ---------------------------------------------------------------------------


class TestDefaultExpectedUnitCost:
    def _setup(
        self, db: Session, client: TestClient
    ) -> tuple[User, Supplier, TaxonomyNode]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        _login_as(client, u)
        return u, sup, leaf

    def test_no_layers_yields_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        _, sup, leaf = self._setup(db_session, client)
        _make_item(
            db_session,
            leaf=leaf,
            sku="NL-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        line = db_session.execute(select(PurchaseOrderLine)).scalar_one()
        assert line.expected_unit_cost is None

    def test_single_layer_used(
        self, client: TestClient, db_session: Session
    ) -> None:
        u, sup, leaf = self._setup(db_session, client)
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="SL-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _seed_layer(
            db_session, item=item, actor=u, qty=Decimal("10"), unit_cost=Decimal("4.25")
        )
        item.current_qty = Decimal("0")
        db_session.commit()

        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        line = db_session.execute(select(PurchaseOrderLine)).scalar_one()
        assert line.expected_unit_cost == Decimal("4.25")

    def test_newest_layer_picked_when_multiple(
        self, client: TestClient, db_session: Session
    ) -> None:
        u, sup, leaf = self._setup(db_session, client)
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="ML-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        old = datetime(2026, 1, 1, tzinfo=UTC)
        _seed_layer(
            db_session,
            item=item,
            actor=u,
            qty=Decimal("10"),
            unit_cost=Decimal("2.00"),
            received_at=old,
        )
        new = old + timedelta(days=10)
        _seed_layer(
            db_session,
            item=item,
            actor=u,
            qty=Decimal("5"),
            unit_cost=Decimal("3.00"),
            received_at=new,
        )
        item.current_qty = Decimal("0")
        db_session.commit()

        resp = client.post(
            "/admin/reorder/draft-po",
            data={"supplier_id": str(sup.id), "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        line = db_session.execute(select(PurchaseOrderLine)).scalar_one()
        # Newest layer wins → unit_cost == 3.00.
        assert line.expected_unit_cost == Decimal("3.0000")


# ---------------------------------------------------------------------------
# GET list view
# ---------------------------------------------------------------------------


class TestPOListView:
    def test_empty_state_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 200
        assert 'data-testid="po-list-empty"' in resp.text
        assert 'data-testid="po-row"' not in resp.text

    def test_populated_list_newest_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        # Two POs; newer one second insertion → should appear first.
        po1 = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        db_session.add(po1)
        db_session.commit()
        po2 = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add(po2)
        db_session.commit()

        _login_as(client, u)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 200
        # Both rows appear; po2 (newer) before po1.
        po2_idx = resp.text.find(f'data-po-id="{po2.id}"')
        po1_idx = resp.text.find(f'data-po-id="{po1.id}"')
        assert 0 < po2_idx < po1_idx

    def test_status_filter_draft_only_shows_drafts(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        draft = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        sent = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add_all([draft, sent])
        db_session.commit()
        db_session.refresh(draft)
        db_session.refresh(sent)

        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?status_filter=draft")
        assert resp.status_code == 200
        assert f'data-po-id="{draft.id}"' in resp.text
        assert f'data-po-id="{sent.id}"' not in resp.text

    def test_status_filter_sent_excludes_drafts(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        draft = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        sent = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add_all([draft, sent])
        db_session.commit()
        db_session.refresh(draft)
        db_session.refresh(sent)

        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?status_filter=sent")
        assert resp.status_code == 200
        assert f'data-po-id="{sent.id}"' in resp.text
        assert f'data-po-id="{draft.id}"' not in resp.text


# ---------------------------------------------------------------------------
# GET detail view
# ---------------------------------------------------------------------------


class TestPODetailView:
    def test_unknown_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders/9999")
        assert resp.status_code == 404

    def test_renders_heading_supplier_status(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="Bullion Co")
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        db_session.add(po)
        db_session.commit()
        db_session.refresh(po)

        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert f"Purchase order #{po.id}" in resp.text
        assert "Bullion Co" in resp.text
        assert 'data-testid="po-status-badge"' in resp.text
        assert 'data-status="draft"' in resp.text

    def test_lines_render_in_sku_order_with_correct_cells(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Non-draft status so the read-only render path is exercised; PO2b
        # turns drafts into edit forms (input cells, not text cells).
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        item_b = _make_item(
            db_session, leaf=leaf, sku="B-2", supplier=sup, name="B item"
        )
        item_a = _make_item(
            db_session, leaf=leaf, sku="A-1", supplier=sup, name="A item"
        )
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add(po)
        db_session.flush()
        line_b = PurchaseOrderLine(
            po_id=po.id,
            item_id=item_b.id,
            qty_ordered=Decimal("20"),
            qty_received=Decimal("0"),
            expected_unit_cost=Decimal("2.50"),
        )
        line_a = PurchaseOrderLine(
            po_id=po.id,
            item_id=item_a.id,
            qty_ordered=Decimal("10"),
            qty_received=Decimal("0"),
            expected_unit_cost=None,
        )
        db_session.add_all([line_b, line_a])
        db_session.commit()
        db_session.refresh(po)

        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        # Two rows; A-1 before B-2.
        a_idx = resp.text.find('data-testid="po-line-sku">A-1')
        b_idx = resp.text.find('data-testid="po-line-sku">B-2')
        assert 0 < a_idx < b_idx
        # Cells.
        assert resp.text.count('data-testid="po-line-row"') == 2
        # Slice to A's row (between A-1 marker and B-2 marker) for cell asserts.
        a_row = resp.text[a_idx:b_idx]
        assert 'data-testid="po-line-qty-ordered">10' in a_row
        assert 'data-testid="po-line-qty-received">0' in a_row
        assert 'data-testid="po-line-cost-empty"' in a_row  # null cost marker
        b_row = resp.text[b_idx : b_idx + 2000]
        assert 'data-testid="po-line-qty-ordered">20' in b_row
        assert 'data-testid="po-line-expected-unit-cost">' in b_row
        # The actual rendered Decimal carries the Numeric(14,4) scale.
        assert "2.50" in b_row

    def test_null_expected_cost_shows_empty_marker(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Non-draft status — the empty-cost marker only appears on the
        # read-only render path. Drafts render an empty input instead.
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="N-1", supplier=sup)
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add(po)
        db_session.flush()
        db_session.add(
            PurchaseOrderLine(
                po_id=po.id,
                item_id=item.id,
                qty_ordered=Decimal("10"),
                qty_received=Decimal("0"),
                expected_unit_cost=None,
            )
        )
        db_session.commit()
        db_session.refresh(po)

        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-line-cost-empty"' in resp.text


# ---------------------------------------------------------------------------
# PO2b — edit + cancel a draft PO
# ---------------------------------------------------------------------------


def _make_draft_po(
    db: Session,
    *,
    supplier: Supplier,
    leaf: TaxonomyNode,
    actor: User,
    skus: list[str] | None = None,
    qty_ordered: Decimal = Decimal("10"),
    expected_unit_cost: Decimal | None = None,
    po_status: POStatus = POStatus.DRAFT,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    """Create a PO + one line per SKU directly (skipping the create route)."""
    skus = skus or ["EDIT-1"]
    po = PurchaseOrder(
        supplier_id=supplier.id, status=po_status, created_by=actor.id
    )
    db.add(po)
    db.flush()
    lines: list[PurchaseOrderLine] = []
    for sku in skus:
        item = _make_item(db, leaf=leaf, sku=sku, supplier=supplier)
        line = PurchaseOrderLine(
            po_id=po.id,
            item_id=item.id,
            qty_ordered=qty_ordered,
            qty_received=Decimal("0"),
            expected_unit_cost=expected_unit_cost,
        )
        db.add(line)
        lines.append(line)
    db.commit()
    db.refresh(po)
    for line in lines:
        db.refresh(line)
    return po, lines


def _form_data_for_po(
    *,
    po: PurchaseOrder,
    lines: list[PurchaseOrderLine],
    csrf: str,
    expected_date: str | None = None,
    notes: str | None = None,
    qty_overrides: dict[int, str] | None = None,
    cost_overrides: dict[int, str] | None = None,
) -> dict[str, str]:
    """Build the form-encoded payload for the edit route.

    Defaults preserve current values (no-op submit). Overrides keyed by line
    id replace the per-line fields.
    """
    qty_overrides = qty_overrides or {}
    cost_overrides = cost_overrides or {}
    data: dict[str, str] = {"csrf_token": csrf}
    data["expected_date"] = (
        expected_date
        if expected_date is not None
        else (po.expected_date.isoformat() if po.expected_date else "")
    )
    data["notes"] = notes if notes is not None else (po.notes or "")
    for line in lines:
        qty_key = f"qty_ordered_{line.id}"
        cost_key = f"expected_unit_cost_{line.id}"
        data[qty_key] = (
            qty_overrides[line.id]
            if line.id in qty_overrides
            else str(line.qty_ordered)
        )
        data[cost_key] = (
            cost_overrides[line.id]
            if line.id in cost_overrides
            else (
                str(line.expected_unit_cost)
                if line.expected_unit_cost is not None
                else ""
            )
        )
    return data


class TestPOEditRoleEnforcement:
    def test_anonymous_post_update_is_401(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/purchase-orders/1",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_anonymous_post_cancel_is_401(
        self, client: TestClient
    ) -> None:
        resp = client.post(
            "/admin/purchase-orders/1/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_pending_post_update_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/purchase-orders/1",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_pending_post_cancel_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.post(
            "/admin/purchase-orders/1/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_workshop_post_update_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/purchase-orders/1",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_workshop_post_cancel_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/purchase-orders/1/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_manager_post_update_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        po, lines = _make_draft_po(
            db_session, supplier=sup, leaf=leaf, actor=u, skus=["E-1"]
        )
        _login_as(client, u)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po, lines=lines, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_office_post_update_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        creator = _make_user(db_session, email="c@x.test", role=Role.MANAGER)
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        po, lines = _make_draft_po(
            db_session, supplier=sup, leaf=leaf, actor=creator, skus=["E-2"]
        )
        _login_as(client, u)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po, lines=lines, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_manager_post_cancel_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        po, _lines = _make_draft_po(
            db_session, supplier=sup, leaf=leaf, actor=u, skus=["E-3"]
        )
        _login_as(client, u)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestPOEditStatusGuard:
    def _setup_po(
        self,
        db: Session,
        client: TestClient,
        *,
        po_status: POStatus,
    ) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["G-1"],
            po_status=po_status,
        )
        _login_as(client, u)
        return po, lines

    def test_edit_sent_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup_po(
            db_session, client, po_status=POStatus.SENT
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po, lines=lines, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_edit_received_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup_po(
            db_session, client, po_status=POStatus.RECEIVED
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po, lines=lines, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_cancel_sent_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, _lines = self._setup_po(
            db_session, client, po_status=POStatus.SENT
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_cancel_already_cancelled_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, _lines = self._setup_po(
            db_session, client, po_status=POStatus.CANCELLED
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_edit_unknown_po_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/purchase-orders/9999",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_cancel_unknown_po_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/purchase-orders/9999/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


class TestPOEditValidation:
    def _setup(
        self,
        db: Session,
        client: TestClient,
    ) -> tuple[User, PurchaseOrder, list[PurchaseOrderLine]]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, lines = _make_draft_po(
            db, supplier=sup, leaf=leaf, actor=u, skus=["V-1"]
        )
        _login_as(client, u)
        return u, po, lines

    def test_bad_expected_date_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            expected_date="not-a-date",
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_notes_over_limit_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po, lines=lines, csrf=_csrf(client), notes="x" * 2001
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_qty_blank_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            qty_overrides={lines[0].id: ""},
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_qty_zero_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            qty_overrides={lines[0].id: "0"},
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_qty_negative_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            qty_overrides={lines[0].id: "-5"},
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_qty_non_numeric_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            qty_overrides={lines[0].id: "abc"},
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_cost_negative_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            cost_overrides={lines[0].id: "-1"},
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_cost_non_numeric_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po,
            lines=lines,
            csrf=_csrf(client),
            cost_overrides={lines[0].id: "abc"},
        )
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_missing_line_qty_field_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po, lines=lines, csrf=_csrf(client)
        )
        del data[f"qty_ordered_{lines[0].id}"]
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_missing_line_cost_field_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        data = _form_data_for_po(
            po=po, lines=lines, csrf=_csrf(client)
        )
        del data[f"expected_unit_cost_{lines[0].id}"]
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=data,
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_validation_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client)
        original_qty = lines[0].qty_ordered
        # Snapshot the current audit row count for this PO (one for create).
        existing = len(_po_audit_rows(db_session))
        client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                qty_overrides={lines[0].id: "0"},  # invalid
            ),
            follow_redirects=False,
        )
        db_session.expire_all()
        # No new audit row.
        assert len(_po_audit_rows(db_session)) == existing
        # Line value unchanged.
        line = db_session.get(PurchaseOrderLine, lines[0].id)
        assert line is not None
        assert line.qty_ordered == original_qty


class TestPOEditHappyPath:
    def _setup(
        self,
        db: Session,
        client: TestClient,
        *,
        skus: list[str] | None = None,
        expected_unit_cost: Decimal | None = None,
    ) -> tuple[User, PurchaseOrder, list[PurchaseOrderLine]]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=skus,
            expected_unit_cost=expected_unit_cost,
        )
        _login_as(client, u)
        return u, po, lines

    def test_change_qty_writes_audit_with_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client, skus=["H-1"])
        line_id = lines[0].id
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                qty_overrides={line_id: "42"},
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        line = db_session.get(PurchaseOrderLine, line_id)
        assert line is not None
        assert line.qty_ordered == Decimal("42")

        rows = _po_audit_rows(db_session, action="purchase_order.updated")
        assert len(rows) == 1
        before = rows[0].before_json
        after = rows[0].after_json
        assert before is not None
        assert after is not None
        assert before["lines"] == [
            {"line_id": line_id, "qty_ordered": "10.0000"}
        ]
        assert after["lines"] == [
            {"line_id": line_id, "qty_ordered": "42"}
        ]
        # No top-level keys when only line changed.
        assert "expected_date" not in before
        assert "notes" not in before

    def test_change_cost_from_null_to_value(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client, skus=["H-2"])
        line_id = lines[0].id
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                cost_overrides={line_id: "1.50"},
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        line = db_session.get(PurchaseOrderLine, line_id)
        assert line is not None
        assert line.expected_unit_cost == Decimal("1.50")

        rows = _po_audit_rows(db_session, action="purchase_order.updated")
        assert len(rows) == 1
        assert rows[0].before_json is not None
        assert rows[0].before_json["lines"] == [
            {"line_id": line_id, "expected_unit_cost": None}
        ]
        assert rows[0].after_json is not None
        assert rows[0].after_json["lines"] == [
            {"line_id": line_id, "expected_unit_cost": "1.50"}
        ]

    def test_clear_cost_to_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(
            db_session,
            client,
            skus=["H-3"],
            expected_unit_cost=Decimal("2.50"),
        )
        line_id = lines[0].id
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                cost_overrides={line_id: ""},
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        line = db_session.get(PurchaseOrderLine, line_id)
        assert line is not None
        assert line.expected_unit_cost is None

    def test_change_top_level_notes(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client, skus=["H-4"])
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                notes="please rush",
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.notes == "please rush"

        rows = _po_audit_rows(db_session, action="purchase_order.updated")
        assert len(rows) == 1
        assert rows[0].before_json is not None
        assert rows[0].before_json.get("notes") is None
        assert rows[0].after_json is not None
        assert rows[0].after_json.get("notes") == "please rush"

    def test_change_top_level_expected_date(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client, skus=["H-5"])
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                expected_date="2026-12-25",
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.expected_date == date(2026, 12, 25)

        rows = _po_audit_rows(db_session, action="purchase_order.updated")
        assert len(rows) == 1
        assert rows[0].after_json is not None
        assert rows[0].after_json.get("expected_date") == "2026-12-25"

    def test_multi_line_only_changed_lines_in_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(
            db_session, client, skus=["M-A", "M-B", "M-C"]
        )
        # Change only line[1].
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                qty_overrides={lines[1].id: "55"},
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _po_audit_rows(db_session, action="purchase_order.updated")
        assert len(rows) == 1
        before_lines = rows[0].before_json["lines"]
        assert len(before_lines) == 1
        assert before_lines[0]["line_id"] == lines[1].id

    def test_no_op_submit_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client, skus=["N-1"])
        existing = len(_po_audit_rows(db_session))
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po, lines=lines, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # No new audit row.
        assert len(_po_audit_rows(db_session)) == existing

    def test_flash_includes_saved(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po, lines = self._setup(db_session, client, skus=["F-1"])
        resp = client.post(
            f"/admin/purchase-orders/{po.id}",
            data=_form_data_for_po(
                po=po,
                lines=lines,
                csrf=_csrf(client),
                qty_overrides={lines[0].id: "20"},
            ),
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "saved" in resp.text.lower()


class TestPOCancelHappyPath:
    def _setup(
        self, db: Session, client: TestClient
    ) -> tuple[User, PurchaseOrder]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db, supplier=sup, leaf=leaf, actor=u, skus=["C-1"]
        )
        _login_as(client, u)
        return u, po

    def test_cancel_flips_status(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po = self._setup(db_session, client)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.CANCELLED

    def test_cancel_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po = self._setup(db_session, client)
        client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        rows = _po_audit_rows(
            db_session, action="purchase_order.cancelled"
        )
        assert len(rows) == 1
        assert rows[0].before_json == {"status": "draft"}
        assert rows[0].after_json == {"status": "cancelled"}
        assert rows[0].entity_id == po.id

    def test_cancel_redirects_to_detail(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po = self._setup(db_session, client)
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == f"/admin/purchase-orders/{po.id}"
        )

    def test_cancel_preserves_lines_and_supplier(
        self, client: TestClient, db_session: Session
    ) -> None:
        _u, po = self._setup(db_session, client)
        original_supplier_id = po.supplier_id
        original_line_count = db_session.scalar(
            select(func.count(PurchaseOrderLine.id)).where(
                PurchaseOrderLine.po_id == po.id
            )
        )
        client.post(
            f"/admin/purchase-orders/{po.id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.supplier_id == original_supplier_id
        new_line_count = db_session.scalar(
            select(func.count(PurchaseOrderLine.id)).where(
                PurchaseOrderLine.po_id == po.id
            )
        )
        assert new_line_count == original_line_count


class TestPODetailRenderDelta:
    def _setup_po(
        self,
        db: Session,
        client: TestClient,
        po_status: POStatus,
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["RD-1"],
            po_status=po_status,
        )
        _login_as(client, u)
        return po

    def test_draft_renders_edit_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.DRAFT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-edit-form"' in resp.text
        assert 'data-testid="po-edit-submit"' in resp.text
        assert 'data-testid="po-cancel-form"' in resp.text
        assert 'data-testid="po-cancel-submit"' in resp.text
        assert 'data-testid="po-edit-qty-input"' in resp.text
        assert 'data-testid="po-edit-cost-input"' in resp.text
        assert 'data-testid="po-edit-expected-date-input"' in resp.text
        assert 'data-testid="po-edit-notes-input"' in resp.text
        assert 'data-testid="po-readonly-banner"' not in resp.text

    def test_sent_renders_readonly(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.SENT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-readonly-banner"' in resp.text
        assert 'data-testid="po-edit-form"' not in resp.text
        assert 'data-testid="po-edit-submit"' not in resp.text
        assert 'data-testid="po-cancel-form"' not in resp.text

    def test_received_renders_readonly(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.RECEIVED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-readonly-banner"' in resp.text
        assert 'data-testid="po-edit-submit"' not in resp.text

    def test_cancelled_renders_readonly(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.CANCELLED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-readonly-banner"' in resp.text
        assert 'data-testid="po-edit-submit"' not in resp.text

    def test_draft_input_values_pre_filled(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.DRAFT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        # The qty input pre-fills with the line's qty_ordered (10 → "10.0000").
        assert 'value="10.0000"' in resp.text


# ---------------------------------------------------------------------------
# PO3 — PDF rendering
# ---------------------------------------------------------------------------


def _assert_is_pdf(resp: Any) -> None:
    """The response is a well-formed PDF."""
    assert resp.headers["content-type"].startswith("application/pdf")
    assert resp.content[:4] == b"%PDF"


class TestPOPdfRoleEnforcement:
    def _make_po(
        self, db: Session, *, po_status: POStatus = POStatus.DRAFT
    ) -> PurchaseOrder:
        actor = _make_user(db, email="creator@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=actor,
            skus=["PDF-1"],
            po_status=po_status,
        )
        return po

    def test_anonymous_get_pdf_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 401

    def test_pending_get_pdf_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 403

    def test_workshop_get_pdf_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 403

    def test_manager_get_pdf_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 200
        _assert_is_pdf(resp)

    def test_office_get_pdf_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 200
        _assert_is_pdf(resp)

    def test_admin_get_pdf_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 200
        _assert_is_pdf(resp)


class TestPOPdfStatusGuard:
    def _make(
        self, db: Session, client: TestClient, *, po_status: POStatus
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["PDF-2"],
            po_status=po_status,
        )
        _login_as(client, u)
        return po

    def test_unknown_po_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders/9999/pdf")
        assert resp.status_code == 404

    def test_cancelled_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.CANCELLED)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 400

    def test_draft_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.DRAFT)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 200
        _assert_is_pdf(resp)

    def test_sent_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.SENT)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 200
        _assert_is_pdf(resp)

    def test_received_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.RECEIVED)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.status_code == 200
        _assert_is_pdf(resp)


class TestPOPdfContent:
    def _setup(
        self,
        db: Session,
        client: TestClient,
        *,
        priced: bool = True,
        supplier_archived: bool = False,
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(
            db,
            name="Pdf Bullion Co",
            archived=supplier_archived,
        )
        leaf = _make_leaf(db)
        cost: Decimal | None = Decimal("2.50") if priced else None
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["PDF-CONTENT-1"],
            qty_ordered=Decimal("12"),
            expected_unit_cost=cost,
        )
        _login_as(client, u)
        return po

    def test_content_type_is_pdf(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.headers["content-type"].startswith("application/pdf")

    def test_inline_disposition_with_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        cd = resp.headers["content-disposition"]
        assert cd.startswith("inline")
        assert f'filename="po-{po.id}.pdf"' in cd

    def test_bytes_start_with_pdf_magic(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert resp.content[:4] == b"%PDF"

    def test_po_id_appears_in_pdf(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        # PDF compression is disabled in the renderer for byte-search.
        assert f"#{po.id}".encode() in resp.content

    def test_supplier_name_appears_in_pdf(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert b"Pdf Bullion Co" in resp.content

    def test_archived_supplier_renders_suffix(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, supplier_archived=True)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        # Parens are PDF-string delimiters; reportlab escapes them as \( \).
        assert rb"\(archived\)" in resp.content

    def test_line_sku_and_qty_in_pdf(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        assert b"PDF-CONTENT-1" in resp.content
        # qty_ordered=12 round-trips from Numeric(14,4) as "12.0000".
        assert b"12.0000" in resp.content

    def test_priced_line_shows_unit_cost_and_total(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, priced=True)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        # qty=12 + cost=2.50 round-trip from Numeric(14,4) as 12.0000 + 2.5000;
        # Decimal multiplication adds scales → product is "30.00000000".
        assert b"2.5000" in resp.content
        assert b"Total: 30.00000000" in resp.content

    def test_unpriced_line_shows_em_dash_for_total(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, priced=False)
        resp = client.get(f"/admin/purchase-orders/{po.id}/pdf")
        # No priced line → grand total is "—". Reportlab encodes the em-dash
        # via WinAnsi (byte 0x97), then escapes it in the literal string as
        # the octal sequence "\227" (4 ASCII bytes).
        assert rb"Total: \227" in resp.content


class TestPOPdfLink:
    def _setup_po(
        self,
        db: Session,
        client: TestClient,
        po_status: POStatus,
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["PDF-LINK-1"],
            po_status=po_status,
        )
        _login_as(client, u)
        return po

    def test_draft_renders_pdf_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.DRAFT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-pdf-link"' in resp.text
        assert f"/admin/purchase-orders/{po.id}/pdf" in resp.text

    def test_sent_renders_pdf_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.SENT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-pdf-link"' in resp.text

    def test_received_renders_pdf_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.RECEIVED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-pdf-link"' in resp.text

    def test_cancelled_does_not_render_pdf_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup_po(db_session, client, POStatus.CANCELLED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-pdf-link"' not in resp.text


# ---------------------------------------------------------------------------
# PO4 — Send PO PDF to supplier (draft → sent)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_email_outbox() -> None:
    """Drain the in-memory console outbox before every test in this module."""
    clear_console_outbox()


def _send_po(
    client: TestClient, po_id: int
) -> Any:
    return client.post(
        f"/admin/purchase-orders/{po_id}/send",
        data={"csrf_token": _csrf(client)},
        follow_redirects=False,
    )


class TestPOSendRoleEnforcement:
    def _make_po(self, db: Session) -> PurchaseOrder:
        actor = _make_user(db, email="creator@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME", email="acme@example.test")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=actor,
            skus=["SEND-1"],
            po_status=POStatus.DRAFT,
        )
        return po

    def test_anonymous_post_send_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        resp = _send_po(client, po.id)
        assert resp.status_code == 401
        assert console_outbox() == []

    def test_pending_post_send_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 403
        assert console_outbox() == []

    def test_workshop_post_send_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = _send_po(client, po.id)
        assert resp.status_code == 403
        assert console_outbox() == []

    def test_manager_post_send_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 303

    def test_office_post_send_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 303

    def test_admin_post_send_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make_po(db_session)
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 303


class TestPOSendStatusGuard:
    def _make(
        self, db: Session, client: TestClient, *, po_status: POStatus
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="ACME", email="acme@example.test")
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["SEND-2"],
            po_status=po_status,
        )
        _login_as(client, u)
        return po

    def test_unknown_po_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = _send_po(client, 9999)
        assert resp.status_code == 404
        assert console_outbox() == []

    def test_sent_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.SENT)
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        assert console_outbox() == []

    def test_partially_received_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(
            db_session, client, po_status=POStatus.PARTIALLY_RECEIVED
        )
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        assert console_outbox() == []

    def test_received_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.RECEIVED)
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        assert console_outbox() == []

    def test_cancelled_po_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session, client, po_status=POStatus.CANCELLED)
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        assert console_outbox() == []


class TestPOSendValidation:
    def test_supplier_with_no_email_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="No Email Co", email=None)
        leaf = _make_leaf(db_session)
        po, _lines = _make_draft_po(
            db_session,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["SEND-NOE-1"],
        )
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        # No status flip + no audit + no outbox.
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.DRAFT
        assert po_reloaded.sent_at is None
        assert _po_audit_rows(db_session, action="purchase_order.sent") == []
        assert console_outbox() == []

    def test_supplier_with_blank_email_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="Blank Email Co", email="   ")
        leaf = _make_leaf(db_session)
        po, _lines = _make_draft_po(
            db_session,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["SEND-NOE-2"],
        )
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        assert console_outbox() == []

    def test_archived_supplier_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Supplier starts active so the draft can be created; we archive it
        # *after*, simulating a stale POST.
        sup = _make_supplier(
            db_session, name="Will Archive", email="archive@example.test"
        )
        leaf = _make_leaf(db_session)
        po, _lines = _make_draft_po(
            db_session,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["SEND-ARC-1"],
        )
        sup.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, u)
        resp = _send_po(client, po.id)
        assert resp.status_code == 400
        assert console_outbox() == []


class TestPOSendHappyPath:
    def _setup(
        self,
        db: Session,
        client: TestClient,
        *,
        supplier_email: str = "supplier@example.test",
        supplier_name: str = "Send Bullion Co",
        notes: str | None = None,
        expected_date: date | None = None,
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(
            db, name=supplier_name, email=supplier_email
        )
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["SEND-OK-1"],
            qty_ordered=Decimal("12"),
            expected_unit_cost=Decimal("2.50"),
        )
        if notes is not None or expected_date is not None:
            po.notes = notes
            po.expected_date = expected_date
            db.commit()
            db.refresh(po)
        _login_as(client, u)
        return po

    def test_status_flips_to_sent(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = _send_po(client, po.id)
        assert resp.status_code == 303
        db_session.expire_all()
        reloaded = db_session.get(PurchaseOrder, po.id)
        assert reloaded is not None
        assert reloaded.status == POStatus.SENT

    def test_sent_at_is_set_to_now(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        before = datetime.now(UTC)
        resp = _send_po(client, po.id)
        after = datetime.now(UTC)
        assert resp.status_code == 303
        db_session.expire_all()
        reloaded = db_session.get(PurchaseOrder, po.id)
        assert reloaded is not None
        assert reloaded.sent_at is not None
        # SQLite returns sent_at as naive; the route writes a UTC datetime,
        # so attach UTC tzinfo for an apples-to-apples comparison.
        sent_at = reloaded.sent_at
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=UTC)
        assert (
            before - timedelta(seconds=2)
            <= sent_at
            <= after + timedelta(seconds=2)
        )

    def test_audit_row_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        _send_po(client, po.id)
        rows = _po_audit_rows(db_session, action="purchase_order.sent")
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_type == "purchase_order"
        assert row.entity_id == po.id
        assert row.before_json == {"status": "draft"}
        after = row.after_json
        assert after is not None
        assert after["status"] == "sent"
        assert after["to_email"] == "supplier@example.test"
        assert isinstance(after["sent_at"], str)
        # ISO with timezone — round-trip parses cleanly.
        parsed = datetime.fromisoformat(after["sent_at"])
        assert parsed.tzinfo is not None

    def test_redirect_target_is_detail_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = _send_po(client, po.id)
        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == f"/admin/purchase-orders/{po.id}"
        )

    def test_flash_includes_supplier_email(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        _send_po(client, po.id)
        # Follow the redirect to see the flashed message rendered.
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert "supplier@example.test" in resp.text

    def test_outbox_message_targets_supplier_with_pdf(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        _send_po(client, po.id)
        outbox = console_outbox()
        assert len(outbox) == 1
        msg = outbox[0]
        assert msg.recipient == "supplier@example.test"
        assert f"#{po.id}" in msg.subject
        assert "Send Bullion Co" in msg.html_body
        assert "1 line(s)" in msg.html_body
        # One PDF attachment with the right filename + magic bytes.
        assert len(msg.attachments) == 1
        att = msg.attachments[0]
        assert att.filename == f"po-{po.id}.pdf"
        assert att.content_type == "application/pdf"
        assert att.content[:4] == b"%PDF"

    def test_double_send_second_call_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The second call rejects via the status guard (PO is now sent)."""
        po = self._setup(db_session, client)
        resp1 = _send_po(client, po.id)
        assert resp1.status_code == 303
        # Second call: PO is now sent → 400.
        resp2 = _send_po(client, po.id)
        assert resp2.status_code == 400
        # Outbox still only carries the first delivery.
        assert len(console_outbox()) == 1
        # Audit log still only carries one sent row.
        assert (
            len(_po_audit_rows(db_session, action="purchase_order.sent"))
            == 1
        )

    def test_failed_validation_writes_no_audit_or_outbox(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="No Email Co", email=None)
        leaf = _make_leaf(db_session)
        po, _lines = _make_draft_po(
            db_session,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["SEND-NOAUDIT-1"],
        )
        _login_as(client, u)
        _send_po(client, po.id)
        assert _po_audit_rows(db_session, action="purchase_order.sent") == []
        assert console_outbox() == []


class TestPOSendDetailRender:
    def _setup(
        self,
        db: Session,
        client: TestClient,
        *,
        po_status: POStatus = POStatus.DRAFT,
        email: str | None = "render@example.test",
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db, name="Render Co", email=email)
        leaf = _make_leaf(db)
        po, _lines = _make_draft_po(
            db,
            supplier=sup,
            leaf=leaf,
            actor=u,
            skus=["RENDER-1"],
            po_status=po_status,
        )
        _login_as(client, u)
        return po

    def test_draft_with_email_shows_send_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-send-form"' in resp.text
        assert 'data-testid="po-send-submit"' in resp.text
        assert 'data-testid="po-send-blocked-no-email"' not in resp.text

    def test_draft_without_email_shows_blocked_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, email=None)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-send-form"' not in resp.text
        assert 'data-testid="po-send-submit"' not in resp.text
        assert 'data-testid="po-send-blocked-no-email"' in resp.text

    def test_sent_does_not_show_send_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, po_status=POStatus.SENT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-send-form"' not in resp.text
        assert 'data-testid="po-readonly-banner"' in resp.text

    def test_cancelled_does_not_show_send_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, po_status=POStatus.CANCELLED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-send-form"' not in resp.text


# ---------------------------------------------------------------------------
# PO5 — Receive against a PO
# ---------------------------------------------------------------------------


def _receive_po(
    client: TestClient,
    po_id: int,
    *,
    received: dict[int, str] | None = None,
    cost: dict[int, str] | None = None,
) -> Any:
    """Build a receive form post and submit it.

    ``received`` and ``cost`` are line-id-keyed dicts. Lines not in the dicts
    submit as blank (== zero received / no cost).
    """
    received = received or {}
    cost = cost or {}
    data: dict[str, str] = {"csrf_token": _csrf(client)}
    for line_id, qty in received.items():
        data[f"received_{line_id}"] = qty
    for line_id, cost_value in cost.items():
        data[f"cost_{line_id}"] = cost_value
    return client.post(
        f"/admin/purchase-orders/{po_id}/receive",
        data=data,
        follow_redirects=False,
    )


def _make_po_for_receive(
    db: Session,
    *,
    actor: User,
    supplier: Supplier | None = None,
    leaf: TaxonomyNode | None = None,
    skus: list[str] | None = None,
    qty_ordered: Decimal = Decimal("10"),
    expected_unit_cost: Decimal | None = Decimal("2.00"),
    po_status: POStatus = POStatus.SENT,
) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
    """Build a receivable PO (default status=sent) for the test cases."""
    sup = supplier or _make_supplier(
        db, name="ACME", email="acme@example.test"
    )
    lf = leaf or _make_leaf(db)
    po, lines = _make_draft_po(
        db,
        supplier=sup,
        leaf=lf,
        actor=actor,
        skus=skus or ["RECV-1"],
        qty_ordered=qty_ordered,
        expected_unit_cost=expected_unit_cost,
        po_status=po_status,
    )
    return po, lines


class TestPOReceiveRoleEnforcement:
    def _make(self, db: Session) -> PurchaseOrder:
        u = _make_user(db, email="creator@x.test", role=Role.MANAGER)
        po, _lines = _make_po_for_receive(db, actor=u)
        return po

    def test_anon_get_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 401

    def test_anon_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        resp = _receive_po(client, po.id)
        assert resp.status_code == 401

    def test_pending_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 403

    def test_pending_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = _receive_po(client, po.id)
        assert resp.status_code == 403

    def test_workshop_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 403

    def test_workshop_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = _receive_po(client, po.id)
        assert resp.status_code == 403

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200

    def test_admin_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._make(db_session)
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200


class TestPOReceiveStatusGuard:
    def _make(
        self,
        db: Session,
        client: TestClient,
        *,
        po_status: POStatus,
    ) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db, actor=u, po_status=po_status, skus=["GS-1"]
        )
        _login_as(client, u)
        return po, lines

    def test_unknown_po_get_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders/9999/receive")
        assert resp.status_code == 404

    def test_unknown_po_post_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = _receive_po(client, 9999)
        assert resp.status_code == 404

    def test_draft_get_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, _ = self._make(db_session, client, po_status=POStatus.DRAFT)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 400

    def test_draft_post_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._make(db_session, client, po_status=POStatus.DRAFT)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "5"},
            cost={lines[0].id: "2.00"},
        )
        assert resp.status_code == 400

    def test_received_get_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, _ = self._make(db_session, client, po_status=POStatus.RECEIVED)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 400

    def test_cancelled_get_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, _ = self._make(db_session, client, po_status=POStatus.CANCELLED)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 400

    def test_partially_received_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, _ = self._make(
            db_session, client, po_status=POStatus.PARTIALLY_RECEIVED
        )
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200


class TestPOReceiveValidation:
    def _setup(
        self,
        db: Session,
        client: TestClient,
        *,
        qty_ordered: Decimal = Decimal("10"),
    ) -> tuple[PurchaseOrder, list[PurchaseOrderLine]]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db, actor=u, skus=["VAL-1"], qty_ordered=qty_ordered
        )
        _login_as(client, u)
        return po, lines

    def test_negative_received_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "-1"},
            cost={lines[0].id: "2.00"},
        )
        assert resp.status_code == 400

    def test_non_numeric_received_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "abc"},
            cost={lines[0].id: "2.00"},
        )
        assert resp.status_code == 400

    def test_negative_cost_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "5"},
            cost={lines[0].id: "-1"},
        )
        assert resp.status_code == 400

    def test_non_numeric_cost_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "5"},
            cost={lines[0].id: "x"},
        )
        assert resp.status_code == 400

    def test_blank_cost_with_qty_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "5"},
            cost={lines[0].id: ""},
        )
        assert resp.status_code == 400

    def test_over_receipt_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        # qty_ordered=10, qty_received=0 → receiving 11 should reject.
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "11"},
            cost={lines[0].id: "2.00"},
        )
        assert resp.status_code == 400

    def test_over_receipt_after_partial_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        line_id = lines[0].id
        # First receipt: 6 of 10. After this, 4 outstanding.
        resp1 = _receive_po(
            client,
            po.id,
            received={line_id: "6"},
            cost={line_id: "2.00"},
        )
        assert resp1.status_code == 303
        # Second receipt: try 5 — should reject (only 4 outstanding).
        resp2 = _receive_po(
            client,
            po.id,
            received={line_id: "5"},
            cost={line_id: "2.00"},
        )
        assert resp2.status_code == 400

    def test_failed_validation_writes_no_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, lines = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={lines[0].id: "11"},
            cost={lines[0].id: "2.00"},
        )
        assert resp.status_code == 400
        # No movement, no cost layer, no audit row, status unchanged.
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.SENT
        line_reloaded = db_session.get(PurchaseOrderLine, lines[0].id)
        assert line_reloaded is not None
        assert line_reloaded.qty_received == Decimal("0")
        assert (
            db_session.execute(select(StockMovement)).first() is None
        )
        from app.models import CostLayer

        assert db_session.execute(select(CostLayer)).first() is None
        assert (
            _po_audit_rows(db_session, action="purchase_order.received") == []
        )


class TestPOReceiveHappyPathSingleFull:
    def _setup(
        self,
        db: Session,
        client: TestClient,
    ) -> tuple[PurchaseOrder, PurchaseOrderLine, Item, User]:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db, actor=u, skus=["FULL-1"], qty_ordered=Decimal("10")
        )
        item = db.get(Item, lines[0].item_id)
        assert item is not None
        _login_as(client, u)
        return po, lines[0], item, u

    def test_status_flips_to_received(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, _item, _u = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.RECEIVED

    def test_movement_created_with_po_id_and_type_in(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, item, _u = self._setup(db_session, client)
        _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        movements = list(
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.item_id == item.id
                )
            )
            .scalars()
            .all()
        )
        assert len(movements) == 1
        m = movements[0]
        assert m.type == MovementType.IN
        assert m.po_id == po.id
        assert m.qty == Decimal("10")
        assert m.total_cost == Decimal("25.0000")  # 10 * 2.50 (engine quantises)

    def test_cost_layer_created_with_po_receipt_source(
        self, client: TestClient, db_session: Session
    ) -> None:
        from app.models import CostLayer as _CostLayer
        po, line, item, _u = self._setup(db_session, client)
        _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        layers = list(
            db_session.execute(
                select(_CostLayer).where(_CostLayer.item_id == item.id)
            )
            .scalars()
            .all()
        )
        assert len(layers) == 1
        layer = layers[0]
        assert layer.source == CostLayerSource.PO_RECEIPT
        assert layer.qty_received == Decimal("10")
        assert layer.qty_remaining == Decimal("10")
        assert layer.unit_cost == Decimal("2.5000")

    def test_line_qty_received_bumped(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, _item, _u = self._setup(db_session, client)
        _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        db_session.expire_all()
        line_reloaded = db_session.get(PurchaseOrderLine, line.id)
        assert line_reloaded is not None
        assert line_reloaded.qty_received == Decimal("10")

    def test_item_current_qty_bumped(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, item, _u = self._setup(db_session, client)
        _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        db_session.expire_all()
        item_reloaded = db_session.get(Item, item.id)
        assert item_reloaded is not None
        assert item_reloaded.current_qty == Decimal("10.0000")

    def test_audit_row_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, _item, _u = self._setup(db_session, client)
        _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        rows = _po_audit_rows(db_session, action="purchase_order.received")
        assert len(rows) == 1
        row = rows[0]
        assert row.entity_type == "purchase_order"
        assert row.entity_id == po.id
        assert row.before_json == {"status": "sent"}
        after = row.after_json
        assert after is not None
        assert after["status"] == "received"
        assert isinstance(after["lines"], list)
        assert len(after["lines"]) == 1
        line_after = after["lines"][0]
        assert line_after["line_id"] == line.id
        assert line_after["received_qty"] == "10"
        assert line_after["actual_unit_cost"] == "2.50"
        assert isinstance(line_after["movement_id"], int)

    def test_redirect_target_is_detail(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, _item, _u = self._setup(db_session, client)
        resp = _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        assert resp.headers["location"] == f"/admin/purchase-orders/{po.id}"

    def test_flash_says_fully_received(
        self, client: TestClient, db_session: Session
    ) -> None:
        po, line, _item, _u = self._setup(db_session, client)
        _receive_po(
            client,
            po.id,
            received={line.id: "10"},
            cost={line.id: "2.50"},
        )
        # Follow redirect to render the flash.
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert "fully received" in resp.text.lower()


class TestPOReceiveHappyPathPartial:
    def test_partial_receive_flips_status_to_partially_received(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["PART-1"],
            qty_ordered=Decimal("10"),
        )
        line = lines[0]
        _login_as(client, u)
        resp = _receive_po(
            client,
            po.id,
            received={line.id: "5"},
            cost={line.id: "2.00"},
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.PARTIALLY_RECEIVED
        line_reloaded = db_session.get(PurchaseOrderLine, line.id)
        assert line_reloaded is not None
        assert line_reloaded.qty_received == Decimal("5")

    def test_second_receipt_completes_to_received(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["PART-2"],
            qty_ordered=Decimal("10"),
        )
        line = lines[0]
        _login_as(client, u)
        # First receipt: 4 of 10 → partially_received.
        _receive_po(
            client,
            po.id,
            received={line.id: "4"},
            cost={line.id: "2.00"},
        )
        # Second receipt: remaining 6 → received.
        _receive_po(
            client,
            po.id,
            received={line.id: "6"},
            cost={line.id: "2.20"},
        )
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.RECEIVED
        line_reloaded = db_session.get(PurchaseOrderLine, line.id)
        assert line_reloaded is not None
        assert line_reloaded.qty_received == Decimal("10")

    def test_partial_creates_two_cost_layers_at_different_unit_costs(
        self, client: TestClient, db_session: Session
    ) -> None:
        from app.models import CostLayer as _CostLayer

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["PART-3"],
            qty_ordered=Decimal("10"),
        )
        line = lines[0]
        item = db_session.get(Item, line.item_id)
        assert item is not None
        _login_as(client, u)
        _receive_po(
            client,
            po.id,
            received={line.id: "4"},
            cost={line.id: "2.00"},
        )
        _receive_po(
            client,
            po.id,
            received={line.id: "6"},
            cost={line.id: "2.20"},
        )
        layers = list(
            db_session.execute(
                select(_CostLayer)
                .where(_CostLayer.item_id == item.id)
                .order_by(_CostLayer.id)
            )
            .scalars()
            .all()
        )
        assert len(layers) == 2
        assert layers[0].unit_cost == Decimal("2.0000")
        assert layers[1].unit_cost == Decimal("2.2000")
        # Both must carry source=PO_RECEIPT.
        for layer in layers:
            assert layer.source == CostLayerSource.PO_RECEIPT


class TestPOReceiveHappyPathMultiLine:
    def test_mixed_full_and_partial_flips_to_partially_received(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["MULTI-A", "MULTI-B"],
            qty_ordered=Decimal("10"),
        )
        line_a, line_b = lines
        _login_as(client, u)
        # A fully received, B half received.
        resp = _receive_po(
            client,
            po.id,
            received={line_a.id: "10", line_b.id: "5"},
            cost={line_a.id: "2.00", line_b.id: "3.00"},
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.PARTIALLY_RECEIVED

    def test_zero_received_line_writes_no_movement(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["MULTI-C", "MULTI-D"],
            qty_ordered=Decimal("10"),
        )
        line_a, line_b = lines
        item_b = db_session.get(Item, line_b.item_id)
        assert item_b is not None
        _login_as(client, u)
        # Only A receives; B is left blank.
        _receive_po(
            client,
            po.id,
            received={line_a.id: "10"},
            cost={line_a.id: "2.00"},
        )
        db_session.expire_all()
        # No movement on item B.
        movements_b = list(
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.item_id == item_b.id
                )
            )
            .scalars()
            .all()
        )
        assert movements_b == []
        # Audit lines list has only the A entry.
        rows = _po_audit_rows(db_session, action="purchase_order.received")
        assert len(rows) == 1
        after = rows[0].after_json
        assert after is not None
        assert len(after["lines"]) == 1
        assert after["lines"][0]["line_id"] == line_a.id

    def test_all_movements_carry_po_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["MULTI-E", "MULTI-F"],
            qty_ordered=Decimal("10"),
        )
        line_a, line_b = lines
        _login_as(client, u)
        _receive_po(
            client,
            po.id,
            received={line_a.id: "10", line_b.id: "5"},
            cost={line_a.id: "2.00", line_b.id: "3.00"},
        )
        movements = list(
            db_session.execute(select(StockMovement)).scalars().all()
        )
        assert len(movements) == 2
        for m in movements:
            assert m.po_id == po.id
            assert m.type == MovementType.IN


class TestPOReceiveNoOpSubmit:
    def test_all_zero_submit_writes_no_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session, actor=u, skus=["NOOP-1"]
        )
        _login_as(client, u)
        # Send all-blanks (no received_<id> keys at all).
        resp = client.post(
            f"/admin/purchase-orders/{po.id}/receive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.SENT
        line_reloaded = db_session.get(PurchaseOrderLine, lines[0].id)
        assert line_reloaded is not None
        assert line_reloaded.qty_received == Decimal("0")
        # No movement, no cost layer, no audit row.
        from app.models import CostLayer as _CostLayer

        assert (
            db_session.execute(select(StockMovement)).first() is None
        )
        assert db_session.execute(select(_CostLayer)).first() is None
        assert (
            _po_audit_rows(db_session, action="purchase_order.received")
            == []
        )

    def test_explicit_zero_submit_writes_no_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session, actor=u, skus=["NOOP-2"]
        )
        line = lines[0]
        _login_as(client, u)
        resp = _receive_po(
            client,
            po.id,
            received={line.id: "0"},
            cost={line.id: "2.00"},
        )
        assert resp.status_code == 303
        db_session.expire_all()
        po_reloaded = db_session.get(PurchaseOrder, po.id)
        assert po_reloaded is not None
        assert po_reloaded.status == POStatus.SENT


class TestPOReceiveDetailRender:
    def _setup(
        self,
        db: Session,
        client: TestClient,
        *,
        po_status: POStatus,
    ) -> PurchaseOrder:
        u = _make_user(db, email="m@x.test", role=Role.MANAGER)
        po, _lines = _make_po_for_receive(
            db, actor=u, skus=["DR-1"], po_status=po_status
        )
        _login_as(client, u)
        return po

    def test_draft_does_not_show_receive_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, po_status=POStatus.DRAFT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-receive-link"' not in resp.text

    def test_sent_shows_receive_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, po_status=POStatus.SENT)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-receive-link"' in resp.text
        assert 'data-testid="po-readonly-banner"' in resp.text

    def test_partially_received_shows_receive_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(
            db_session, client, po_status=POStatus.PARTIALLY_RECEIVED
        )
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert resp.status_code == 200
        assert 'data-testid="po-receive-link"' in resp.text

    def test_received_does_not_show_receive_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, po_status=POStatus.RECEIVED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-receive-link"' not in resp.text

    def test_cancelled_does_not_show_receive_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        po = self._setup(db_session, client, po_status=POStatus.CANCELLED)
        resp = client.get(f"/admin/purchase-orders/{po.id}")
        assert 'data-testid="po-receive-link"' not in resp.text


class TestPOReceiveFormRender:
    def test_form_shows_lines_with_outstanding(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["RF-1"],
            qty_ordered=Decimal("10"),
        )
        _login_as(client, u)
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200
        assert 'data-testid="po-receive-form"' in resp.text
        assert 'data-testid="po-receive-line-row"' in resp.text
        assert f'data-line-id="{lines[0].id}"' in resp.text
        assert 'data-testid="po-receive-outstanding"' in resp.text
        # Outstanding for a fresh sent PO equals qty_ordered.
        assert "10.0000" in resp.text

    def test_form_after_partial_shows_remaining_outstanding(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        po, lines = _make_po_for_receive(
            db_session,
            actor=u,
            skus=["RF-2"],
            qty_ordered=Decimal("10"),
        )
        line = lines[0]
        _login_as(client, u)
        # Receive 4 → outstanding 6.
        _receive_po(
            client,
            po.id,
            received={line.id: "4"},
            cost={line.id: "2.00"},
        )
        resp = client.get(f"/admin/purchase-orders/{po.id}/receive")
        assert resp.status_code == 200
        # The outstanding cell should now read 6.0000.
        assert "6.0000" in resp.text


# ---------------------------------------------------------------------------
# CSV export (R5)
# ---------------------------------------------------------------------------


class TestPOListCsvRoleEnforcement:
    """``?format=csv`` inherits the same role gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 401

    def test_pending_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200

    def test_office_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200


class TestPOListCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="purchase_orders_all.csv"' in cd

    def test_content_disposition_status_filtered_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/purchase-orders?format=csv&status_filter=draft"
        )
        cd = resp.headers["content-disposition"]
        assert 'filename="purchase_orders_draft.csv"' in cd


class TestPOListCsvBody:
    def test_empty_emits_only_header_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200
        body = resp.text
        assert body == (
            "po_id,supplier,supplier_archived,status,line_count,created_at\r\n"
        )

    def test_one_po_one_data_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        db_session.add(po)
        db_session.commit()
        db_session.refresh(po)

        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        assert len(lines) == 3  # header + 1 data + trailing empty
        cells = lines[1].split(",")
        assert cells[0] == str(po.id)
        assert cells[1] == "ACME"
        assert cells[2] == "no"
        assert cells[3] == "draft"
        assert cells[4] == "0"
        # cells[5] is the created_at ISO timestamp.

    def test_archived_supplier_renders_yes(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME", archived=True)
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        db_session.add(po)
        db_session.commit()

        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200
        # The supplier_archived cell carries the literal string "yes".
        # Two-cell match avoids accidental hit on a substring "yes" elsewhere.
        assert ",ACME,yes,draft," in resp.text

    def test_status_filter_applies_to_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        draft = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        sent = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add_all([draft, sent])
        db_session.commit()
        db_session.refresh(draft)
        db_session.refresh(sent)

        _login_as(client, u)
        resp = client.get(
            "/admin/purchase-orders?format=csv&status_filter=draft"
        )
        assert resp.status_code == 200
        # The draft PO appears; the sent PO does not.
        body = resp.text
        # Match each PO id at the start of a CSV row.
        assert f"\r\n{draft.id}," in body
        assert f"\r\n{sent.id}," not in body

    def test_newest_first_ordering_in_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        po1 = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        db_session.add(po1)
        db_session.commit()
        po2 = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.SENT, created_by=u.id
        )
        db_session.add(po2)
        db_session.commit()

        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200
        body = resp.text
        po2_pos = body.index(f"\r\n{po2.id},")
        po1_pos = body.index(f"\r\n{po1.id},")
        assert po2_pos < po1_pos


class TestPOListCsvHtmlBranch:
    def test_format_blank_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="po-list-heading"' in resp.text

    def test_format_unknown_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestPOListCsvReadOnly:
    def test_csv_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
        )
        db_session.add(po)
        db_session.commit()
        before = _audit_count(db_session)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?format=csv")
        assert resp.status_code == 200
        after = _audit_count(db_session)
        assert after == before


class TestPOListCsvLink:
    def test_html_renders_csv_link_with_active_status_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/purchase-orders?status_filter=draft")
        assert resp.status_code == 200
        assert 'data-testid="po-list-csv-link"' in resp.text
        assert "format=csv" in resp.text
        assert "status_filter=draft" in resp.text


def _audit_count(db: Session) -> int:
    return len(list(db.execute(select(AuditLog)).scalars().all()))
