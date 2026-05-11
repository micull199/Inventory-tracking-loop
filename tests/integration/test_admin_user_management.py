"""Integration tests for the admin user-management mutations.

Covers:
- Admin can assign / clear a role on another user.
- Admin can change another user's status.
- Self-mutation guards (can't demote self, can't disable self).
- Active-without-role guard.
- Non-admin POSTs are 403.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None = None,
    status: UserStatus = UserStatus.PENDING,
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
    """Return the active CSRF token, bootstrapping the cookie if needed."""
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


class TestAdminSetRole:
    def test_admin_assigns_role_to_pending_user(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="newbie@x.test")
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "manager", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/users"

        db_session.expire_all()
        refreshed = db_session.get(User, target.id)
        assert refreshed is not None
        assert refreshed.role is Role.MANAGER

    def test_admin_can_clear_a_role(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="ex-staff@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        assert db_session.get(User, target.id).role is None  # type: ignore[union-attr]

    def test_invalid_role_value_returns_400(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="t@x.test")
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "wizard", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_admin_cannot_demote_self(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{admin.id}/role",
            data={"role": "workshop", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

        db_session.expire_all()
        assert db_session.get(User, admin.id).role is Role.ADMIN  # type: ignore[union-attr]

    def test_admin_cannot_clear_own_role(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{admin.id}/role",
            data={"role": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unknown_user_id_returns_404(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            "/admin/users/9999/role",
            data={"role": "workshop", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


class TestAdminSetStatus:
    def test_admin_activates_user_with_role(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="ready@x.test", role=Role.WORKSHOP, status=UserStatus.PENDING
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "active", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        assert db_session.get(User, target.id).status is UserStatus.ACTIVE  # type: ignore[union-attr]

    def test_admin_disables_user(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="t@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "disabled", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        assert db_session.get(User, target.id).status is UserStatus.DISABLED  # type: ignore[union-attr]

    def test_invalid_status_value_returns_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="t@x.test")
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "snoozing", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_cannot_activate_user_with_no_role(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Activating a roleless user is a UX trap — they'd see the welcome page
        with no permissions. Admin must assign a role first."""
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="roleless@x.test")  # role=None, status=PENDING
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "active", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

        db_session.expire_all()
        assert db_session.get(User, target.id).status is UserStatus.PENDING  # type: ignore[union-attr]

    def test_admin_cannot_disable_self(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{admin.id}/status",
            data={"status": "disabled", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

        db_session.expire_all()
        assert db_session.get(User, admin.id).status is UserStatus.ACTIVE  # type: ignore[union-attr]

    def test_admin_cannot_set_self_pending(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{admin.id}/status",
            data={"status": "pending", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestNonAdminCannotMutate:
    def test_workshop_post_role_is_403(self, client: TestClient, db_session: Session) -> None:
        attacker = _make_user(
            db_session, email="w@x.test", role=Role.WORKSHOP, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="t@x.test")
        _login_as(client, attacker)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "admin", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

        db_session.expire_all()
        assert db_session.get(User, target.id).role is None  # type: ignore[union-attr]

    def test_manager_post_status_is_403(self, client: TestClient, db_session: Session) -> None:
        attacker = _make_user(
            db_session, email="m@x.test", role=Role.MANAGER, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="t@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE
        )
        _login_as(client, attacker)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "disabled", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_unauthenticated_post_is_401(self, client: TestClient, db_session: Session) -> None:
        """Anon user with valid CSRF gets 401 — auth check runs after CSRF."""
        target = _make_user(db_session, email="t@x.test")
        # GET first to bootstrap a CSRF token; without that the request would
        # 403 on CSRF before reaching the auth check.
        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "admin", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 401


class TestAdminUsersListOrdering:
    def test_pending_users_come_first(self, client: TestClient, db_session: Session) -> None:
        """Pending users are what the admin acts on — they sort to the top."""
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _make_user(db_session, email="active@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE)
        _make_user(
            db_session, email="disabled@x.test", role=Role.OFFICE, status=UserStatus.DISABLED
        )
        _make_user(db_session, email="pending@x.test")  # default = pending, no role
        _login_as(client, admin)

        resp = client.get("/admin/users")
        assert resp.status_code == 200
        text = resp.text
        # Pending appears before active appears before disabled.
        assert text.index("pending@x.test") < text.index("active@x.test")
        assert text.index("active@x.test") < text.index("disabled@x.test")


# ---------------------------------------------------------------------------
# R5i — CSV export on the /admin/users list
# ---------------------------------------------------------------------------


class TestAdminUsersListCsvRoleEnforcement:
    """``?format=csv`` inherits the same Admin-only gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 401

    def test_pending_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="p@x.test")  # default pending
        _login_as(client, u)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP, status=UserStatus.ACTIVE)
        _login_as(client, ws)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 403

    def test_office_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE)
        _login_as(client, off)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        """User-management is Admin-only (MISSION §3) — Manager sees 403."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER, status=UserStatus.ACTIVE)
        _login_as(client, mgr)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 403

    def test_admin_csv_is_200(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 200


class TestAdminUsersListCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_filename(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="users.csv"' in cd


class TestAdminUsersListCsvBody:
    def test_header_row_is_first_line(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 200
        first_line = resp.text.split("\r\n")[0]
        assert first_line == "id,email,name,role,status,created_at"

    def test_pending_user_has_empty_role_cell(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        pending = _make_user(db_session, email="newbie@x.test")  # role=None, pending
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        body = resp.text
        # Find the data row for the pending user.
        lines = body.split("\r\n")
        pending_line = next(line for line in lines if "newbie@x.test" in line)
        cells = pending_line.split(",")
        assert cells[0] == str(pending.id)
        assert cells[1] == "newbie@x.test"
        # cells[2] = name; cells[3] = role (empty); cells[4] = status; cells[5] = created_at
        assert cells[3] == ""
        assert cells[4] == "pending"

    def test_role_and_status_cells_are_canonical_value_strings(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Enums are pre-coerced to their ``.value``, not Python's repr."""
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _make_user(db_session, email="manager@x.test", role=Role.MANAGER, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        body = resp.text
        # Locate the manager row.
        manager_line = next(line for line in body.split("\r\n") if "manager@x.test" in line)
        cells = manager_line.split(",")
        assert cells[3] == "manager"  # not "Role.MANAGER" or "<Role.MANAGER: 'manager'>"
        assert cells[4] == "active"  # not "UserStatus.ACTIVE"
        # And no Python repr leaks anywhere in the body.
        assert "Role." not in body
        assert "UserStatus." not in body

    def test_ordering_pending_first_then_active_then_disabled(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="aaa-admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _make_user(
            db_session,
            email="active-other@x.test",
            role=Role.OFFICE,
            status=UserStatus.ACTIVE,
        )
        _make_user(
            db_session,
            email="disabled@x.test",
            role=Role.OFFICE,
            status=UserStatus.DISABLED,
        )
        _make_user(db_session, email="pending@x.test")  # pending, no role
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        body = resp.text
        # Pending appears before active appears before disabled.
        assert body.index("pending@x.test") < body.index("active-other@x.test")
        assert body.index("active-other@x.test") < body.index("disabled@x.test")

    def test_admin_self_row_has_admin_role(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="me@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        body = resp.text
        my_line = next(line for line in body.split("\r\n") if "me@x.test" in line)
        cells = my_line.split(",")
        assert cells[3] == "admin"
        assert cells[4] == "active"


class TestAdminUsersListCsvHtmlBranch:
    def test_format_blank_renders_html(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="admin-users-table"' in resp.text

    def test_format_unknown_renders_html(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestAdminUsersListCsvReadOnly:
    def test_csv_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _make_user(db_session, email="other@x.test")  # at least one peer row
        before = db_session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        before_count = len(before)
        _login_as(client, admin)
        resp = client.get("/admin/users?format=csv")
        assert resp.status_code == 200
        after_count = len(
            db_session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
        )
        assert after_count == before_count


class TestAdminUsersListCsvLink:
    def test_html_renders_csv_link(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE)
        _login_as(client, admin)
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="admin-users-csv-link"' in body
        assert "format=csv" in body
