"""Integration tests for the auth router and role-protected routes.

We mock ``oauth.google.authorize_access_token`` so we don't need a live Google
OAuth conversation. The dev-login endpoint (mounted because ``APP_ENV=test``)
is also exercised — same path Playwright uses.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import auth as auth_module
from app.models import Role, User, UserStatus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_user(
    db: Session,
    *,
    email: str = "u@example.com",
    role: Role | None = Role.WORKSHOP,
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


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------


class TestAuthMe:
    def test_unauthenticated_gets_401(self, client: TestClient) -> None:
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_authenticated_returns_user_payload(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="me@example.com", role=Role.OFFICE)
        # Use the dev-login endpoint to put a real signed session cookie on the client.
        resp = client.post(
            "/auth/_dev-login",
            data={"email": user.email, "name": user.name, "sub": user.google_sub},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        resp = client.get("/auth/me")
        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "me@example.com"
        assert body["role"] == "office"
        assert body["status"] == "active"

    def test_disabled_user_with_valid_session_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Disabling a user must take effect immediately, even mid-session."""
        user = _make_user(
            db_session,
            email="banned@example.com",
            role=Role.OFFICE,
            status=UserStatus.ACTIVE,
        )
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        # Mid-session, an admin disables the account.
        user.status = UserStatus.DISABLED
        db_session.commit()

        resp = client.get("/auth/me")
        assert resp.status_code == 403

    def test_pending_user_with_valid_session_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A pending user (no role yet) shouldn't see the /me payload either."""
        client.post(
            "/auth/_dev-login",
            data={"email": "pending@example.com", "sub": "g-pending"},
            follow_redirects=False,
        )
        resp = client.get("/auth/me")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Role enforcement on /admin/users
# ---------------------------------------------------------------------------


class TestAdminUsersRoleEnforcement:
    def test_unauthenticated_gets_401(self, client: TestClient) -> None:
        resp = client.get("/admin/users")
        assert resp.status_code == 401

    def test_workshop_user_gets_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="w@example.com", role=Role.WORKSHOP)
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/admin/users")
        assert resp.status_code == 403

    def test_manager_user_gets_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="m@example.com", role=Role.MANAGER)
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/admin/users")
        assert resp.status_code == 403

    def test_admin_user_can_list(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@example.com", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _make_user(db_session, email="other@example.com", role=Role.OFFICE)
        client.post(
            "/auth/_dev-login",
            data={"email": admin.email, "sub": admin.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        # HTML page renders both users in the table.
        assert "admin@example.com" in resp.text
        assert "other@example.com" in resp.text
        assert 'data-testid="admin-users-table"' in resp.text

    def test_pending_user_with_admin_role_blocked(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Belt-and-braces: status=pending overrides role for access decisions."""
        user = _make_user(
            db_session,
            email="paused@example.com",
            role=Role.ADMIN,
            status=UserStatus.PENDING,
        )
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/admin/users")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Index page (anonymous, pending, active)
# ---------------------------------------------------------------------------


class TestIndexPage:
    def test_anonymous_sees_sign_in_button(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'data-testid="sign-in"' in resp.text
        assert "/auth/google/login" in resp.text

    def test_pending_user_sees_pending_message(
        self, client: TestClient, db_session: Session
    ) -> None:
        client.post(
            "/auth/_dev-login",
            data={"email": "newbie@example.com", "sub": "g-newbie"},
            follow_redirects=False,
        )
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'data-testid="pending-heading"' in resp.text

    def test_active_user_sees_welcome(self, client: TestClient, db_session: Session) -> None:
        user = _make_user(db_session, email="welcome@example.com", role=Role.OFFICE)
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/")
        assert resp.status_code == 200
        assert 'data-testid="welcome"' in resp.text


# ---------------------------------------------------------------------------
# Google OAuth callback (mocked)
# ---------------------------------------------------------------------------


class TestGoogleCallback:
    def test_callback_creates_user_and_redirects_to_root(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        fake_token: dict[str, Any] = {
            "userinfo": {
                "sub": "g-callback-1",
                "email": "callbacker@example.com",
                "name": "Callbacker",
            }
        }

        # Patch the Authlib client used by the route.
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(return_value=fake_token)
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: google_mock)

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

        from sqlalchemy import select

        user = db_session.execute(
            select(User).where(User.email == "callbacker@example.com")
        ).scalar_one()
        assert user.status is UserStatus.PENDING
        assert user.role is None

    def test_callback_logs_existing_user_in_and_updates_name(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        existing = _make_user(
            db_session,
            email="returning@example.com",
            role=Role.MANAGER,
            status=UserStatus.ACTIVE,
        )
        existing.name = "Old Name"
        db_session.commit()

        fake_token = {
            "userinfo": {
                "sub": existing.google_sub,
                "email": existing.email,
                "name": "Updated Name",
            }
        }
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(return_value=fake_token)
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: google_mock)

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(User, existing.id)
        assert refreshed is not None
        assert refreshed.name == "Updated Name"
        # Active manager was preserved, NOT reverted to pending.
        assert refreshed.role is Role.MANAGER
        assert refreshed.status is UserStatus.ACTIVE


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_clears_session(client: TestClient, db_session: Session) -> None:
    user = _make_user(db_session, email="logger@example.com", role=Role.WORKSHOP)
    client.post(
        "/auth/_dev-login",
        data={"email": user.email, "sub": user.google_sub},
        follow_redirects=False,
    )
    assert client.get("/auth/me").status_code == 200
    csrf = client.cookies["csrftoken"]

    resp = client.post(
        "/auth/logout",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert client.get("/auth/me").status_code == 401
