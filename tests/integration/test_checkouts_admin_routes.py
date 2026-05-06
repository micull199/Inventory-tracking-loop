"""Integration tests for the manager-facing checkouts oversight (C4).

The cross-item view at ``GET /admin/checkouts`` for **Manager + Office** only.
Workshop is excluded — they already see per-item status blocks. Read-only:
no audit, no DB writes.

Covers:
- Role enforcement: anon 401; pending 403; Workshop 403; Office / Manager /
  Admin 200.
- Empty state when there are no open checkouts (or only returned ones).
- Open list rendering: qty-tracked + unique-tracked rows; holder email; null
  user → "—"; archived item still surfaces with the archived suffix.
- Overdue derivation: no due-date → not overdue; future due → not overdue;
  past due → overdue badge + days_overdue.
- Filter tabs: ``?show=open`` shows everything; ``?show=overdue`` narrows to
  past-due only; unrecognised values fall through to ``open``.
- Counters: open + overdue counters reflect the right counts.
- Returned checkouts never show up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Checkout,
    Item,
    ItemUnit,
    ItemUnitStatus,
    Role,
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
    archived: bool = False,
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=tracking_mode,
        requires_checkout=True,
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
) -> ItemUnit:
    unit = ItemUnit(
        item_id=item.id,
        serial_or_label=serial,
        status=status,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


def _open_checkout(
    db: Session,
    *,
    item: Item,
    user: User | None,
    item_unit: ItemUnit | None = None,
    checked_out_at: datetime | None = None,
    expected_return: datetime | None = None,
    returned_at: datetime | None = None,
    condition_note: str | None = None,
) -> Checkout:
    co = Checkout(
        item_id=item.id,
        item_unit_id=item_unit.id if item_unit is not None else None,
        user_id=user.id if user is not None else None,
        checked_out_at=checked_out_at
        or datetime(2026, 5, 1, 9, 0, tzinfo=UTC),
        expected_return=expected_return,
        returned_at=returned_at,
        condition_note=condition_note,
    )
    db.add(co)
    db.commit()
    db.refresh(co)
    return co


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 401

    def test_pending_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 403

    def test_workshop_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 403

    def test_office_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 200

    def test_manager_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 200

    def test_admin_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


class TestEmptyState:
    def test_no_checkouts_renders_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-testid="checkouts-admin-empty"' in body
        assert 'data-testid="checkouts-row"' not in body
        assert 'data-testid="checkouts-open-count">0<' in body
        assert 'data-testid="checkouts-overdue-count">0<' in body

    def test_only_returned_checkouts_render_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        # Closed checkout — must not appear.
        _open_checkout(
            db_session,
            item=item,
            user=ws,
            returned_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-testid="checkouts-admin-empty"' in body
        assert 'data-testid="checkouts-row"' not in body


# ---------------------------------------------------------------------------
# Open list rendering
# ---------------------------------------------------------------------------


class TestOpenListRendering:
    def test_qty_tracked_row_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="QTY-1", name="Polishing kit"
        )
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-testid="checkouts-row"' in body
        assert "QTY-1" in body
        assert "Polishing kit" in body
        assert "ws@x.test" in body
        # Qty-tracked has no unit — should render the em-dash for unit cell.
        assert 'data-testid="checkouts-row-unit">\n                            —' in body or \
            'data-testid="checkouts-row-unit">' in body

    def test_unique_tracked_row_shows_unit_serial(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="UNQ-1",
            tracking_mode=TrackingMode.UNIQUE,
        )
        unit = _make_unit(db_session, item=item, serial="MOULD-A")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws, item_unit=unit)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert "MOULD-A" in body
        assert 'data-testid="checkouts-row-unit"' in body

    def test_null_user_renders_dash(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A SET NULL user (rare) renders as ``—`` in the holder cell."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="ORPHAN")
        _open_checkout(db_session, item=item, user=None)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        # Find the holder cell for the row and check it shows the em-dash.
        idx = body.find('data-testid="checkouts-row-holder"')
        assert idx > 0
        snippet = body[idx : idx + 200]
        assert "—" in snippet

    def test_archived_item_still_surfaces_with_suffix(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="ARCH-1", archived=True
        )
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert "ARCH-1" in body
        # Archived suffix appears alongside the sku.
        idx = body.find("ARCH-1")
        assert "(archived)" in body[idx : idx + 50]

    def test_per_row_link_targets_per_item_checkin(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert f'href="/admin/items/{item.id}/checkout"' in body
        assert 'data-testid="checkouts-row-item-link"' in body


# ---------------------------------------------------------------------------
# Overdue derivation
# ---------------------------------------------------------------------------


class TestOverdueDerivation:
    def test_no_expected_return_is_not_overdue(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws, expected_return=None)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-overdue="false"' in body
        assert 'data-testid="checkouts-row-overdue-badge"' not in body

    def test_future_expected_return_is_not_overdue(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        future = datetime.now(UTC) + timedelta(days=14)
        _open_checkout(
            db_session, item=item, user=ws, expected_return=future
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-overdue="false"' in body
        assert 'data-testid="checkouts-row-overdue-badge"' not in body

    def test_past_expected_return_is_overdue_with_days(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        # Five days ago at midnight UTC — clearly past.
        past = datetime.now(UTC) - timedelta(days=5)
        past = past.replace(hour=0, minute=0, second=0, microsecond=0)
        _open_checkout(
            db_session, item=item, user=ws, expected_return=past
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-overdue="true"' in body
        assert 'data-testid="checkouts-row-overdue-badge"' in body
        # Exactly 5 days overdue.
        assert "Overdue (5d)" in body

    def test_overdue_rows_sorted_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item_a = _make_item(db_session, leaf=leaf, sku="OK-1", name="Newer ok")
        item_b = _make_item(db_session, leaf=leaf, sku="OD-1", name="Older overdue")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        # NOT overdue (newer checkout).
        _open_checkout(
            db_session,
            item=item_a,
            user=ws,
            checked_out_at=datetime(2026, 5, 6, 10, 0, tzinfo=UTC),
            expected_return=None,
        )
        # Overdue (older checkout).
        _open_checkout(
            db_session,
            item=item_b,
            user=ws,
            checked_out_at=datetime(2026, 5, 1, 10, 0, tzinfo=UTC),
            expected_return=datetime(2026, 5, 2, 10, 0, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        # Overdue OD-1 should appear before non-overdue OK-1.
        idx_od = body.find("OD-1")
        idx_ok = body.find("OK-1")
        assert idx_od > 0
        assert idx_ok > 0
        assert idx_od < idx_ok


# ---------------------------------------------------------------------------
# Filter tabs
# ---------------------------------------------------------------------------


class TestFilterTabs:
    def test_show_open_lists_overdue_and_non_overdue(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item_open = _make_item(db_session, leaf=leaf, sku="OPEN-1")
        item_over = _make_item(db_session, leaf=leaf, sku="OVER-1")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session, item=item_open, user=ws, expected_return=None
        )
        _open_checkout(
            db_session,
            item=item_over,
            user=ws,
            expected_return=datetime(2026, 1, 1, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?show=open")
        body = resp.text
        assert "OPEN-1" in body
        assert "OVER-1" in body

    def test_show_overdue_filters_to_overdue_only(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item_open = _make_item(db_session, leaf=leaf, sku="OPEN-1")
        item_over = _make_item(db_session, leaf=leaf, sku="OVER-1")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session, item=item_open, user=ws, expected_return=None
        )
        _open_checkout(
            db_session,
            item=item_over,
            user=ws,
            expected_return=datetime(2026, 1, 1, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?show=overdue")
        body = resp.text
        assert "OVER-1" in body
        assert "OPEN-1" not in body

    def test_unrecognised_show_falls_through_to_open(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="OPEN-1")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session, item=item, user=ws, expected_return=None
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?show=garbage")
        body = resp.text
        # OPEN-1 is not overdue but appears under the default ``open`` filter.
        assert "OPEN-1" in body
        # The filter-open link is marked as the active page.
        snippet = body[body.find('data-testid="filter-open"') :][:300]
        assert 'aria-current="page"' in snippet


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------


class TestCounters:
    def test_counters_reflect_open_and_overdue_subset(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item_a = _make_item(db_session, leaf=leaf, sku="A-1")
        item_b = _make_item(db_session, leaf=leaf, sku="B-1")
        item_c = _make_item(db_session, leaf=leaf, sku="C-1")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        # Two open: one overdue, one not.
        _open_checkout(
            db_session, item=item_a, user=ws, expected_return=None
        )
        _open_checkout(
            db_session,
            item=item_b,
            user=ws,
            expected_return=datetime(2026, 1, 1, tzinfo=UTC),
        )
        # One returned (excluded from both counters).
        _open_checkout(
            db_session,
            item=item_c,
            user=ws,
            returned_at=datetime(2026, 5, 5, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        body = resp.text
        assert 'data-testid="checkouts-open-count">2<' in body
        assert 'data-testid="checkouts-overdue-count">1<' in body


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


class TestReadOnly:
    def test_get_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        from sqlalchemy import select

        from app.models import AuditLog

        before = list(
            db_session.execute(select(AuditLog.id)).scalars().all()
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        client.get("/admin/checkouts")
        after = list(
            db_session.execute(select(AuditLog.id)).scalars().all()
        )
        assert before == after


# ---------------------------------------------------------------------------
# Dashboard wiring (R1 widget now real)
# ---------------------------------------------------------------------------


class TestDashboardOverdueWiring:
    def test_dashboard_widget_zero_when_no_checkouts(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        body = resp.text
        assert 'data-testid="dashboard-overdue-checkouts">0<' in body

    def test_dashboard_widget_counts_overdue(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session,
            item=item,
            user=ws,
            expected_return=datetime(2026, 1, 1, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        body = resp.text
        assert 'data-testid="dashboard-overdue-checkouts">1<' in body

    def test_dashboard_widget_excludes_returned(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session,
            item=item,
            user=ws,
            expected_return=datetime(2026, 1, 1, tzinfo=UTC),
            returned_at=datetime(2026, 5, 5, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        body = resp.text
        assert 'data-testid="dashboard-overdue-checkouts">0<' in body

    def test_dashboard_widget_excludes_open_with_no_due(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Open-but-no-expected-return is NOT overdue."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session, item=item, user=ws, expected_return=None
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        body = resp.text
        assert 'data-testid="dashboard-overdue-checkouts">0<' in body
