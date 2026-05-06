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

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Location,
    Role,
    StockTake,
    TaxonomyNode,
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
