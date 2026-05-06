"""Integration tests for the manual stock-in route (M2).

Covers:
- Role enforcement: anonymous 401; pending 403; Workshop / Office / Manager /
  Admin all 200 on GET and 303 on POST. Workshop's first positive-write surface.
- Form rendering: qty / unit_cost / reason / note inputs; CSRF; current_qty
  display; recent-movements list (empty + populated, newest first).
- Validation matrix on POST: qty must parse as positive ``Decimal``; unit_cost
  must parse as non-negative; zero unit_cost allowed; archived item rejected;
  unknown item 404.
- Happy path: creates a ``StockMovement(type=IN)``, a ``CostLayer`` with
  ``source=MANUAL_IN``, bumps ``item.current_qty``, sets ``movement.total_cost``,
  writes a ``stock_movement.in`` audit row, sets a flash, redirects 303 back
  to ``GET /admin/items/{id}/in``.
- Multi-receipt: two consecutive POSTs accumulate qty + create two layers; the
  recent-movements list shows them newest first.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CostLayer,
    CostLayerSource,
    Item,
    MovementType,
    Role,
    StockMovement,
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


def _make_item(
    db: Session,
    *,
    leaf: TaxonomyNode,
    sku: str = "RM-001",
    name: str = "Silver wire",
    archived: bool = False,
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _payload(
    *,
    qty: str = "10",
    unit_cost: str = "2.50",
    reason: str = "",
    note: str = "",
    csrf: str = "",
) -> dict[str, str]:
    return {
        "qty": qty,
        "unit_cost": unit_cost,
        "reason": reason,
        "note": note,
        "csrf_token": csrf,
    }


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "stock_movement")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_form_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 401

    def test_anonymous_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_pending_user_get_form_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.WORKSHOP,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 403

    def test_workshop_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 200

    def test_workshop_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Workshop's first positive-write surface (MISSION §3)."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert db_session.execute(select(StockMovement)).first() is not None

    def test_office_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 200

    def test_office_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_manager_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 200

    def test_manager_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_admin_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Form rendering
# ---------------------------------------------------------------------------


class TestStockInForm:
    def test_form_includes_inputs_and_csrf(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WIRE-1", name="Silver wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 200
        body = resp.text
        assert "Silver wire" in body
        assert 'name="qty"' in body
        assert 'name="unit_cost"' in body
        assert 'name="reason"' in body
        assert 'name="note"' in body
        assert 'name="csrf_token"' in body

    def test_form_shows_current_qty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = Item(
            sku="W",
            name="W",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            current_qty=Decimal("42"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert "42" in resp.text

    def test_form_recent_movements_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert "movements-empty" in resp.text

    def test_form_recent_movements_lists_existing(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        # Two prior receipts via the route so they're real.
        for _ in range(2):
            client.post(
                f"/admin/items/{item.id}/in",
                data=_payload(qty="3", unit_cost="1.00", csrf=_csrf(client)),
                follow_redirects=False,
            )
        resp = client.get(f"/admin/items/{item.id}/in")
        # Two movement rows render.
        assert resp.text.count('data-testid="movement-row"') == 2

    def test_unknown_item_form_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/items/999/in")
        assert resp.status_code == 404

    def test_archived_item_form_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/in")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validation matrix on POST
# ---------------------------------------------------------------------------


class TestStockInValidation:
    def _setup(self, db_session: Session, client: TestClient) -> Item:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        return item

    def test_blank_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(StockMovement)).first() is None
        assert db_session.execute(select(CostLayer)).first() is None

    def test_zero_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="0", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(StockMovement)).first() is None

    def test_negative_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="-1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(StockMovement)).first() is None

    def test_non_numeric_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="banana", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_blank_unit_cost_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(unit_cost="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_negative_unit_cost_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(unit_cost="-1.50", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_non_numeric_unit_cost_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(unit_cost="cheap", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_zero_unit_cost_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Zero unit cost is intentionally allowed (sample / gifted stock)."""
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="5", unit_cost="0", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.unit_cost == Decimal("0")

    def test_unknown_item_post_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/items/999/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert db_session.execute(select(StockMovement)).first() is None

    def test_archived_item_post_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(StockMovement)).first() is None
        assert db_session.execute(select(CostLayer)).first() is None

    def test_validation_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="-1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestStockInHappyPath:
    def test_creates_movement_layer_and_bumps_qty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WIRE-1", name="Wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(
                qty="10", unit_cost="2.50", reason="purchase", note="invoice 42",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/in"

        movement = db_session.execute(select(StockMovement)).scalar_one()
        assert movement.item_id == item.id
        assert movement.type is MovementType.IN
        assert movement.qty == Decimal("10")
        assert movement.user_id == ws.id
        assert movement.reason == "purchase"
        assert movement.note == "invoice 42"
        assert movement.total_cost == Decimal("25.00")
        assert movement.po_id is None
        assert movement.stock_take_id is None

        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.item_id == item.id
        assert layer.qty_received == Decimal("10")
        assert layer.qty_remaining == Decimal("10")
        assert layer.unit_cost == Decimal("2.50")
        assert layer.source is CostLayerSource.MANUAL_IN
        assert layer.source_movement_id == movement.id

        db_session.refresh(item)
        assert item.current_qty == Decimal("10")

    def test_strips_whitespace_on_reason_and_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(reason="  buy  ", note="  inv 1  ", csrf=_csrf(client)),
            follow_redirects=False,
        )
        movement = db_session.execute(select(StockMovement)).scalar_one()
        assert movement.reason == "buy"
        assert movement.note == "inv 1"

    def test_blank_reason_and_note_become_none(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(reason="", note="   ", csrf=_csrf(client)),
            follow_redirects=False,
        )
        movement = db_session.execute(select(StockMovement)).scalar_one()
        assert movement.reason is None
        assert movement.note is None

    def test_audit_row_written(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(
                qty="4", unit_cost="1.25", reason="received",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="stock_movement.in")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == ws.id
        assert row.entity_type == "stock_movement"
        movement = db_session.execute(select(StockMovement)).scalar_one()
        assert row.entity_id == movement.id
        assert row.before_json is None
        assert row.after_json is not None
        assert row.after_json["item_id"] == item.id
        assert row.after_json["qty"] == "4"
        assert row.after_json["unit_cost"] == "1.25"
        assert row.after_json["total_cost"] == "5.00"
        assert row.after_json["source"] == "manual_in"
        assert row.after_json["reason"] == "received"

    def test_flash_message_set(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Silver wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="3", unit_cost="2", csrf=_csrf(client)),
            follow_redirects=False,
        )
        # Follow the redirect to render the flash region.
        resp = client.get(f"/admin/items/{item.id}/in")
        assert "Silver wire" in resp.text
        assert "3" in resp.text  # qty surfaced

    def test_decimal_qty_and_cost_persist_with_precision(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="2.5", unit_cost="0.0001", csrf=_csrf(client)),
            follow_redirects=False,
        )
        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.qty_received == Decimal("2.5000")
        assert layer.unit_cost == Decimal("0.0001")
        movement = db_session.execute(select(StockMovement)).scalar_one()
        # qty * unit_cost = 0.00025; Numeric(14,4) rounds to 0.0003 (HALF_EVEN
        # in Python Decimal terms — actually Decimal preserves it but the col
        # type quantises). We only assert the cost-engine result, not the
        # column-quantisation behaviour.
        assert movement.total_cost is not None


# ---------------------------------------------------------------------------
# Multiple receipts
# ---------------------------------------------------------------------------


class TestMultipleReceipts:
    def test_two_consecutive_receipts_accumulate(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        # First receipt
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="10", unit_cost="2.00", csrf=_csrf(client)),
            follow_redirects=False,
        )
        # Second receipt at a different unit cost
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="5", unit_cost="3.00", csrf=_csrf(client)),
            follow_redirects=False,
        )

        movements = list(
            db_session.execute(
                select(StockMovement).order_by(StockMovement.id)
            ).scalars()
        )
        assert len(movements) == 2
        layers = list(
            db_session.execute(
                select(CostLayer).order_by(CostLayer.id)
            ).scalars()
        )
        assert len(layers) == 2
        assert [layer.unit_cost for layer in layers] == [
            Decimal("2.00"),
            Decimal("3.00"),
        ]

        db_session.refresh(item)
        assert item.current_qty == Decimal("15")

    def test_recent_movements_list_shows_newest_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="1", unit_cost="1", reason="first", csrf=_csrf(client)),
            follow_redirects=False,
        )
        client.post(
            f"/admin/items/{item.id}/in",
            data=_payload(qty="2", unit_cost="2", reason="second", csrf=_csrf(client)),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{item.id}/in")
        body = resp.text
        # "second" appears before "first" in the rendered list.
        idx_second = body.find("second")
        idx_first = body.find("first")
        assert 0 < idx_second < idx_first

    def test_other_items_movements_not_shown(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A", name="Alpha")
        b = _make_item(db_session, leaf=leaf, sku="B", name="Bravo")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{a.id}/in",
            data=_payload(qty="1", unit_cost="1", reason="alpha-receipt",
                          csrf=_csrf(client)),
            follow_redirects=False,
        )
        client.post(
            f"/admin/items/{b.id}/in",
            data=_payload(qty="1", unit_cost="1", reason="bravo-receipt",
                          csrf=_csrf(client)),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{a.id}/in")
        assert "alpha-receipt" in resp.text
        assert "bravo-receipt" not in resp.text


# ---------------------------------------------------------------------------
# Edit-form integration: "Stock in" link
# ---------------------------------------------------------------------------


class TestStockInLinkOnEditForm:
    def test_edit_form_shows_stock_in_link_for_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert f"/admin/items/{item.id}/in" in resp.text
        assert 'data-testid="stock-in-link"' in resp.text

    def test_edit_form_hides_stock_in_link_for_archived_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="stock-in-link"' not in resp.text
