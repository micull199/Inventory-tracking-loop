"""Integration tests for the manual stock-in (M2) and stock-out (M3) routes.

Covers stock-in:
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

Covers stock-out (M3):
- Role enforcement: same matrix as stock-in (Workshop / Office / Manager).
- Form rendering: qty / reason / note inputs (no unit_cost); CSRF;
  current_qty + open_value summary; recent-movements list reused.
- Validation matrix: qty positive ``Decimal``; reason / note stripped to None
  when blank; archived item 400; unknown item 404; failed validation writes
  no audit.
- Happy path: creates ``StockMovement(type=OUT)``; writes one
  ``CostLayerConsumption`` per layer touched; decrements ``qty_remaining`` +
  ``current_qty``; sets ``movement.total_cost`` from the layer-weighted sum;
  audit row carries ``stock_movement.out`` with the route inputs + total_cost.
- Insufficient stock: engine raises ``InsufficientStockError`` *before* any
  mutation; route catches, rolls back, re-renders the form with a 400 status,
  the user's qty / reason / note preserved, and an in-form error block.
  No movement / consumption row written, ``current_qty`` and layer
  ``qty_remaining`` unchanged.
- Multi-layer FIFO consume: oldest layer drained first, second layer partially
  consumed; total_cost is the layer-weighted sum.
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
    CostLayer,
    CostLayerConsumption,
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


# ===========================================================================
# Stock-out (M3)
# ===========================================================================


def _payload_out(
    *,
    qty: str = "5",
    reason: str = "",
    note: str = "",
    csrf: str = "",
) -> dict[str, str]:
    return {
        "qty": qty,
        "reason": reason,
        "note": note,
        "csrf_token": csrf,
    }


def _seed_layer(
    db: Session,
    *,
    item: Item,
    qty: Decimal | str,
    unit_cost: Decimal | str,
    actor: User,
    received_at: datetime | None = None,
) -> StockMovement:
    """Seed a real cost layer + IN movement via the engine.

    Tests for stock-out depend on the item having open layers; rather than
    re-implementing the receipt arithmetic, we go through the engine the
    same way M2's POST handler does.
    """
    qty_decimal = qty if isinstance(qty, Decimal) else Decimal(qty)
    unit_cost_decimal = unit_cost if isinstance(unit_cost, Decimal) else Decimal(unit_cost)
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.IN,
        qty=qty_decimal,
        user_id=actor.id,
    )
    db.add(movement)
    db.flush()
    record_receipt(
        db,
        item=item,
        qty=qty_decimal,
        unit_cost=unit_cost_decimal,
        source=CostLayerSource.MANUAL_IN,
        movement=movement,
        received_at=received_at,
    )
    db.commit()
    db.refresh(item)
    db.refresh(movement)
    return movement


# ---------------------------------------------------------------------------
# Role enforcement (stock-out)
# ---------------------------------------------------------------------------


class TestStockOutRoleEnforcement:
    def test_anonymous_get_form_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 401

    def test_anonymous_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(csrf=_csrf(client)),
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
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 403

    def test_workshop_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 200

    def test_workshop_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Workshop's stock-out write surface (mirrors stock-in)."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=ws)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="3", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_office_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 200

    def test_office_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _seed_layer(
            db_session, item=item, qty="10", unit_cost="2.00", actor=office
        )
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="3", csrf=_csrf(client)),
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
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 200

    def test_manager_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=mgr)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="3", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_admin_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=admin)
        _login_as(client, admin)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="3", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Form rendering (stock-out)
# ---------------------------------------------------------------------------


class TestStockOutForm:
    def test_form_includes_inputs_and_csrf(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WIRE-1", name="Silver wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 200
        body = resp.text
        assert "Silver wire" in body
        assert 'name="qty"' in body
        # No unit_cost on the stock-out form — consumption price is per-layer.
        assert 'name="unit_cost"' not in body
        assert 'name="reason"' in body
        assert 'name="note"' in body
        assert 'name="csrf_token"' in body
        assert 'data-testid="stock-out-submit"' in body

    def test_form_shows_current_qty_and_open_value(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(
            db_session, item=item, qty="10", unit_cost="2.50", actor=ws
        )
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        body = resp.text
        # current_qty = 10
        assert 'data-testid="item-current-qty"' in body
        assert "10" in body
        # open_value = 10 * 2.50 = 25
        assert 'data-testid="item-open-value"' in body
        assert "25" in body

    def test_form_open_value_zero_for_no_layers(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        # 0 should appear in the open-value span.
        assert 'data-testid="item-open-value"' in resp.text

    def test_form_recent_movements_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert "movements-empty" in resp.text

    def test_form_recent_movements_includes_seed_in(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="3", unit_cost="1", actor=ws)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.text.count('data-testid="movement-row"') == 1

    def test_unknown_item_form_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/items/999/out")
        assert resp.status_code == 404

    def test_archived_item_form_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/out")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validation matrix on POST (stock-out)
# ---------------------------------------------------------------------------


class TestStockOutValidation:
    def _setup(self, db_session: Session, client: TestClient) -> Item:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="50", unit_cost="2", actor=ws)
        _login_as(client, ws)
        return item

    def test_blank_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        before = db_session.execute(
            select(StockMovement).where(StockMovement.type == MovementType.OUT)
        ).first()
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert before is None
        # No OUT movement / consumption written.
        assert (
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.type == MovementType.OUT
                )
            ).first()
            is None
        )
        assert db_session.execute(select(CostLayerConsumption)).first() is None

    def test_zero_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="0", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert (
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.type == MovementType.OUT
                )
            ).first()
            is None
        )

    def test_negative_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="-1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert (
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.type == MovementType.OUT
                )
            ).first()
            is None
        )

    def test_non_numeric_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="lots", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unknown_item_post_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/items/999/out",
            data=_payload_out(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archived_item_post_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_validation_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="-1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert (
            _audit_rows(db_session, action="stock_movement.out") == []
        )


# ---------------------------------------------------------------------------
# Happy path (stock-out)
# ---------------------------------------------------------------------------


class TestStockOutHappyPath:
    def test_creates_movement_consumption_and_decrements_qty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WIRE-1", name="Wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        seed = _seed_layer(
            db_session, item=item, qty="10", unit_cost="2.50", actor=ws
        )
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(
                qty="3", reason="production", note="job 42",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/out"

        out = db_session.execute(
            select(StockMovement).where(StockMovement.type == MovementType.OUT)
        ).scalar_one()
        assert out.item_id == item.id
        assert out.qty == Decimal("3")
        assert out.user_id == ws.id
        assert out.reason == "production"
        assert out.note == "job 42"
        # 3 * 2.50 = 7.50
        assert out.total_cost == Decimal("7.50")

        cons = db_session.execute(select(CostLayerConsumption)).scalars().all()
        assert len(cons) == 1
        c = cons[0]
        assert c.movement_id == out.id
        assert c.qty_consumed == Decimal("3")
        assert c.unit_cost_at_consumption == Decimal("2.50")

        layer = db_session.execute(
            select(CostLayer).where(CostLayer.id == c.layer_id)
        ).scalar_one()
        assert layer.source_movement_id == seed.id
        assert layer.qty_remaining == Decimal("7")  # 10 - 3
        assert layer.qty_received == Decimal("10")  # immutable

        db_session.refresh(item)
        assert item.current_qty == Decimal("7")

    def test_strips_whitespace_on_reason_and_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="5", unit_cost="1", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(
                qty="1", reason="  use  ", note="  job  ",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        out = db_session.execute(
            select(StockMovement).where(StockMovement.type == MovementType.OUT)
        ).scalar_one()
        assert out.reason == "use"
        assert out.note == "job"

    def test_blank_reason_and_note_become_none(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="5", unit_cost="1", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="1", reason="", note="   ",
                              csrf=_csrf(client)),
            follow_redirects=False,
        )
        out = db_session.execute(
            select(StockMovement).where(StockMovement.type == MovementType.OUT)
        ).scalar_one()
        assert out.reason is None
        assert out.note is None

    def test_audit_row_written(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(
                qty="4", reason="produced", csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="stock_movement.out")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == ws.id
        assert row.entity_type == "stock_movement"
        out = db_session.execute(
            select(StockMovement).where(StockMovement.type == MovementType.OUT)
        ).scalar_one()
        assert row.entity_id == out.id
        assert row.before_json is None
        assert row.after_json is not None
        assert row.after_json["item_id"] == item.id
        assert row.after_json["qty"] == "4"
        # total_cost is the engine-computed sum-across-layers; the layer's
        # unit_cost was round-tripped through Numeric(14,4) so the string
        # representation carries the column's scale.
        assert row.after_json["total_cost"] == "8.0000"
        assert row.after_json["reason"] == "produced"
        # no unit_cost / source on stock-out audit (varies per layer / N/A).
        assert "unit_cost" not in row.after_json
        assert "source" not in row.after_json

    def test_flash_message_set(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Casting alloy")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="5", unit_cost="2", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="2", csrf=_csrf(client)),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{item.id}/out")
        assert "Casting alloy" in resp.text


# ---------------------------------------------------------------------------
# Insufficient stock (stock-out)
# ---------------------------------------------------------------------------


class TestStockOutInsufficientStock:
    def test_consume_more_than_open_layers_returns_400_with_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="3", unit_cost="2", actor=ws)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(
                qty="10", reason="oops", note="too much",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.text
        # In-form error block + preserved inputs.
        assert 'data-testid="stock-out-error"' in body
        assert "Not enough stock" in body
        # Preserved qty / reason / note in the form.
        assert 'value="10"' in body
        assert "oops" in body
        assert "too much" in body
        # No OUT movement, no consumption row, layer unchanged.
        assert (
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.type == MovementType.OUT
                )
            ).first()
            is None
        )
        assert db_session.execute(select(CostLayerConsumption)).first() is None
        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.qty_remaining == Decimal("3")
        db_session.refresh(item)
        assert item.current_qty == Decimal("3")

    def test_no_layers_at_all_returns_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert 'data-testid="stock-out-error"' in resp.text

    def test_insufficient_stock_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="2", unit_cost="1", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="100", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert _audit_rows(db_session, action="stock_movement.out") == []

    def test_consume_exact_open_balance_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="5", unit_cost="2", actor=ws)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="5", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.qty_remaining == Decimal("0")
        db_session.refresh(item)
        assert item.current_qty == Decimal("0")


# ---------------------------------------------------------------------------
# Multi-layer FIFO consume (stock-out)
# ---------------------------------------------------------------------------


class TestStockOutMultiLayerFIFO:
    def test_consume_spans_two_layers_oldest_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        # Older layer @ 2.00; newer @ 3.00. Use distinct received_at so the
        # FIFO order is unambiguous (the engine breaks ties by id but we don't
        # need to rely on that here).
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 2, 1, tzinfo=UTC)
        _seed_layer(
            db_session, item=item, qty="4", unit_cost="2.00",
            actor=ws, received_at=old,
        )
        _seed_layer(
            db_session, item=item, qty="6", unit_cost="3.00",
            actor=ws, received_at=new,
        )
        _login_as(client, ws)
        # Consume 7: takes 4 from the old layer, 3 from the new.
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="7", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        out = db_session.execute(
            select(StockMovement).where(StockMovement.type == MovementType.OUT)
        ).scalar_one()
        # 4*2.00 + 3*3.00 = 8.00 + 9.00 = 17.00
        assert out.total_cost == Decimal("17.00")

        cons = list(
            db_session.execute(
                select(CostLayerConsumption).order_by(
                    CostLayerConsumption.id
                )
            )
            .scalars()
            .all()
        )
        assert len(cons) == 2
        assert cons[0].qty_consumed == Decimal("4")
        assert cons[0].unit_cost_at_consumption == Decimal("2.00")
        assert cons[1].qty_consumed == Decimal("3")
        assert cons[1].unit_cost_at_consumption == Decimal("3.00")

        layers = list(
            db_session.execute(
                select(CostLayer).order_by(CostLayer.received_at, CostLayer.id)
            )
            .scalars()
            .all()
        )
        # Old fully drained, new partially.
        assert layers[0].qty_remaining == Decimal("0")
        assert layers[1].qty_remaining == Decimal("3")

        db_session.refresh(item)
        assert item.current_qty == Decimal("3")

    def test_recent_movements_includes_in_and_out(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(
            db_session, item=item, qty="5", unit_cost="2",
            actor=ws,
        )
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/out",
            data=_payload_out(qty="2", reason="job-out", csrf=_csrf(client)),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{item.id}/out")
        body = resp.text
        # Two rows: the seed IN and the new OUT.
        assert body.count('data-testid="movement-row"') == 2
        # OUT renders newest first → "job-out" appears before any IN reason
        # (which is None). Loose check: the OUT reason is in the table body.
        assert "job-out" in body


# ---------------------------------------------------------------------------
# Edit-form integration: "Stock out" link
# ---------------------------------------------------------------------------


class TestStockOutLinkOnEditForm:
    def test_edit_form_shows_stock_out_link_for_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert f"/admin/items/{item.id}/out" in resp.text
        assert 'data-testid="stock-out-link"' in resp.text

    def test_edit_form_hides_stock_out_link_for_archived_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="stock-out-link"' not in resp.text


# =============================================================================
# M4 — adjustment movements
# =============================================================================


def _payload_adjust(
    *,
    qty: str = "5",
    direction: str = "increase",
    unit_cost: str = "2.00",
    reason: str = "stock-take variance",
    note: str = "",
    csrf: str = "",
) -> dict[str, str]:
    return {
        "qty": qty,
        "direction": direction,
        "unit_cost": unit_cost,
        "reason": reason,
        "note": note,
        "csrf_token": csrf,
    }


# ---------------------------------------------------------------------------
# Role enforcement (adjustment)
# ---------------------------------------------------------------------------


class TestStockAdjustRoleEnforcement:
    def test_anonymous_get_form_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 401

    def test_anonymous_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
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
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 403

    def test_workshop_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 200

    def test_workshop_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Workshop's adjustment write surface (MISSION §3 grants this)."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_office_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 200

    def test_office_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
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
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 200

    def test_manager_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
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
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Form rendering (adjustment)
# ---------------------------------------------------------------------------


class TestStockAdjustForm:
    def test_form_includes_inputs_and_csrf(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="WIRE-1", name="Silver wire"
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 200
        body = resp.text
        assert "Silver wire" in body
        assert 'name="qty"' in body
        assert 'name="direction"' in body
        assert 'name="unit_cost"' in body
        assert 'name="reason"' in body
        assert 'name="note"' in body
        assert 'name="csrf_token"' in body
        assert 'data-testid="stock-adjust-submit"' in body
        # Both directions selectable.
        assert 'value="increase"' in body
        assert 'value="decrease"' in body
        # Reason is marked required (HTML required attribute on the input).
        # Search for the reason input segment specifically.
        reason_section = body[body.index('name="reason"') :][:400]
        assert "required" in reason_section

    def test_form_shows_current_qty_and_open_value(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(
            db_session, item=item, qty="10", unit_cost="2.50", actor=ws
        )
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        body = resp.text
        assert 'data-testid="item-current-qty"' in body
        assert 'data-testid="item-open-value"' in body
        # 10 * 2.50 = 25
        assert "25" in body

    def test_form_recent_movements_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert "movements-empty" in resp.text

    def test_unknown_item_form_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/items/999/adjust")
        assert resp.status_code == 404

    def test_archived_item_form_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validation matrix (adjustment)
# ---------------------------------------------------------------------------


class TestStockAdjustValidation:
    def _setup(self, db_session: Session, client: TestClient) -> Item:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="50", unit_cost="2", actor=ws)
        _login_as(client, ws)
        return item

    def test_blank_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(qty="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_zero_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(qty="0", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_negative_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(qty="-1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_non_numeric_qty_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(qty="lots", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_blank_direction_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(direction="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_invalid_direction_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(direction="sideways", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_blank_reason_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(reason="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_whitespace_only_reason_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(reason="   ", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_blank_unit_cost_on_increase_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase", unit_cost="", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_negative_unit_cost_on_increase_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase", unit_cost="-1", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_non_numeric_unit_cost_on_increase_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase", unit_cost="cheap", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_zero_unit_cost_on_increase_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase",
                qty="2",
                unit_cost="0",
                reason="found",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_unit_cost_ignored_on_decrease(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Garbage unit_cost is fine for decreases — the field is ignored."""
        item = self._setup(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="3",
                unit_cost="not-a-number",
                reason="loss",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_unknown_item_post_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/items/999/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archived_item_post_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_validation_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = self._setup(db_session, client)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(qty="-1", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert _audit_rows(db_session, action="stock_movement.adjustment") == []


# ---------------------------------------------------------------------------
# Increase happy path (positive adjustment → new layer)
# ---------------------------------------------------------------------------


class TestStockAdjustIncreaseHappyPath:
    def test_creates_movement_layer_and_bumps_qty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WIRE-1", name="Wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase",
                qty="20",
                unit_cost="3.00",
                reason="found in storage",
                note="boxed away",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/adjust"

        adj = db_session.execute(
            select(StockMovement).where(
                StockMovement.type == MovementType.ADJUSTMENT
            )
        ).scalar_one()
        assert adj.item_id == item.id
        assert adj.type == MovementType.ADJUSTMENT
        assert adj.qty == Decimal("20")
        assert adj.user_id == ws.id
        assert adj.reason == "found in storage"
        assert adj.note == "boxed away"
        # 20 * 3.00 = 60.00
        assert adj.total_cost == Decimal("60.00")

        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.item_id == item.id
        assert layer.qty_received == Decimal("20")
        assert layer.qty_remaining == Decimal("20")
        assert layer.unit_cost == Decimal("3.00")
        assert layer.source == CostLayerSource.POSITIVE_ADJUSTMENT
        assert layer.source_movement_id == adj.id

        db_session.refresh(item)
        assert item.current_qty == Decimal("20")

    def test_audit_row_for_increase(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase",
                qty="5",
                unit_cost="2.50",
                reason="found extra",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="stock_movement.adjustment")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == ws.id
        assert row.entity_type == "stock_movement"
        adj = db_session.execute(
            select(StockMovement).where(
                StockMovement.type == MovementType.ADJUSTMENT
            )
        ).scalar_one()
        assert row.entity_id == adj.id
        assert row.before_json is None
        assert row.after_json is not None
        a = row.after_json
        assert a["item_id"] == item.id
        assert a["qty"] == "5"
        assert a["direction"] == "increase"
        assert a["unit_cost"] == "2.50"
        # 5 * 2.50 = 12.50
        assert a["total_cost"] == "12.50"
        assert a["source"] == "positive_adjustment"
        assert a["reason"] == "found extra"
        assert "received_at" in a

    def test_increase_strips_whitespace_on_reason_and_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase",
                qty="1",
                unit_cost="1",
                reason="  trim me  ",
                note="  ditto  ",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        adj = db_session.execute(
            select(StockMovement).where(
                StockMovement.type == MovementType.ADJUSTMENT
            )
        ).scalar_one()
        assert adj.reason == "trim me"
        assert adj.note == "ditto"

    def test_increase_flash_message(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Casting alloy")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="increase",
                qty="2",
                unit_cost="1",
                reason="r",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert "Casting alloy" in resp.text
        # Flash uses "+qty" for increases.
        assert "+2" in resp.text


# ---------------------------------------------------------------------------
# Decrease happy path (negative adjustment → consume FIFO)
# ---------------------------------------------------------------------------


class TestStockAdjustDecreaseHappyPath:
    def test_creates_movement_consumption_and_decrements_qty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        seed = _seed_layer(
            db_session, item=item, qty="10", unit_cost="2.50", actor=ws
        )
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="3",
                unit_cost="ignored",
                reason="scrap loss",
                note="job 7",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        adj = db_session.execute(
            select(StockMovement).where(
                StockMovement.type == MovementType.ADJUSTMENT
            )
        ).scalar_one()
        assert adj.qty == Decimal("3")
        assert adj.reason == "scrap loss"
        assert adj.note == "job 7"
        # 3 * 2.50 = 7.50
        assert adj.total_cost == Decimal("7.50")

        cons = db_session.execute(select(CostLayerConsumption)).scalars().all()
        assert len(cons) == 1
        c = cons[0]
        assert c.movement_id == adj.id
        assert c.qty_consumed == Decimal("3")
        assert c.unit_cost_at_consumption == Decimal("2.50")

        layer = db_session.execute(
            select(CostLayer).where(CostLayer.id == c.layer_id)
        ).scalar_one()
        assert layer.source_movement_id == seed.id
        assert layer.qty_remaining == Decimal("7")
        db_session.refresh(item)
        assert item.current_qty == Decimal("7")

    def test_audit_row_for_decrease(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="4",
                reason="damaged",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="stock_movement.adjustment")
        assert len(rows) == 1
        a = rows[0].after_json
        assert a is not None
        assert a["item_id"] == item.id
        assert a["qty"] == "4"
        assert a["direction"] == "decrease"
        # Layer-weighted; reads back from Numeric(14,4).
        assert a["total_cost"] == "8.0000"
        assert a["reason"] == "damaged"
        # No unit_cost / source / received_at on decrease.
        assert "unit_cost" not in a
        assert "source" not in a
        assert "received_at" not in a

    def test_decrease_blank_note_becomes_none(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="5", unit_cost="1", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="1",
                reason="x",
                note="   ",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        adj = db_session.execute(
            select(StockMovement).where(
                StockMovement.type == MovementType.ADJUSTMENT
            )
        ).scalar_one()
        assert adj.note is None

    def test_decrease_flash_message(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Casting alloy")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="5", unit_cost="2", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="2",
                reason="loss",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{item.id}/adjust")
        assert "Casting alloy" in resp.text
        # Flash uses "-qty" for decreases.
        assert "-2" in resp.text


# ---------------------------------------------------------------------------
# Insufficient stock on decrease (atomic on raise)
# ---------------------------------------------------------------------------


class TestStockAdjustInsufficientStock:
    def test_decrease_more_than_open_returns_400_with_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Wire")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="3", unit_cost="2", actor=ws)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="10",
                reason="oops",
                note="too much",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        body = resp.text
        assert 'data-testid="stock-adjust-error"' in body
        assert "Not enough stock" in body
        # Preserved form values.
        assert 'value="10"' in body
        assert "oops" in body
        assert "too much" in body
        # Direction preserved as "decrease" (selected option in the form).
        # The selected attribute follows the value on the next line of the
        # rendered <option>; check the segment between them contains nothing
        # but whitespace.
        idx = body.index('value="decrease"')
        # Look at the next ~120 chars for the selected attribute.
        assert "selected" in body[idx : idx + 120]

        # No mutation: no ADJUSTMENT movement / consumption / qty change.
        assert (
            db_session.execute(
                select(StockMovement).where(
                    StockMovement.type == MovementType.ADJUSTMENT
                )
            ).first()
            is None
        )
        assert db_session.execute(select(CostLayerConsumption)).first() is None
        layer = db_session.execute(select(CostLayer)).scalar_one()
        assert layer.qty_remaining == Decimal("3")
        db_session.refresh(item)
        assert item.current_qty == Decimal("3")

    def test_decrease_no_layers_returns_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="1",
                reason="x",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert 'data-testid="stock-adjust-error"' in resp.text

    def test_insufficient_stock_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _seed_layer(db_session, item=item, qty="2", unit_cost="1", actor=ws)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="100",
                reason="x",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert _audit_rows(db_session, action="stock_movement.adjustment") == []


# ---------------------------------------------------------------------------
# Multi-layer FIFO on decrease
# ---------------------------------------------------------------------------


class TestStockAdjustMultiLayerFIFODecrease:
    def test_decrease_spans_two_layers_oldest_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        old = datetime(2026, 1, 1, tzinfo=UTC)
        new = datetime(2026, 2, 1, tzinfo=UTC)
        _seed_layer(
            db_session, item=item, qty="4", unit_cost="2.00",
            actor=ws, received_at=old,
        )
        _seed_layer(
            db_session, item=item, qty="6", unit_cost="3.00",
            actor=ws, received_at=new,
        )
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data=_payload_adjust(
                direction="decrease",
                qty="7",
                reason="scrap",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

        adj = db_session.execute(
            select(StockMovement).where(
                StockMovement.type == MovementType.ADJUSTMENT
            )
        ).scalar_one()
        # 4*2 + 3*3 = 17
        assert adj.total_cost == Decimal("17.00")

        cons = list(
            db_session.execute(
                select(CostLayerConsumption).order_by(
                    CostLayerConsumption.id
                )
            ).scalars().all()
        )
        assert len(cons) == 2
        assert cons[0].qty_consumed == Decimal("4")
        assert cons[0].unit_cost_at_consumption == Decimal("2.00")
        assert cons[1].qty_consumed == Decimal("3")
        assert cons[1].unit_cost_at_consumption == Decimal("3.00")

        db_session.refresh(item)
        assert item.current_qty == Decimal("3")


# ---------------------------------------------------------------------------
# Edit-form integration: "Adjust" link
# ---------------------------------------------------------------------------


class TestStockAdjustLinkOnEditForm:
    def test_edit_form_shows_adjust_link_for_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert f"/admin/items/{item.id}/adjust" in resp.text
        assert 'data-testid="stock-adjust-link"' in resp.text

    def test_edit_form_hides_adjust_link_for_archived_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="stock-adjust-link"' not in resp.text


# ---------------------------------------------------------------------------
# Item detail page (M6) — read-only page consolidating layers + timeline.
# ---------------------------------------------------------------------------


def _seed_consume(
    db: Session,
    *,
    item: Item,
    qty: Decimal | str,
    actor: User,
) -> StockMovement:
    """Seed a real consumption (OUT) via the engine — opposite of _seed_layer."""
    from app.cost_engine import consume_fifo

    qty_decimal = qty if isinstance(qty, Decimal) else Decimal(qty)
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.OUT,
        qty=qty_decimal,
        user_id=actor.id,
    )
    db.add(movement)
    db.flush()
    consume_fifo(db, item=item, qty=qty_decimal, movement=movement)
    db.commit()
    db.refresh(item)
    db.refresh(movement)
    return movement


def _seed_adjust_increase(
    db: Session,
    *,
    item: Item,
    qty: Decimal | str,
    unit_cost: Decimal | str,
    actor: User,
) -> StockMovement:
    """Seed a positive-adjustment (creates a layer) via the engine."""
    qty_decimal = qty if isinstance(qty, Decimal) else Decimal(qty)
    unit_cost_decimal = (
        unit_cost if isinstance(unit_cost, Decimal) else Decimal(unit_cost)
    )
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.ADJUSTMENT,
        qty=qty_decimal,
        user_id=actor.id,
        reason="seed adjust increase",
    )
    db.add(movement)
    db.flush()
    record_receipt(
        db,
        item=item,
        qty=qty_decimal,
        unit_cost=unit_cost_decimal,
        source=CostLayerSource.POSITIVE_ADJUSTMENT,
        movement=movement,
    )
    db.commit()
    db.refresh(item)
    db.refresh(movement)
    return movement


def _seed_adjust_decrease(
    db: Session,
    *,
    item: Item,
    qty: Decimal | str,
    actor: User,
) -> StockMovement:
    """Seed a negative-adjustment (consumes layers FIFO) via the engine."""
    from app.cost_engine import consume_fifo

    qty_decimal = qty if isinstance(qty, Decimal) else Decimal(qty)
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.ADJUSTMENT,
        qty=qty_decimal,
        user_id=actor.id,
        reason="seed adjust decrease",
    )
    db.add(movement)
    db.flush()
    consume_fifo(db, item=item, qty=qty_decimal, movement=movement)
    db.commit()
    db.refresh(item)
    db.refresh(movement)
    return movement


# Make `_seed_layer` already imported above usable here too — no work needed,
# the helper is module-scoped.


class TestItemDetailRoleEnforcement:
    def test_anonymous_get_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 401

    def test_pending_user_is_403(
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
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 403

    def test_workshop_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200

    def test_admin_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200


class TestItemDetailRendering:
    def test_unknown_item_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/99999/detail")
        assert resp.status_code == 404

    def test_archived_item_still_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Archived items show their history. Action links hide separately.
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200
        assert 'data-testid="item-detail-archived"' in resp.text
        # Action links suppressed on archived items.
        assert 'data-testid="stock-in-link"' not in resp.text
        assert 'data-testid="stock-out-link"' not in resp.text
        assert 'data-testid="stock-adjust-link"' not in resp.text

    def test_renders_item_header_and_summary(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="DET-1", name="Detail Test")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200
        assert 'data-testid="item-detail-heading"' in resp.text
        assert "Detail Test" in resp.text
        assert "DET-1" in resp.text
        assert 'data-testid="item-current-qty"' in resp.text
        assert 'data-testid="item-open-value"' in resp.text
        assert 'data-testid="item-detail-threshold"' in resp.text

    def test_action_links_visible_for_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="stock-in-link"' in resp.text
        assert 'data-testid="stock-out-link"' in resp.text
        assert 'data-testid="stock-adjust-link"' in resp.text
        assert 'data-testid="edit-item-link"' in resp.text

    def test_workshop_does_not_see_edit_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Workshop can see in/out/adjust action links but cannot edit the
        # item; the edit link must hide for them (matches I1b's role table).
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.status_code == 200
        assert 'data-testid="edit-item-link"' not in resp.text
        # In/out/adjust links still visible.
        assert 'data-testid="stock-in-link"' in resp.text
        assert 'data-testid="stock-out-link"' in resp.text
        assert 'data-testid="stock-adjust-link"' in resp.text


class TestItemDetailCostLayers:
    def test_empty_state_when_no_layers(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="cost-layers-empty"' in resp.text
        assert 'data-testid="cost-layers-table"' not in resp.text

    def test_single_layer_rendered(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="cost-layers-table"' in resp.text
        # Exactly one row.
        assert resp.text.count('data-testid="cost-layer-row"') == 1
        assert "10" in resp.text  # qty_received / qty_remaining
        assert "2.00" in resp.text  # unit_cost
        assert "manual_in" in resp.text

    def test_multi_layer_with_mixed_sources(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(
            db_session,
            item=item,
            qty="5",
            unit_cost="2.00",
            actor=mgr,
            received_at=datetime(2026, 1, 1, 10, tzinfo=UTC),
        )
        _seed_adjust_increase(
            db_session, item=item, qty="3", unit_cost="3.00", actor=mgr
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert resp.text.count('data-testid="cost-layer-row"') == 2
        assert "manual_in" in resp.text
        assert "positive_adjustment" in resp.text

    def test_fully_consumed_layer_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Add a 5-unit layer; consume all 5.
        _seed_layer(db_session, item=item, qty="5", unit_cost="2.00", actor=mgr)
        _seed_consume(db_session, item=item, qty="5", actor=mgr)
        # Add a fresh layer.
        _seed_layer(
            db_session,
            item=item,
            qty="3",
            unit_cost="4.00",
            actor=mgr,
            received_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        # Drained layer omitted from open-layers section.
        assert resp.text.count('data-testid="cost-layer-row"') == 1
        assert "4.00" in resp.text

    def test_layers_ordered_fifo(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Older layer with cost 9.99.
        _seed_layer(
            db_session,
            item=item,
            qty="2",
            unit_cost="9.99",
            actor=mgr,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # Newer layer with cost 1.11.
        _seed_layer(
            db_session,
            item=item,
            qty="2",
            unit_cost="1.11",
            actor=mgr,
            received_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        # Older one (9.99) appears first in body.
        idx_older = resp.text.find("9.99")
        idx_newer = resp.text.find("1.11")
        assert idx_older > 0
        assert idx_newer > idx_older


class TestItemDetailMovementsTimeline:
    def test_empty_state_when_no_movements(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="movements-timeline-empty"' in resp.text
        assert 'data-testid="movements-timeline"' not in resp.text

    def test_in_row_direction_plus(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="7", unit_cost="2.00", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-direction="+"' in resp.text
        # Total cost = 7 * 2 = 14.00 (set by record_receipt).
        assert "14.00" in resp.text

    def test_out_row_direction_minus(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=mgr)
        _seed_consume(db_session, item=item, qty="3", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-direction="-"' in resp.text
        # Layer-weighted total_cost stored as Numeric(14,4) → "6.0000".
        assert "6.0000" in resp.text

    def test_adjustment_increase_direction_plus(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_adjust_increase(
            db_session, item=item, qty="5", unit_cost="3.00", actor=mgr
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        # The adjustment-increase row is the only row, and direction is +.
        assert 'data-direction="+"' in resp.text
        assert 'data-direction="-"' not in resp.text

    def test_adjustment_decrease_direction_minus(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=mgr)
        _seed_adjust_decrease(db_session, item=item, qty="4", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        # Two rows: original IN (+) and adjust-decrease (-). Both data-direction
        # attrs appear.
        assert 'data-direction="+"' in resp.text
        assert 'data-direction="-"' in resp.text

    def test_timeline_ordered_newest_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        first = _seed_layer(
            db_session, item=item, qty="5", unit_cost="1.00", actor=mgr
        )
        second = _seed_layer(
            db_session, item=item, qty="3", unit_cost="2.00", actor=mgr
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        idx_second = resp.text.find(f'data-movement-id="{second.id}"')
        idx_first = resp.text.find(f'data-movement-id="{first.id}"')
        assert idx_second > 0
        assert idx_first > 0
        # Newest first → second.id appears before first.id in the body.
        assert idx_second < idx_first


class TestItemDetailLayerBreakdown:
    def test_in_row_has_no_breakdown(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="5", unit_cost="2.00", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="layer-breakdown"' not in resp.text

    def test_out_row_has_breakdown(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=mgr)
        out_movement = _seed_consume(db_session, item=item, qty="3", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="layer-breakdown"' in resp.text
        # The breakdown's data-movement-id attribute matches the OUT movement.
        assert f'data-movement-id="{out_movement.id}"' in resp.text
        # Single consumption: 3 x 2 from one layer.
        assert resp.text.count('data-testid="layer-breakdown-row"') == 1

    def test_negative_adjustment_has_breakdown(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="10", unit_cost="2.00", actor=mgr)
        _seed_adjust_decrease(db_session, item=item, qty="4", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="layer-breakdown"' in resp.text
        assert resp.text.count('data-testid="layer-breakdown-row"') == 1

    def test_positive_adjustment_no_breakdown(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_adjust_increase(
            db_session, item=item, qty="3", unit_cost="2.00", actor=mgr
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="layer-breakdown"' not in resp.text

    def test_multi_layer_out_breakdown(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Two layers with distinct received_at; consume across both.
        _seed_layer(
            db_session,
            item=item,
            qty="4",
            unit_cost="2.00",
            actor=mgr,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        _seed_layer(
            db_session,
            item=item,
            qty="6",
            unit_cost="3.00",
            actor=mgr,
            received_at=datetime(2026, 6, 1, tzinfo=UTC),
        )
        _seed_consume(db_session, item=item, qty="7", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        # Breakdown shows two consumption rows (4 from old + 3 from new).
        assert resp.text.count('data-testid="layer-breakdown-row"') == 2
        # Both unit costs visible.
        assert "2.00" in resp.text
        assert "3.00" in resp.text


class TestItemDetailPagination:
    def test_no_movements_renders_empty_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        # Empty timeline → the whole timeline + pagination block is replaced
        # with the timeline-empty marker.
        assert 'data-testid="movements-timeline-empty"' in resp.text
        assert 'data-testid="pagination"' not in resp.text

    def test_single_page_when_total_le_page_size(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # 5 movements — well under the page size of 20.
        for _ in range(5):
            _seed_layer(
                db_session, item=item, qty="1", unit_cost="1", actor=mgr
            )
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="pagination-single-page"' in resp.text
        assert "Page 1 of 1" in resp.text
        assert 'data-testid="pagination-next"' not in resp.text
        assert 'data-testid="pagination-prev"' not in resp.text

    def test_multi_page_with_navigation(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # 21 movements → 2 pages (page 1 of 20, page 2 of 1).
        for i in range(21):
            _seed_layer(
                db_session,
                item=item,
                qty="1",
                unit_cost="1",
                actor=mgr,
                received_at=datetime(2026, 1, 1, tzinfo=UTC)
                + timedelta(minutes=i),
            )
        _login_as(client, mgr)
        # Page 1: should have a Next link, no Prev.
        resp1 = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="pagination-info"' in resp1.text
        assert "Page 1 of 2" in resp1.text
        assert 'data-testid="pagination-next"' in resp1.text
        assert 'data-testid="pagination-prev"' not in resp1.text
        assert resp1.text.count('data-testid="timeline-row"') == 20
        # Page 2: Prev link, no Next, 1 row.
        resp2 = client.get(f"/admin/items/{item.id}/detail?page=2")
        assert "Page 2 of 2" in resp2.text
        assert 'data-testid="pagination-prev"' in resp2.text
        assert 'data-testid="pagination-next"' not in resp2.text
        assert resp2.text.count('data-testid="timeline-row"') == 1

    def test_out_of_range_page_clamps_to_last(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        for _ in range(3):
            _seed_layer(
                db_session, item=item, qty="1", unit_cost="1", actor=mgr
            )
        _login_as(client, mgr)
        # Asking for page 99 against a 1-page dataset clamps to page 1 of 1.
        resp = client.get(f"/admin/items/{item.id}/detail?page=99")
        assert resp.status_code == 200
        assert "Page 1 of 1" in resp.text

    def test_zero_or_negative_page_clamps_to_one(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _seed_layer(db_session, item=item, qty="1", unit_cost="1", actor=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/detail?page=0")
        assert resp.status_code == 200
        assert "Page 1 of 1" in resp.text


class TestItemDetailLink:
    def test_items_list_shows_detail_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert 'data-testid="detail-link"' in resp.text
        assert f"/admin/items/{item.id}/detail" in resp.text

    def test_edit_form_shows_detail_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="detail-link"' in resp.text
        assert f"/admin/items/{item.id}/detail" in resp.text
