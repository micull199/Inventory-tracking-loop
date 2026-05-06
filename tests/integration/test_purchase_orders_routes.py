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

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cost_engine import record_receipt
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
    db: Session, name: str = "ACME", *, archived: bool = False
) -> Supplier:
    s = Supplier(
        name=name,
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
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
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
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        sup = _make_supplier(db_session, name="ACME")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="N-1", supplier=sup)
        po = PurchaseOrder(
            supplier_id=sup.id, status=POStatus.DRAFT, created_by=u.id
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
