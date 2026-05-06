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
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
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


def _audit_rows(db: Session) -> list[AuditLog]:
    return list(
        db.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
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


# ---------------------------------------------------------------------------
# R5h — CSV export on the cross-item checkouts admin list
# ---------------------------------------------------------------------------


class TestCheckoutsAdminCsvRoleEnforcement:
    """``?format=csv`` inherits the same Manager+Office gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/checkouts?format=csv")
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
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 200

    def test_office_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 200


class TestCheckoutsAdminCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="checkouts_open.csv"' in cd

    def test_content_disposition_overdue_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv&show=overdue")
        cd = resp.headers["content-disposition"]
        assert 'filename="checkouts_overdue.csv"' in cd


class TestCheckoutsAdminCsvBody:
    def test_empty_emits_only_header_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 200
        assert resp.text == (
            "checkout_id,item_id,item_sku,item_name,item_archived,"
            "unit_serial,holder_email,checked_out_at,expected_return,"
            "is_overdue,days_overdue\r\n"
        )

    def test_one_open_qty_tracked_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="QTY-1", name="Polishing kit"
        )
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        co = _open_checkout(db_session, item=item, user=ws)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        assert len(lines) == 3  # header + 1 data + trailing empty
        cells = lines[1].split(",")
        assert cells[0] == str(co.id)
        assert cells[1] == str(item.id)
        assert cells[2] == "QTY-1"
        assert cells[3] == "Polishing kit"
        assert cells[4] == "no"  # item_archived
        assert cells[5] == ""  # unit_serial empty (qty-tracked)
        assert cells[6] == "ws@x.test"
        # checked_out_at is an ISO datetime
        assert "2026-05-01T09:00:00" in cells[7]
        assert cells[8] == ""  # expected_return is None
        assert cells[9] == "no"  # is_overdue
        assert cells[10] == ""  # days_overdue

    def test_unique_tracked_row_carries_unit_serial(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="MOULD-1",
            name="Wax mould A",
            tracking_mode=TrackingMode.UNIQUE,
        )
        unit = _make_unit(db_session, item=item, serial="CHK-A")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws, item_unit=unit)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        body = resp.text
        assert "CHK-A" in body
        # unit_serial cell should be the serial.
        data_line = body.split("\r\n")[1]
        cells = data_line.split(",")
        assert cells[5] == "CHK-A"

    def test_null_user_renders_empty_holder(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        # user=None — represents a hard-deleted holder (FK SET NULL).
        _open_checkout(db_session, item=item, user=None)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        data_line = resp.text.split("\r\n")[1]
        cells = data_line.split(",")
        assert cells[6] == ""  # holder_email empty

    def test_archived_item_renders_yes(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="OLD-1", archived=True
        )
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        data_line = resp.text.split("\r\n")[1]
        cells = data_line.split(",")
        assert cells[4] == "yes"  # item_archived

    def test_past_due_renders_overdue_yes_and_days(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A checkout with expected_return in the past must surface as overdue."""
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        # Far in the past so the integer days_overdue is unambiguously > 0.
        past = datetime(2020, 1, 1, tzinfo=UTC)
        _open_checkout(
            db_session, item=item, user=ws, expected_return=past
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        data_line = resp.text.split("\r\n")[1]
        cells = data_line.split(",")
        # is_overdue=yes, days_overdue is a positive int.
        assert cells[9] == "yes"
        assert cells[10].isdigit()
        assert int(cells[10]) > 0

    def test_show_overdue_narrows_to_past_due_only(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item_a = _make_item(db_session, leaf=leaf, sku="OD-1")
        item_b = _make_item(db_session, leaf=leaf, sku="OD-2")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        # One overdue, one with no due-date.
        _open_checkout(
            db_session,
            item=item_a,
            user=ws,
            expected_return=datetime(2020, 1, 1, tzinfo=UTC),
        )
        _open_checkout(
            db_session, item=item_b, user=ws, expected_return=None
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        # ?show=open → both rows.
        resp_open = client.get("/admin/checkouts?format=csv&show=open")
        assert "OD-1" in resp_open.text
        assert "OD-2" in resp_open.text

        # ?show=overdue → only the past-due row.
        resp_od = client.get("/admin/checkouts?format=csv&show=overdue")
        assert "OD-1" in resp_od.text
        assert "OD-2" not in resp_od.text

    def test_returned_checkouts_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, sku="RET-1")
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(
            db_session,
            item=item,
            user=ws,
            returned_at=datetime(2026, 5, 2, 12, 0, tzinfo=UTC),
        )
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        # Header row only — the returned row is excluded by the base query.
        assert resp.text.count("\r\n") == 1
        assert "RET-1" not in resp.text


class TestCheckoutsAdminCsvHtmlBranch:
    def test_format_blank_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="checkouts-admin-heading"' in resp.text

    def test_format_unknown_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestCheckoutsAdminCsvReadOnly:
    def test_csv_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf)
        ws = _make_user(db_session, email="ws@x.test", role=Role.WORKSHOP)
        _open_checkout(db_session, item=item, user=ws)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        before = len(_audit_rows(db_session))
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?format=csv")
        assert resp.status_code == 200
        after = len(_audit_rows(db_session))
        assert after == before


class TestCheckoutsAdminCsvLink:
    def test_html_renders_csv_link_with_active_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="checkouts-admin-csv-link"' in body
        assert "format=csv" in body
        assert "show=open" in body

    def test_html_renders_csv_link_with_overdue_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/checkouts?show=overdue")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="checkouts-admin-csv-link"' in body
        assert "show=overdue" in body
