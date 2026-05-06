"""Integration tests for the stock-takes scheduling surface (ST1).

Three routes mounted at ``/admin/stock-takes`` for **Manager + Office** only.
Workshop is excluded — they don't run stock takes (per MISSION §3 "Office
user runs a stock take end-to-end").

ST1 only writes the ``scheduled`` state. Subsequent slices (ST2 + ST3) will
add start / count / commit. Read covers list + new-form rendering. Write
covers POST validation, audit shape, and happy path. No ``StockTakeLine``
rows are written in ST1.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    CostLayer,
    CostLayerConsumption,
    Item,
    Location,
    Role,
    StockMovement,
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


def _csrf(client: TestClient) -> str:
    """Get the cookie-bound CSRF token (issued on any GET)."""
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _make_node(
    db: Session,
    *,
    name: str = "Tools",
    parent: TaxonomyNode | None = None,
    archived: bool = False,
) -> TaxonomyNode:
    n = TaxonomyNode(
        name=name,
        parent_id=parent.id if parent is not None else None,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_location(
    db: Session, *, name: str = "Workshop bench", archived: bool = False
) -> Location:
    loc = Location(
        name=name,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def _make_stock_take(
    db: Session,
    *,
    scheduled_for: date | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    scope_node: TaxonomyNode | None = None,
    scope_location: Location | None = None,
    created_by: User | None = None,
    notes: str | None = None,
) -> StockTake:
    st = StockTake(
        scope_node_id=scope_node.id if scope_node is not None else None,
        scope_location_id=scope_location.id if scope_location is not None else None,
        scheduled_for=scheduled_for or date(2026, 6, 1),
        started_at=started_at,
        completed_at=completed_at,
        notes=notes,
        created_by=created_by.id if created_by is not None else None,
    )
    db.add(st)
    db.commit()
    db.refresh(st)
    return st


def _audit_rows(db: Session, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/stock-takes")
        assert resp.status_code == 401

    def test_anonymous_get_new_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/stock-takes/new")
        assert resp.status_code == 401

    def test_anonymous_post_is_401(self, client: TestClient) -> None:
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 401

    def test_pending_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, user)
        assert client.get("/admin/stock-takes").status_code == 403

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        assert client.get("/admin/stock-takes").status_code == 403

    def test_workshop_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 403

    def test_office_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        assert client.get("/admin/stock-takes").status_code == 200

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        assert client.get("/admin/stock-takes").status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        assert client.get("/admin/stock-takes").status_code == 200

    def test_office_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-01",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# List rendering
# ---------------------------------------------------------------------------


class TestListRendering:
    def test_empty_state(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        body = resp.text
        assert 'data-testid="stock-takes-empty"' in body
        assert 'data-testid="stock-takes-row"' not in body

    def test_show_open_lists_scheduled_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        body = resp.text
        assert 'data-testid="stock-takes-row"' in body
        assert 'data-testid="stock-takes-empty"' not in body
        assert "scheduled" in body  # status badge

    def test_show_completed_lists_completed_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_stock_take(
            db_session,
            created_by=mgr,
            started_at=datetime(2026, 5, 1, 9, tzinfo=UTC),
            completed_at=datetime(2026, 5, 1, 11, tzinfo=UTC),
        )
        _login_as(client, mgr)
        # default open tab → empty (the row is completed)
        resp = client.get("/admin/stock-takes")
        assert 'data-testid="stock-takes-empty"' in resp.text
        # completed tab → row visible
        resp = client.get("/admin/stock-takes?show=completed")
        assert 'data-testid="stock-takes-row"' in resp.text
        assert "completed" in resp.text

    def test_unrecognised_show_falls_through_to_open(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes?show=foo")
        # The scheduled row is visible (open path).
        assert 'data-testid="stock-takes-row"' in resp.text

    def test_in_progress_renders_in_open_tab(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_stock_take(
            db_session,
            created_by=mgr,
            started_at=datetime(2026, 5, 1, 9, tzinfo=UTC),
            completed_at=None,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        assert 'data-testid="stock-takes-row"' in resp.text
        assert "in_progress" in resp.text

    def test_scope_label_for_node(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = _make_node(db_session, name="Raw Materials")
        _make_stock_take(db_session, scope_node=node, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        assert "Category: Raw Materials" in resp.text

    def test_scope_label_for_location(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = _make_location(db_session, name="Safe")
        _make_stock_take(db_session, scope_location=loc, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        assert "Location: Safe" in resp.text

    def test_scope_label_for_all(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        assert "All items" in resp.text


# ---------------------------------------------------------------------------
# Form rendering
# ---------------------------------------------------------------------------


class TestNewFormRendering:
    def test_form_inputs_present(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes/new")
        body = resp.text
        assert resp.status_code == 200
        assert 'data-testid="stock-take-form"' in body
        assert 'name="csrf_token"' in body
        assert 'data-testid="stock-take-scope-type-input"' in body
        assert 'data-testid="stock-take-scope-node-input"' in body
        assert 'data-testid="stock-take-scope-location-input"' in body
        assert 'data-testid="stock-take-scheduled-for-input"' in body
        assert 'data-testid="stock-take-notes-input"' in body
        assert 'data-testid="stock-take-submit"' in body

    def test_active_nodes_in_select(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        active = _make_node(db_session, name="Active Tools")
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes/new")
        assert "Active Tools" in resp.text
        assert f'value="{active.id}"' in resp.text

    def test_archived_nodes_excluded_from_select(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_node(db_session, name="Archived Cat", archived=True)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes/new")
        # The select should only contain the placeholder option, no archived.
        # Slice to the node-select region.
        idx = resp.text.find('data-testid="stock-take-scope-node-input"')
        assert idx > 0
        end = resp.text.find("</select>", idx)
        snippet = resp.text[idx:end]
        assert "Archived Cat" not in snippet

    def test_archived_locations_excluded_from_select(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_location(db_session, name="Archived Loc", archived=True)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes/new")
        idx = resp.text.find('data-testid="stock-take-scope-location-input"')
        assert idx > 0
        end = resp.text.find("</select>", idx)
        snippet = resp.text[idx:end]
        assert "Archived Loc" not in snippet


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestValidation:
    def test_invalid_scope_type_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "garbage",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_blank_scope_type_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_blank_scheduled_for_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "",
            },
        )
        assert resp.status_code == 400

    def test_bad_scheduled_for_format_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "not-a-date",
            },
        )
        assert resp.status_code == 400

    def test_node_scope_blank_node_id_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "node",
                "scope_node_id": "",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_node_scope_non_int_node_id_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "node",
                "scope_node_id": "abc",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_node_scope_unknown_node_id_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "node",
                "scope_node_id": "99999",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_node_scope_archived_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        archived = _make_node(db_session, name="Old", archived=True)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "node",
                "scope_node_id": str(archived.id),
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_location_scope_archived_location_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        archived = _make_location(db_session, name="Old loc", archived=True)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "location",
                "scope_location_id": str(archived.id),
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_location_scope_blank_location_id_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "location",
                "scope_location_id": "",
                "scheduled_for": "2026-06-01",
            },
        )
        assert resp.status_code == 400

    def test_oversize_notes_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-01",
                "notes": "x" * 2001,
            },
        )
        assert resp.status_code == 400

    def test_failed_validation_writes_no_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "bad",
            },
        )
        assert resp.status_code == 400
        # No StockTake row, no audit row.
        assert (
            db_session.execute(select(StockTake)).scalars().first() is None
        )
        assert _audit_rows(db_session, action="stock_take.created") == []


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestHappyPathAll:
    def test_creates_row_with_both_scope_ids_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-15",
                "notes": "monthly count",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/stock-takes"
        st = db_session.execute(select(StockTake)).scalars().one()
        assert st.scope_node_id is None
        assert st.scope_location_id is None
        assert st.scheduled_for == date(2026, 6, 15)
        assert st.notes == "monthly count"
        assert st.created_by == mgr.id
        assert st.started_at is None
        assert st.completed_at is None

    def test_audit_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-15",
            },
        )
        rows = _audit_rows(db_session, action="stock_take.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.entity_type == "stock_take"
        assert row.before_json is None
        assert row.after_json == {
            "scope_node_id": None,
            "scope_location_id": None,
            "scheduled_for": "2026-06-15",
            "notes": None,
        }

    def test_flash_visible_after_redirect(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scheduled_for": "2026-06-15",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "2026-06-15" in resp.text
        assert "Stock take scheduled" in resp.text


class TestHappyPathNode:
    def test_creates_row_with_node_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = _make_node(db_session, name="Polishing supplies")
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "node",
                "scope_node_id": str(node.id),
                "scheduled_for": "2026-07-01",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        st = db_session.execute(select(StockTake)).scalars().one()
        assert st.scope_node_id == node.id
        assert st.scope_location_id is None

    def test_audit_carries_node_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = _make_node(db_session, name="Tools")
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "node",
                "scope_node_id": str(node.id),
                "scheduled_for": "2026-07-01",
            },
        )
        row = _audit_rows(db_session, action="stock_take.created")[0]
        assert row.after_json is not None
        assert row.after_json["scope_node_id"] == node.id
        assert row.after_json["scope_location_id"] is None


class TestHappyPathLocation:
    def test_creates_row_with_location_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = _make_location(db_session, name="Vault")
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "location",
                "scope_location_id": str(loc.id),
                "scheduled_for": "2026-07-15",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        st = db_session.execute(select(StockTake)).scalars().one()
        assert st.scope_node_id is None
        assert st.scope_location_id == loc.id

    def test_audit_carries_location_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = _make_location(db_session, name="Vault")
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "location",
                "scope_location_id": str(loc.id),
                "scheduled_for": "2026-07-15",
            },
        )
        row = _audit_rows(db_session, action="stock_take.created")[0]
        assert row.after_json is not None
        assert row.after_json["scope_node_id"] is None
        assert row.after_json["scope_location_id"] == loc.id


class TestScopeAllIgnoresIdInputs:
    def test_node_id_ignored_when_scope_is_all(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A user who toggles to ``all`` after picking a node should not have
        the picked node land on the row. The route ignores node/location ids
        when scope_type is all."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = _make_node(db_session, name="Tools")
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes",
            data={
                "csrf_token": token,
                "scope_type": "all",
                "scope_node_id": str(node.id),  # should be ignored
                "scheduled_for": "2026-06-01",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        st = db_session.execute(select(StockTake)).scalars().one()
        assert st.scope_node_id is None


# ---------------------------------------------------------------------------
# Layout / nav
# ---------------------------------------------------------------------------


class TestLayoutNav:
    def test_manager_sees_nav_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/")
        assert 'data-testid="nav-stock-takes"' in resp.text
        assert 'href="/admin/stock-takes"' in resp.text

    def test_office_sees_nav_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get("/")
        assert 'data-testid="nav-stock-takes"' in resp.text

    def test_admin_sees_nav_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/")
        assert 'data-testid="nav-stock-takes"' in resp.text

    def test_workshop_does_not_see_nav_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/")
        assert 'data-testid="nav-stock-takes"' not in resp.text

    def test_aria_current_on_stock_takes_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        snippet = resp.text[resp.text.find('data-testid="nav-stock-takes"') :]
        assert 'aria-current="page"' in snippet[:300]


# ===========================================================================
# ST2 — detail / start / counts
# ===========================================================================


def _make_item(
    db: Session,
    *,
    leaf: TaxonomyNode,
    sku: str = "ITEM-1",
    name: str = "Item",
    current_qty: Decimal = Decimal("0"),
    location: Location | None = None,
    archived: bool = False,
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.QTY,
        current_qty=current_qty,
        location_id=location.id if location is not None else None,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _start_st(db: Session, st: StockTake) -> StockTake:
    """Mark a stock take as ``in_progress`` directly (skip the route)."""
    st.started_at = datetime(2026, 5, 1, 9, tzinfo=UTC)
    db.commit()
    db.refresh(st)
    return st


def _make_line(
    db: Session,
    *,
    st: StockTake,
    item: Item,
    system_qty: Decimal = Decimal("10.0000"),
    counted_qty: Decimal | None = None,
    variance: Decimal | None = None,
) -> StockTakeLine:
    line = StockTakeLine(
        stock_take_id=st.id,
        item_id=item.id,
        system_qty=system_qty,
        counted_qty=counted_qty,
        variance=variance,
    )
    db.add(line)
    db.commit()
    db.refresh(line)
    return line


# ---------------------------------------------------------------------------
# Detail page — role enforcement
# ---------------------------------------------------------------------------


class TestDetailRoleEnforcement:
    def test_anonymous_get_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        assert resp.status_code == 401

    def test_pending_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        assert client.get(f"/admin/stock-takes/{st.id}").status_code == 403

    def test_workshop_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        assert client.get(f"/admin/stock-takes/{st.id}").status_code == 403

    def test_office_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        assert client.get(f"/admin/stock-takes/{st.id}").status_code == 200

    def test_admin_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        assert client.get(f"/admin/stock-takes/{st.id}").status_code == 200


# ---------------------------------------------------------------------------
# Detail page — render branches
# ---------------------------------------------------------------------------


class TestDetailRenderScheduled:
    def test_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        assert client.get("/admin/stock-takes/9999").status_code == 404

    def test_scheduled_with_items_shows_start_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session, name="Tools")
        _make_item(
            db_session, leaf=leaf, sku="WID-1", current_qty=Decimal("42")
        )
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        body = resp.text
        assert resp.status_code == 200
        assert 'data-testid="stock-take-detail-heading"' in body
        assert 'data-status="scheduled"' in body
        assert 'data-testid="stock-take-start-form"' in body
        assert 'data-testid="stock-take-start-submit"' in body
        assert 'data-testid="stock-take-scope-row"' in body
        assert "WID-1" in body

    def test_scheduled_with_empty_scope_hides_start_button(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session, name="Tools")
        # All items archived → empty scope.
        _make_item(db_session, leaf=leaf, sku="WID-1", archived=True)
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        body = resp.text
        assert 'data-testid="stock-take-no-items-note"' in body
        assert 'data-testid="stock-take-start-form"' not in body

    def test_scope_label_visible(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = _make_node(db_session, name="Polishing supplies")
        st = _make_stock_take(db_session, created_by=mgr, scope_node=node)
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        assert "Category: Polishing supplies" in resp.text


class TestDetailRenderInProgress:
    def test_shows_count_form_with_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session, name="Tools")
        item = _make_item(
            db_session, leaf=leaf, sku="WID-1", current_qty=Decimal("10")
        )
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        _make_line(db_session, st=st, item=item, system_qty=Decimal("10"))
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        body = resp.text
        assert resp.status_code == 200
        assert 'data-status="in_progress"' in body
        assert 'data-testid="stock-take-count-form"' in body
        assert 'data-testid="stock-take-count-row"' in body
        assert 'data-testid="stock-take-count-counted-input"' in body
        assert 'data-testid="stock-take-count-submit"' in body
        assert 'data-testid="stock-take-start-form"' not in body

    def test_shows_progress_summary(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WID-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        _make_line(
            db_session,
            st=st,
            item=item,
            system_qty=Decimal("10"),
            counted_qty=Decimal("8"),
            variance=Decimal("-2"),
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        body = resp.text
        assert 'data-testid="stock-take-progress-counted">1' in body
        assert 'data-testid="stock-take-progress-uncounted">0' in body
        assert 'data-testid="stock-take-progress-with-variance">1' in body

    def test_renders_existing_counted_qty_in_input(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WID-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        _make_line(
            db_session,
            st=st,
            item=item,
            system_qty=Decimal("10"),
            counted_qty=Decimal("12"),
            variance=Decimal("2"),
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        body = resp.text
        # Variance rendered with leading sign.
        assert "+2" in body


class TestDetailRenderCompleted:
    def test_shows_read_only_table(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="WID-1")
        st = _make_stock_take(
            db_session,
            created_by=mgr,
            started_at=datetime(2026, 5, 1, 9, tzinfo=UTC),
            completed_at=datetime(2026, 5, 1, 11, tzinfo=UTC),
        )
        _make_line(
            db_session,
            st=st,
            item=item,
            counted_qty=Decimal("10"),
            variance=Decimal("0"),
        )
        _login_as(client, mgr)
        resp = client.get(f"/admin/stock-takes/{st.id}")
        body = resp.text
        assert 'data-status="completed"' in body
        assert 'data-testid="stock-take-count-form"' not in body
        assert 'data-testid="stock-take-start-form"' not in body
        assert 'data-testid="stock-take-count-row"' in body


# ---------------------------------------------------------------------------
# Start route — role + validation
# ---------------------------------------------------------------------------


class TestStartRoleEnforcement:
    def test_anonymous_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 401

    def test_pending_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.OFFICE,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 403

    def test_workshop_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 403

    def test_office_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestStartValidation:
    def test_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes/9999/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 404

    def test_already_started_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 400

    def test_already_completed_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(
            db_session,
            created_by=mgr,
            started_at=datetime(2026, 5, 1, 9, tzinfo=UTC),
            completed_at=datetime(2026, 5, 1, 11, tzinfo=UTC),
        )
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 400

    def test_zero_items_in_scope_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1", archived=True)
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 400

    def test_failed_validation_writes_no_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        before_started = st.started_at
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        assert resp.status_code == 400
        db_session.refresh(st)
        assert st.started_at == before_started
        assert (
            db_session.execute(
                select(StockTakeLine).where(
                    StockTakeLine.stock_take_id == st.id
                )
            ).scalars().all()
            == []
        )
        assert _audit_rows(db_session, action="stock_take.started") == []


# ---------------------------------------------------------------------------
# Start happy paths
# ---------------------------------------------------------------------------


class TestStartHappyPathAllScope:
    def test_creates_one_line_per_active_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(
            db_session, leaf=leaf, sku="A-1", current_qty=Decimal("10")
        )
        b = _make_item(
            db_session, leaf=leaf, sku="B-1", current_qty=Decimal("20")
        )
        _make_item(
            db_session, leaf=leaf, sku="ARCH", archived=True
        )  # excluded
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        lines = (
            db_session.execute(
                select(StockTakeLine).where(
                    StockTakeLine.stock_take_id == st.id
                )
            ).scalars().all()
        )
        assert len(lines) == 2
        by_item = {ln.item_id: ln for ln in lines}
        assert by_item[a.id].system_qty == Decimal("10.0000")
        assert by_item[b.id].system_qty == Decimal("20.0000")
        assert by_item[a.id].counted_qty is None
        assert by_item[a.id].variance is None
        assert by_item[a.id].committed is False

    def test_started_at_is_set(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        db_session.refresh(st)
        assert st.started_at is not None
        assert st.completed_at is None

    def test_audit_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(
            db_session, leaf=leaf, sku="A-1", current_qty=Decimal("10")
        )
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        rows = _audit_rows(db_session, action="stock_take.started")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.entity_type == "stock_take"
        assert row.entity_id == st.id
        assert row.before_json == {"started_at": None}
        assert row.after_json is not None
        assert row.after_json["started_at"]
        assert len(row.after_json["lines"]) == 1
        snapshot_row = row.after_json["lines"][0]
        assert snapshot_row["item_id"] == a.id
        assert snapshot_row["system_qty"] == "10.0000"
        assert isinstance(snapshot_row["line_id"], int)

    def test_flash_visible_after_redirect(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "1 item" in resp.text


class TestStartHappyPathNodeScope:
    def test_only_node_items_become_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf_a = _make_node(db_session, name="Cat A")
        leaf_b = _make_node(db_session, name="Cat B")
        in_a = _make_item(db_session, leaf=leaf_a, sku="IN-A")
        _make_item(db_session, leaf=leaf_b, sku="IN-B")
        st = _make_stock_take(
            db_session, created_by=mgr, scope_node=leaf_a
        )
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        lines = (
            db_session.execute(
                select(StockTakeLine).where(
                    StockTakeLine.stock_take_id == st.id
                )
            ).scalars().all()
        )
        assert {ln.item_id for ln in lines} == {in_a.id}

    def test_descendant_items_included(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_node(db_session, name="Parent")
        child = TaxonomyNode(name="Child", parent_id=parent.id)
        db_session.add(child)
        db_session.commit()
        db_session.refresh(child)
        in_parent = _make_item(db_session, leaf=parent, sku="P-1")
        in_child = _make_item(db_session, leaf=child, sku="C-1")
        st = _make_stock_take(db_session, created_by=mgr, scope_node=parent)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        lines = (
            db_session.execute(
                select(StockTakeLine).where(
                    StockTakeLine.stock_take_id == st.id
                )
            ).scalars().all()
        )
        assert {ln.item_id for ln in lines} == {in_parent.id, in_child.id}


class TestStartHappyPathLocationScope:
    def test_only_location_items_become_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        loc = _make_location(db_session, name="Vault")
        loc_b = _make_location(db_session, name="Bench")
        in_loc = _make_item(
            db_session, leaf=leaf, sku="IN-L", location=loc
        )
        _make_item(db_session, leaf=leaf, sku="OUT-L", location=loc_b)
        st = _make_stock_take(
            db_session, created_by=mgr, scope_location=loc
        )
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        lines = (
            db_session.execute(
                select(StockTakeLine).where(
                    StockTakeLine.stock_take_id == st.id
                )
            ).scalars().all()
        )
        assert {ln.item_id for ln in lines} == {in_loc.id}


# ---------------------------------------------------------------------------
# Counts route — role + validation
# ---------------------------------------------------------------------------


class TestCountsRoleEnforcement:
    def test_anonymous_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token},
        )
        assert resp.status_code == 401

    def test_pending_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.OFFICE,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token},
        )
        assert resp.status_code == 403

    def test_workshop_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token},
        )
        assert resp.status_code == 403

    def test_office_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestCountsValidation:
    def test_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            "/admin/stock-takes/9999/counts",
            data={"csrf_token": token},
        )
        assert resp.status_code == 404

    def test_not_in_progress_scheduled_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token},
        )
        assert resp.status_code == 400

    def test_not_in_progress_completed_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(
            db_session,
            created_by=mgr,
            started_at=datetime(2026, 5, 1, 9, tzinfo=UTC),
            completed_at=datetime(2026, 5, 1, 11, tzinfo=UTC),
        )
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token},
        )
        assert resp.status_code == 400

    def test_non_numeric_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(db_session, st=st, item=item)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": "abc"},
        )
        assert resp.status_code == 400

    def test_negative_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(db_session, st=st, item=item)
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": "-3"},
        )
        assert resp.status_code == 400

    def test_failed_validation_writes_no_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(db_session, st=st, item=item)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": "abc"},
        )
        db_session.refresh(line)
        assert line.counted_qty is None
        assert line.variance is None
        assert _audit_rows(db_session, action="stock_take.counted") == []


# ---------------------------------------------------------------------------
# Counts happy paths
# ---------------------------------------------------------------------------


class TestCountsHappyPath:
    def test_single_line_update(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(
            db_session, st=st, item=item, system_qty=Decimal("10")
        )
        _login_as(client, mgr)
        token = _csrf(client)
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": "12"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(line)
        assert line.counted_qty == Decimal("12")
        assert line.variance == Decimal("2")

    def test_blank_uncounts(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(
            db_session,
            st=st,
            item=item,
            system_qty=Decimal("10"),
            counted_qty=Decimal("8"),
            variance=Decimal("-2"),
        )
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": ""},
        )
        db_session.refresh(line)
        assert line.counted_qty is None
        assert line.variance is None

    def test_multi_line_partial_update(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A-1")
        b = _make_item(db_session, leaf=leaf, sku="B-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line_a = _make_line(db_session, st=st, item=a, system_qty=Decimal("5"))
        line_b = _make_line(db_session, st=st, item=b, system_qty=Decimal("8"))
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={
                "csrf_token": token,
                f"counted_{line_a.id}": "5",  # match (variance 0)
                f"counted_{line_b.id}": "10",  # excess
            },
        )
        db_session.refresh(line_a)
        db_session.refresh(line_b)
        assert line_a.counted_qty == Decimal("5")
        assert line_a.variance == Decimal("0")
        assert line_b.counted_qty == Decimal("10")
        assert line_b.variance == Decimal("2")

    def test_no_op_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(db_session, leaf=leaf, sku="A-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(
            db_session,
            st=st,
            item=item,
            system_qty=Decimal("10"),
            counted_qty=Decimal("8"),
            variance=Decimal("-2"),
        )
        _login_as(client, mgr)
        token = _csrf(client)
        # Re-submit the same value.
        resp = client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": "8"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="stock_take.counted") == []

    def test_missing_key_leaves_line_unchanged(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A-1")
        b = _make_item(db_session, leaf=leaf, sku="B-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line_a = _make_line(
            db_session,
            st=st,
            item=a,
            system_qty=Decimal("5"),
            counted_qty=Decimal("4"),
            variance=Decimal("-1"),
        )
        line_b = _make_line(db_session, st=st, item=b, system_qty=Decimal("8"))
        _login_as(client, mgr)
        token = _csrf(client)
        # Only submit line_b's count; line_a's key is missing.
        client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line_b.id}": "8"},
        )
        db_session.refresh(line_a)
        db_session.refresh(line_b)
        # line_a should still carry its previous counted_qty.
        assert line_a.counted_qty == Decimal("4")
        assert line_a.variance == Decimal("-1")
        assert line_b.counted_qty == Decimal("8")
        assert line_b.variance == Decimal("0")


class TestCountsAuditShape:
    def test_only_changed_lines_in_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        a = _make_item(db_session, leaf=leaf, sku="A-1")
        b = _make_item(db_session, leaf=leaf, sku="B-1")
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line_a = _make_line(
            db_session,
            st=st,
            item=a,
            system_qty=Decimal("5"),
            counted_qty=Decimal("5"),
            variance=Decimal("0"),
        )
        line_b = _make_line(db_session, st=st, item=b, system_qty=Decimal("8"))
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={
                "csrf_token": token,
                f"counted_{line_a.id}": "5",  # no change (already 5)
                f"counted_{line_b.id}": "10",  # change (None → 10)
            },
        )
        rows = _audit_rows(db_session, action="stock_take.counted")
        assert len(rows) == 1
        assert rows[0].before_json is not None
        assert rows[0].after_json is not None
        before_lines = rows[0].before_json["lines"]
        after_lines = rows[0].after_json["lines"]
        assert len(before_lines) == 1
        assert before_lines[0]["line_id"] == line_b.id
        assert before_lines[0]["counted_qty"] is None
        assert after_lines[0]["counted_qty"] == "10"
        # Variance is computed as ``counted - system``. ``system`` is read
        # back from the column at scale 4 (``Decimal("8.0000")``) so the
        # difference inherits scale 4 (``Decimal("2.0000")``).
        assert after_lines[0]["variance"] == "2.0000"


# ---------------------------------------------------------------------------
# Engine isolation (M1's invariants)
# ---------------------------------------------------------------------------


class TestEngineIsolation:
    def test_start_writes_no_engine_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="A-1", current_qty=Decimal("42")
        )
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/start",
            data={"csrf_token": token},
        )
        db_session.refresh(item)
        # current_qty unchanged.
        assert item.current_qty == Decimal("42.0000")
        # No movements / cost layers / consumption rows.
        assert (
            db_session.execute(select(StockMovement)).scalars().all() == []
        )
        assert db_session.execute(select(CostLayer)).scalars().all() == []
        assert (
            db_session.execute(select(CostLayerConsumption)).scalars().all()
            == []
        )

    def test_counts_writes_no_engine_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_node(db_session)
        item = _make_item(
            db_session, leaf=leaf, sku="A-1", current_qty=Decimal("42")
        )
        st = _make_stock_take(db_session, created_by=mgr)
        _start_st(db_session, st)
        line = _make_line(
            db_session, st=st, item=item, system_qty=Decimal("42")
        )
        _login_as(client, mgr)
        token = _csrf(client)
        client.post(
            f"/admin/stock-takes/{st.id}/counts",
            data={"csrf_token": token, f"counted_{line.id}": "40"},
        )
        db_session.refresh(item)
        assert item.current_qty == Decimal("42.0000")
        assert (
            db_session.execute(select(StockMovement)).scalars().all() == []
        )
        assert db_session.execute(select(CostLayer)).scalars().all() == []


# ---------------------------------------------------------------------------
# List page — detail link
# ---------------------------------------------------------------------------


class TestListDetailLink:
    def test_detail_link_visible_per_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        st = _make_stock_take(db_session, created_by=mgr)
        _login_as(client, mgr)
        resp = client.get("/admin/stock-takes")
        body = resp.text
        assert 'data-testid="stock-takes-row-detail-link"' in body
        assert f'href="/admin/stock-takes/{st.id}"' in body
