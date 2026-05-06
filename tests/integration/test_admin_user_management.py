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
from sqlalchemy.orm import Session

from app.models import Role, User, UserStatus


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

    def test_admin_can_clear_a_role(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_invalid_role_value_returns_400(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_admin_cannot_demote_self(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_admin_cannot_clear_own_role(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_unknown_user_id_returns_404(
        self, client: TestClient, db_session: Session
    ) -> None:
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
    def test_admin_activates_user_with_role(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_admin_cannot_disable_self(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_admin_cannot_set_self_pending(
        self, client: TestClient, db_session: Session
    ) -> None:
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
    def test_workshop_post_role_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_manager_post_status_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_unauthenticated_post_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
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
    def test_pending_users_come_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Pending users are what the admin acts on — they sort to the top."""
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _make_user(
            db_session, email="active@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE
        )
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
