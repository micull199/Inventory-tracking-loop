"""Integration tests for the reports surface (R4 — variance trend).

Covers:
- Role enforcement: anon 401; pending 403; Workshop 403 (cannot see aggregated
  cost data per MISSION §3); Manager / Office / Admin 200.
- Empty state: no completed stock takes; only scheduled / in-progress takes.
- Window filter: ``?days=N`` clamping; out-of-range coerces silently.
- Aggregation: positive + negative variance splits; net + abs sums; uncommitted
  + zero-variance lines excluded.
- Totals card: sum across in-window stock takes.
- Read-only invariant: GET writes no audit row.
- Dashboard wiring: the variance-trend link is rendered for Manager / Office.

The route is read-only — no audit, no mutations, no engine touches.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Item,
    Location,
    Role,
    StockTake,
    StockTakeLine,
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


def _make_node(db: Session, name: str = "Tools") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_location(db: Session, name: str = "Bench") -> Location:
    loc = Location(name=name)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def _make_item(
    db: Session,
    leaf: TaxonomyNode,
    *,
    sku: str = "ITEM-1",
    name: str = "Item",
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.QTY,
        current_qty=Decimal("10"),
        reorder_threshold=Decimal("0"),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_completed_st(
    db: Session,
    *,
    completed_at: datetime,
    scheduled_for: date | None = None,
    scope_node: TaxonomyNode | None = None,
    scope_location: Location | None = None,
) -> StockTake:
    """Helper: make a stock take in ``completed`` state at the given time."""
    st = StockTake(
        scheduled_for=scheduled_for or completed_at.date(),
        started_at=completed_at - timedelta(hours=1),
        completed_at=completed_at,
        scope_node_id=scope_node.id if scope_node is not None else None,
        scope_location_id=scope_location.id if scope_location is not None else None,
    )
    db.add(st)
    db.commit()
    db.refresh(st)
    return st


def _make_scheduled_st(db: Session) -> StockTake:
    st = StockTake(scheduled_for=date(2026, 6, 1))
    db.add(st)
    db.commit()
    db.refresh(st)
    return st


def _make_line(
    db: Session,
    *,
    st: StockTake,
    item: Item,
    variance: Decimal | None,
    committed: bool,
) -> StockTakeLine:
    line = StockTakeLine(
        stock_take_id=st.id,
        item_id=item.id,
        system_qty=Decimal("10.0000"),
        counted_qty=(Decimal("10.0000") + variance) if variance is not None else None,
        variance=variance,
        committed=committed,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


def _audit_count(db: Session) -> int:
    return len(list(db.execute(select(AuditLog)).scalars().all()))


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestVarianceTrendRoleEnforcement:
    def test_anonymous_get_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/reports/variance-trend")
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
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 403

    def test_workshop_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 403

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200

    def test_admin_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


class TestVarianceTrendEmptyState:
    def test_no_stock_takes_renders_empty(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-empty"' in resp.text
        # Totals card always renders.
        assert 'data-testid="variance-trend-stock-take-count">0' in resp.text

    def test_only_scheduled_stock_takes_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A stock take with no ``completed_at`` doesn't appear."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_scheduled_st(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-empty"' in resp.text
        assert 'data-testid="variance-trend-stock-take-count">0' in resp.text

    def test_in_progress_stock_take_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        """``started_at`` set without ``completed_at`` is in-progress, excluded."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = StockTake(
            scheduled_for=date(2026, 5, 1),
            started_at=datetime(2026, 5, 1, 9, tzinfo=UTC),
        )
        db_session.add(st)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-empty"' in resp.text


# ---------------------------------------------------------------------------
# Window filter
# ---------------------------------------------------------------------------


class TestVarianceTrendWindowFilter:
    def test_recent_completed_within_default_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(days=1)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("2"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-row"' in resp.text
        assert f'data-stock-take-id="{st.id}"' in resp.text

    def test_old_completed_excluded_by_default_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Default window is 90 days — a 100-day-old completion is excluded."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(days=100)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("2"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-empty"' in resp.text
        assert f'data-stock-take-id="{st.id}"' not in resp.text

    def test_old_completed_included_when_window_widened(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(days=100)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("2"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?days=200")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-row"' in resp.text
        assert f'data-stock-take-id="{st.id}"' in resp.text
        # Form pre-fills with the coerced (= submitted) value.
        assert 'value="200"' in resp.text

    def test_bad_days_silently_coerces_to_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?days=foo")
        assert resp.status_code == 200
        # Form input pre-fills with the coerced default of 90.
        assert 'value="90"' in resp.text


# ---------------------------------------------------------------------------
# Per-stock-take aggregation
# ---------------------------------------------------------------------------


class TestVarianceTrendAggregation:
    def test_mixed_variance_lines_split_by_sign(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Positive lines roll up to the positive cell; negative to the negative cell."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(db_session, leaf, sku="A-1")
        b = _make_item(db_session, leaf, sku="B-1")
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=st, item=a, variance=Decimal("3"), committed=True
        )
        _make_line(
            db_session, st=st, item=b, variance=Decimal("-5"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-row-lines-with-variance">2' in resp.text
        assert 'data-testid="variance-trend-row-positive">3' in resp.text
        assert 'data-testid="variance-trend-row-negative-abs">5' in resp.text
        assert 'data-testid="variance-trend-row-net">-2' in resp.text
        assert 'data-testid="variance-trend-row-abs">8' in resp.text

    def test_uncommitted_lines_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A completed stock take whose lines weren't committed shows zeros."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("3"), committed=False
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-row"' in resp.text
        assert 'data-testid="variance-trend-row-lines-with-variance">0' in resp.text
        assert 'data-testid="variance-trend-row-positive">0' in resp.text

    def test_zero_variance_lines_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A committed line with zero variance contributes nothing."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("0"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-row-lines-with-variance">0' in resp.text


# ---------------------------------------------------------------------------
# Totals card
# ---------------------------------------------------------------------------


class TestVarianceTrendTotals:
    def test_totals_sum_two_stock_takes(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(db_session, leaf, sku="A-1")
        b = _make_item(db_session, leaf, sku="B-1")
        st1 = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=2)
        )
        st2 = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=st1, item=a, variance=Decimal("3"), committed=True
        )
        _make_line(
            db_session, st=st2, item=b, variance=Decimal("-5"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-stock-take-count">2' in resp.text
        assert (
            'data-testid="variance-trend-total-lines-with-variance">2' in resp.text
        )
        assert 'data-testid="variance-trend-total-positive">3' in resp.text
        assert 'data-testid="variance-trend-total-negative-abs">5' in resp.text
        assert 'data-testid="variance-trend-total-net">-2' in resp.text
        assert 'data-testid="variance-trend-total-abs">8' in resp.text

    def test_newest_first_ordering(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Most-recently-completed stock take appears above older ones."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        old = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(days=10)
        )
        new = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=old, item=item, variance=Decimal("3"), committed=True
        )
        # Make a new line on the new stock take with a different variance so we
        # can find its row distinctly; reuse the same item is fine for the
        # ordering check.
        _make_line(
            db_session, st=new, item=item, variance=Decimal("-5"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        body = resp.text
        new_pos = body.index(f'data-stock-take-id="{new.id}"')
        old_pos = body.index(f'data-stock-take-id="{old.id}"')
        assert new_pos < old_pos


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


class TestVarianceTrendReadOnly:
    def test_get_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        before = _audit_count(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        after = _audit_count(db_session)
        assert after == before


# ---------------------------------------------------------------------------
# Dashboard wiring (link visibility)
# ---------------------------------------------------------------------------


class TestVarianceTrendDashboardLink:
    def test_manager_sees_variance_trend_link_on_dashboard(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200
        assert 'data-testid="dashboard-variance-trend-link"' in resp.text
        assert 'href="/admin/reports/variance-trend"' in resp.text

    def test_office_sees_variance_trend_link_on_dashboard(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/dashboard")
        assert resp.status_code == 200
        assert 'data-testid="dashboard-variance-trend-link"' in resp.text


# ---------------------------------------------------------------------------
# CSV export (R5)
# ---------------------------------------------------------------------------


class TestVarianceTrendCsvRoleEnforcement:
    """``?format=csv`` inherits the same role gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/reports/variance-trend?format=csv")
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
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200

    def test_office_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200


class TestVarianceTrendCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="variance_trend_90d.csv"' in cd

    def test_content_disposition_custom_window(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Filename reflects the active ``days`` param."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv&days=30")
        assert resp.status_code == 200
        cd = resp.headers["content-disposition"]
        assert 'filename="variance_trend_30d.csv"' in cd

    def test_bad_days_filename_uses_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Non-int days silently coerces to default 90 (same as HTML)."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv&days=foo")
        assert resp.status_code == 200
        cd = resp.headers["content-disposition"]
        assert 'filename="variance_trend_90d.csv"' in cd


class TestVarianceTrendCsvBody:
    def test_empty_emits_only_header_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200
        body = resp.text
        # Header row only — nine columns ending in CRLF.
        assert body == (
            "stock_take_id,scope,scheduled_for,completed_at,"
            "lines_with_variance,positive_variance,negative_variance_abs,"
            "net_variance,abs_variance\r\n"
        )

    def test_one_stock_take_one_data_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session, name="Tools")
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session,
            completed_at=datetime.now(UTC) - timedelta(hours=1),
            scope_node=leaf,
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("3"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        # Header + 1 data row + trailing empty (from final CRLF).
        assert len(lines) == 3
        assert lines[2] == ""
        # Data row carries the stock take id + scope + counts/sums. The
        # decimals come back at the column's scale 4 (e.g. "3.0000"), so the
        # tail check uses ``,1,3`` then matches the rest tolerantly.
        data = lines[1]
        assert data.startswith(f"{st.id},Category: Tools,")
        cells = data.split(",")
        # 9 columns total.
        assert len(cells) == 9
        assert cells[4] == "1"  # lines_with_variance
        assert Decimal(cells[5]) == Decimal("3")  # positive_variance
        assert Decimal(cells[6]) == Decimal("0")  # negative_variance_abs
        assert Decimal(cells[7]) == Decimal("3")  # net_variance
        assert Decimal(cells[8]) == Decimal("3")  # abs_variance

    def test_days_filter_applies_to_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A 100-day-old completion is excluded by default 90, included at 200."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(days=100)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("2"), committed=True
        )
        _login_as(client, mgr)
        # Default window: only header row.
        resp_default = client.get("/admin/reports/variance-trend?format=csv")
        assert resp_default.status_code == 200
        assert resp_default.text.count("\r\n") == 1
        # Widened window: header + 1 row.
        resp_wide = client.get(
            "/admin/reports/variance-trend?format=csv&days=200"
        )
        assert resp_wide.status_code == 200
        assert resp_wide.text.count("\r\n") == 2
        assert f",{st.id}," in resp_wide.text or resp_wide.text.split(
            "\r\n"
        )[1].startswith(f"{st.id},")

    def test_multiple_stock_takes_newest_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        old = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(days=10)
        )
        new = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=old, item=item, variance=Decimal("3"), committed=True
        )
        _make_line(
            db_session, st=new, item=item, variance=Decimal("-5"), committed=True
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200
        body = resp.text
        new_pos = body.index(f"\r\n{new.id},")
        old_pos = body.index(f"\r\n{old.id},")
        assert new_pos < old_pos


class TestVarianceTrendCsvHtmlBranch:
    def test_format_blank_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        """No format param → existing HTML response."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="variance-trend-heading"' in resp.text

    def test_format_unknown_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        """``?format=garbage`` falls back to HTML (silent coerce)."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestVarianceTrendCsvReadOnly:
    def test_csv_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf)
        st = _make_completed_st(
            db_session, completed_at=datetime.now(UTC) - timedelta(hours=1)
        )
        _make_line(
            db_session, st=st, item=item, variance=Decimal("3"), committed=True
        )
        before = _audit_count(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?format=csv")
        assert resp.status_code == 200
        after = _audit_count(db_session)
        assert after == before


class TestVarianceTrendCsvLink:
    def test_html_renders_csv_link_with_active_days(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reports/variance-trend?days=45")
        assert resp.status_code == 200
        assert 'data-testid="variance-trend-csv-link"' in resp.text
        # The link's href preserves the active ``days``.
        assert "format=csv" in resp.text
        assert "days=45" in resp.text
