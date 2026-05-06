"""Integration tests for the check-out flow (C2).

Adds the first write surface for the ``checkouts`` table introduced in C1.
Two routes mounted at ``/admin/items``:

- ``GET /admin/items/{item_id}/checkout`` — renders a form. For unique-tracked
  items the form includes a ``<select>`` of available (active + status=available
  + not-currently-out) units; for qty-tracked items there's no unit select.
  Renders a status block when an open checkout already exists.
- ``POST /admin/items/{item_id}/checkout`` — validates, creates a ``Checkout``
  row, writes a ``checkout.created`` audit row, redirects 303 with a flash.

Validation guards:
- Item exists (404), not archived (400), ``requires_checkout=True`` (400).
- ``expected_return``: blank → None; ISO ``YYYY-MM-DD`` else 400.
- ``condition_note``: stripped, blank → None, ≤ 2000 chars else 400.
- Unique-tracked: ``item_unit_id`` required; on this item; status=available;
  not currently in an open checkout; archived rejects.
- Qty-tracked: ``item_unit_id`` silently ignored; at-most-one-open per item.

Checkouts do NOT touch the cost engine: no ``StockMovement`` is created and
``item.current_qty`` is unchanged. A checkout is custody, not consumption.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Checkout,
    Item,
    ItemUnit,
    ItemUnitStatus,
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


def _make_leaf(db: Session, name: str = "Tools") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_item(
    db: Session,
    *,
    leaf: TaxonomyNode,
    sku: str = "TOOL-1",
    name: str = "Pliers",
    tracking_mode: TrackingMode = TrackingMode.QTY,
    requires_checkout: bool = True,
    archived: bool = False,
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=tracking_mode,
        requires_checkout=requires_checkout,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_unit(
    db: Session,
    *,
    item: Item,
    serial: str = "U-1",
    status: ItemUnitStatus = ItemUnitStatus.AVAILABLE,
    archived: bool = False,
) -> ItemUnit:
    unit = ItemUnit(
        item_id=item.id,
        serial_or_label=serial,
        status=status,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


def _payload(
    *,
    item_unit_id: str = "",
    expected_return: str = "",
    condition_note: str = "",
    csrf: str = "",
) -> dict[str, str]:
    return {
        "item_unit_id": item_unit_id,
        "expected_return": expected_return,
        "condition_note": condition_note,
        "csrf_token": csrf,
    }


def _audit_rows(
    db: Session, *, action: str | None = None
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "checkout")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 401

    def test_anonymous_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_pending_get_is_403(
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
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 403

    def test_pending_post_is_403(
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
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_workshop_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 200

    def test_workshop_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Workshop self-checkout is the primary use case (MISSION §3)."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert db_session.execute(select(Checkout)).first() is not None

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 200

    def test_office_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 200

    def test_manager_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
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
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Form rendering
# ---------------------------------------------------------------------------


class TestCheckoutForm:
    def test_qty_tracked_form_no_unit_select(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, tracking_mode=TrackingMode.QTY
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 200
        body = resp.text
        # No unit input rendered for qty-tracked items.
        assert 'data-testid="checkout-unit-input"' not in body
        # CSRF + form + submit + expected_return + note inputs all present.
        assert 'name="csrf_token"' in body
        assert 'data-testid="checkout-form"' in body
        assert 'data-testid="checkout-submit"' in body
        assert 'data-testid="checkout-expected-return-input"' in body
        assert 'data-testid="checkout-note-input"' in body

    def test_unique_tracked_form_lists_available_units(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="U-TOOL",
        )
        u1 = _make_unit(db_session, item=item, serial="U-A")
        u2 = _make_unit(db_session, item=item, serial="U-B")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="checkout-unit-input"' in body
        # Both units' option values appear in the rendered select.
        assert f'value="{u1.id}"' in body
        assert f'value="{u2.id}"' in body
        assert "U-A" in body
        assert "U-B" in body

    def test_unique_tracked_form_excludes_archived_unit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="U-TOOL",
        )
        active = _make_unit(db_session, item=item, serial="ACTIVE-1")
        gone = _make_unit(
            db_session, item=item, serial="ARCH-1", archived=True
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        body = resp.text
        # Active unit is in the rendered select; archived isn't.
        assert f'value="{active.id}"' in body
        assert f'value="{gone.id}"' not in body
        assert "ACTIVE-1" in body

    def test_unique_tracked_form_excludes_lost_unit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="U-TOOL",
        )
        active = _make_unit(db_session, item=item, serial="OK-1")
        lost = _make_unit(
            db_session,
            item=item,
            serial="LOST-1",
            status=ItemUnitStatus.LOST,
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        body = resp.text
        assert f'value="{active.id}"' in body
        assert f'value="{lost.id}"' not in body

    def test_unique_tracked_form_excludes_currently_open_unit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="U-TOOL",
        )
        u1 = _make_unit(db_session, item=item, serial="U-A")
        u2 = _make_unit(db_session, item=item, serial="U-B")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        # Check out u1 first.
        resp1 = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(u1.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        # u1 should now be excluded; u2 still available.
        resp = client.get(f"/admin/items/{item.id}/checkout")
        body = resp.text
        assert f'value="{u2.id}"' in body
        assert f'value="{u1.id}"' not in body

    def test_form_status_block_when_open_checkout(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        # Create an open checkout.
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        # Re-render shows the status block + open row.
        resp = client.get(f"/admin/items/{item.id}/checkout")
        body = resp.text
        assert 'data-testid="checkout-status-block"' in body
        assert 'data-testid="checkout-open-row"' in body
        assert "w@x.test" in body  # actor email surfaced

    def test_form_no_status_block_when_no_open_checkouts(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert 'data-testid="checkout-status-block"' not in resp.text

    def test_unknown_item_form_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/items/999/checkout")
        assert resp.status_code == 404

    def test_archived_item_form_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 400

    def test_non_flagged_item_form_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, requires_checkout=False
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Validation matrix on POST
# ---------------------------------------------------------------------------


class TestCheckoutValidation:
    def _setup_qty(
        self, db_session: Session, client: TestClient
    ) -> tuple[Item, User]:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        return item, ws

    def _setup_unique(
        self, db_session: Session, client: TestClient
    ) -> tuple[Item, ItemUnit, User]:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, tracking_mode=TrackingMode.UNIQUE
        )
        unit = _make_unit(db_session, item=item, serial="U-1")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        return item, unit, ws

    def test_bad_expected_return_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _ = self._setup_qty(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(expected_return="not-a-date", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Checkout)).first() is None

    def test_blank_unit_id_for_unique_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _, _ = self._setup_unique(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Checkout)).first() is None

    def test_non_numeric_unit_id_for_unique_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _, _ = self._setup_unique(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id="banana", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unknown_unit_id_for_unique_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _, _ = self._setup_unique(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id="9999", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unit_on_different_item_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _, _ = self._setup_unique(db_session, client)
        # A second unique-tracked item with its own unit; submitting that
        # unit's id against the first item's URL must reject.
        leaf2 = _make_leaf(db_session, name="Other Tools")
        other = _make_item(
            db_session,
            leaf=leaf2,
            sku="OTHER-1",
            tracking_mode=TrackingMode.UNIQUE,
        )
        other_unit = _make_unit(db_session, item=other, serial="X-1")
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(
                item_unit_id=str(other_unit.id), csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Checkout)).first() is None

    def test_archived_unit_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, tracking_mode=TrackingMode.UNIQUE
        )
        unit = _make_unit(
            db_session, item=item, serial="GONE", archived=True
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(unit.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_lost_unit_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, tracking_mode=TrackingMode.UNIQUE
        )
        unit = _make_unit(
            db_session,
            item=item,
            serial="LOST-1",
            status=ItemUnitStatus.LOST,
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(unit.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_already_open_qty_tracked_item_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _ = self._setup_qty(db_session, client)
        # First checkout succeeds.
        resp1 = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        # Second checkout (still open) rejects.
        resp2 = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp2.status_code == 400
        # Still only one row.
        rows = list(db_session.execute(select(Checkout)).scalars().all())
        assert len(rows) == 1

    def test_already_open_unique_unit_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, unit, _ = self._setup_unique(db_session, client)
        resp1 = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(unit.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        resp2 = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(unit.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp2.status_code == 400
        rows = list(db_session.execute(select(Checkout)).scalars().all())
        assert len(rows) == 1

    def test_oversize_condition_note_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _ = self._setup_qty(db_session, client)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(condition_note="x" * 2001, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Checkout)).first() is None

    def test_archived_item_post_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, archived=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Checkout)).first() is None

    def test_non_flagged_item_post_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, requires_checkout=False
        )
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unknown_item_post_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            "/admin/items/999/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_failed_validation_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        item, _, _ = self._setup_unique(db_session, client)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Happy path — qty-tracked
# ---------------------------------------------------------------------------


class TestCheckoutQtyHappyPath:
    def test_creates_checkout_row_with_no_unit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Borrowable Hammer")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        before = datetime.now(UTC)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(
                expected_return="2026-06-15",
                condition_note="back by Friday",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        after = datetime.now(UTC)

        assert resp.status_code == 303
        assert (
            resp.headers["location"]
            == f"/admin/items/{item.id}/checkout"
        )

        co = db_session.execute(select(Checkout)).scalar_one()
        assert co.item_id == item.id
        assert co.item_unit_id is None
        assert co.user_id == ws.id
        assert co.returned_at is None
        assert co.condition_note == "back by Friday"
        assert co.expected_return is not None
        assert co.expected_return.date().isoformat() == "2026-06-15"
        # SQLite drops tz info on round-trip; compare naive-vs-naive.
        checked_out = co.checked_out_at.replace(tzinfo=None)
        assert before.replace(tzinfo=None) <= checked_out <= after.replace(
            tzinfo=None
        )

        # Engine isolation: no movement, item.current_qty unchanged.
        assert db_session.execute(select(StockMovement)).first() is None
        db_session.refresh(item)
        assert item.current_qty == Decimal("0")

    def test_blank_expected_return_becomes_none(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(expected_return="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        co = db_session.execute(select(Checkout)).scalar_one()
        assert co.expected_return is None

    def test_blank_condition_note_becomes_none(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(condition_note="   ", csrf=_csrf(client)),
            follow_redirects=False,
        )
        co = db_session.execute(select(Checkout)).scalar_one()
        assert co.condition_note is None

    def test_audit_row_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Banding Iron")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(
                expected_return="2026-06-15",
                condition_note="careful",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="checkout.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == ws.id
        assert row.entity_type == "checkout"
        co = db_session.execute(select(Checkout)).scalar_one()
        assert row.entity_id == co.id
        assert row.before_json is None
        assert row.after_json is not None
        assert row.after_json["item_id"] == item.id
        assert row.after_json["item_unit_id"] is None
        assert row.after_json["user_id"] == ws.id
        # ISO date with the synthetic UTC-midnight time.
        assert row.after_json["expected_return"].startswith("2026-06-15")
        assert row.after_json["condition_note"] == "careful"
        # checked_out_at present and ISO-formatted.
        assert "checked_out_at" in row.after_json
        assert "T" in row.after_json["checked_out_at"]

    def test_flash_message_set(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, name="Polishing Mop")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        # Render the destination page and confirm the flash includes the name.
        resp = client.get(f"/admin/items/{item.id}/checkout")
        assert "Polishing Mop" in resp.text


# ---------------------------------------------------------------------------
# Happy path — unique-tracked
# ---------------------------------------------------------------------------


class TestCheckoutUniqueHappyPath:
    def test_creates_row_with_unit_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="MOULD-1",
        )
        unit = _make_unit(db_session, item=item, serial="MOULD-1-A")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(unit.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        co = db_session.execute(select(Checkout)).scalar_one()
        assert co.item_id == item.id
        assert co.item_unit_id == unit.id
        assert co.user_id == ws.id
        assert co.returned_at is None

    def test_audit_carries_unit_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="MOULD-1",
        )
        unit = _make_unit(db_session, item=item, serial="MOULD-1-A")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(unit.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="checkout.created")
        assert len(rows) == 1
        assert rows[0].after_json is not None
        assert rows[0].after_json["item_unit_id"] == unit.id

    def test_picked_unit_disappears_from_next_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            tracking_mode=TrackingMode.UNIQUE,
            sku="MOULD-1",
        )
        u1 = _make_unit(db_session, item=item, serial="A")
        u2 = _make_unit(db_session, item=item, serial="B")
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(item_unit_id=str(u1.id), csrf=_csrf(client)),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items/{item.id}/checkout")
        # u1 is now in the status block, not the unit select.
        assert f'<option value="{u1.id}"' not in resp.text
        assert f'<option value="{u2.id}"' in resp.text


# ---------------------------------------------------------------------------
# Form link visibility on items_form / item_detail
# ---------------------------------------------------------------------------


class TestCheckoutLinkVisibility:
    def test_items_form_shows_link_for_flagged_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, requires_checkout=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        body = resp.text
        assert 'data-testid="checkout-link"' in body
        assert f"/admin/items/{item.id}/checkout" in body

    def test_items_form_hides_link_for_non_flagged_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, requires_checkout=False)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="checkout-link"' not in resp.text

    def test_items_form_hides_link_for_archived_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, requires_checkout=True, archived=True
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="checkout-link"' not in resp.text

    def test_item_detail_shows_link_for_flagged_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, requires_checkout=True)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/items/{item.id}/detail")
        assert 'data-testid="checkout-link"' in resp.text


# ---------------------------------------------------------------------------
# Engine isolation
# ---------------------------------------------------------------------------


class TestEngineIsolation:
    def test_no_stock_movement_or_qty_change(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        # Seed a non-zero current_qty so we can assert it doesn't move.
        item.current_qty = Decimal("17.0000")
        db_session.commit()
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        client.post(
            f"/admin/items/{item.id}/checkout",
            data=_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert db_session.execute(select(StockMovement)).first() is None
        db_session.refresh(item)
        assert item.current_qty == Decimal("17.0000")
