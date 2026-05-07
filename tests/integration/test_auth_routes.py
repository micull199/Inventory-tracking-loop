"""Integration tests for the auth router and role-protected routes.

We mock ``oauth.google.authorize_access_token`` so we don't need a live Google
OAuth conversation. The dev-login endpoint (mounted because ``APP_ENV=test``)
is also exercised — same path Playwright uses.
"""

from __future__ import annotations

import inspect
from typing import Any, ClassVar
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import auth as auth_module
from app.models import AuditLog, Role, User, UserStatus

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
# Google OAuth login (mocked)
# ---------------------------------------------------------------------------


class TestGoogleLogin:
    """Coverage for ``GET /auth/google/login``.

    The route delegates to Authlib's ``authorize_redirect`` which constructs a
    state-bearing URL into Google's authz endpoint and stores the expected
    state in the session. We mock the Authlib client so we don't make a real
    network call while still pinning the route's contract: 503 if Google isn't
    configured, otherwise return whatever the Authlib helper produced.
    """

    def test_login_redirects_to_google_authz_endpoint(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from fastapi.responses import RedirectResponse

        stub_url = "https://accounts.google.test/o/oauth2/v2/auth?state=stub-state"
        google_mock = AsyncMock()
        google_mock.authorize_redirect = AsyncMock(
            return_value=RedirectResponse(url=stub_url, status_code=307)
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

        resp = client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == stub_url

    def test_login_returns_503_when_google_not_configured(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: None)

        resp = client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 503
        assert "not configured" in resp.text.lower()

    def test_login_passes_callback_redirect_uri_to_authlib(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The redirect_uri passed to Authlib must end with the callback path
        and start with ``settings.app_base_url``. A future PR that drops the
        explicit ``redirect_uri`` (Authlib has a magic-from-request mode) would
        silently break the prod registration where the URI must match an
        allowlisted entry in the Google Cloud console — this test catches that
        regression in-process.
        """
        from fastapi.responses import RedirectResponse

        captured: dict[str, Any] = {}

        async def _capture_authorize_redirect(
            _request: Any, redirect_uri: str
        ) -> RedirectResponse:
            captured["redirect_uri"] = redirect_uri
            return RedirectResponse(url="https://stub", status_code=307)

        google_mock = AsyncMock()
        google_mock.authorize_redirect = _capture_authorize_redirect
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

        resp = client.get("/auth/google/login", follow_redirects=False)
        assert resp.status_code == 307

        from app.config import settings as app_settings

        expected_base = app_settings.app_base_url.rstrip("/")
        assert captured["redirect_uri"] == f"{expected_base}/auth/google/callback"


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

    # ----- Unhappy paths -----

    def test_callback_returns_400_on_oauth_error(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Authlib raises ``OAuthError`` for state-mismatch, unverified
        signature, expired code, etc. The callback handler must surface that
        as a 400 with the underlying error name in the detail.
        """
        from authlib.integrations.starlette_client import OAuthError

        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            side_effect=OAuthError(error="invalid_state", description="state mismatch")
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 400
        assert "OAuth error" in resp.text
        assert "invalid_state" in resp.text

    def test_callback_returns_400_when_userinfo_missing_email(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={"userinfo": {"sub": "g-no-email"}}
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 400
        assert "missing user identity" in resp.text

    def test_callback_returns_400_when_userinfo_missing_sub(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={"userinfo": {"email": "no-sub@example.com"}}
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 400
        assert "missing user identity" in resp.text

    def test_callback_fetches_userinfo_separately_when_token_omits_it(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Some providers (and some Authlib code paths) return the access token
        without the inline ``userinfo`` claim. The callback handler must fall
        back to ``google.userinfo(token=token)`` and complete the flow.
        """
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(return_value={})
        google_mock.userinfo = AsyncMock(
            return_value={
                "sub": "g-sep-1",
                "email": "separate@example.com",
                "name": "Separate Userinfo",
            }
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        google_mock.userinfo.assert_awaited_once()

        user = db_session.execute(
            select(User).where(User.email == "separate@example.com")
        ).scalar_one()
        assert user.status is UserStatus.PENDING
        assert user.role is None

    def test_callback_returns_503_when_google_not_configured(
        self,
        client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: None)

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 503
        assert "not configured" in resp.text.lower()

    # ----- Bootstrap-admin path through the route -----

    def test_callback_promotes_bootstrap_admin_when_email_matches_and_no_admin_exists(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First sign-in matching ``BOOTSTRAP_ADMIN_EMAIL`` (and no admin yet
        seeded) seeds the very first admin via the OAuth route. Both the
        ``user.created`` and ``user.bootstrap_admin_granted`` audit rows must
        land — the unit tests pin this for the helper, this pins it through the
        actual ``google_callback`` route boundary.
        """
        monkeypatch.setattr(auth_module.settings, "bootstrap_admin_email", "boss@uc.example")

        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={
                "userinfo": {
                    "sub": "g-boss-1",
                    "email": "boss@uc.example",
                    "name": "Boss",
                }
            }
        )
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: google_mock)

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303

        user = db_session.execute(
            select(User).where(User.google_sub == "g-boss-1")
        ).scalar_one()
        assert user.role is Role.ADMIN
        assert user.status is UserStatus.ACTIVE

        created_rows = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user.id,
                AuditLog.action == "user.created",
            )
        ).scalars().all()
        assert len(created_rows) == 1

        bootstrap_rows = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user.id,
                AuditLog.action == "user.bootstrap_admin_granted",
            )
        ).scalars().all()
        assert len(bootstrap_rows) == 1
        bootstrap = bootstrap_rows[0]
        assert bootstrap.before_json == {"role": None, "status": "pending"}
        assert bootstrap.after_json == {"role": "admin", "status": "active"}

    def test_callback_skips_bootstrap_when_admin_already_exists(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Bootstrap is a one-shot seed: once any admin exists, the route must
        not promote even when the env var still names a matching email. The new
        sign-in stays pending and no ``bootstrap_admin_granted`` row is written.
        """
        # Seed an admin via _make_user. This bypasses upsert_user_from_userinfo
        # (so no user.created audit row exists for this seed user) but still
        # makes the bootstrap-skip branch fire.
        _make_user(
            db_session,
            email="existing-admin@uc.example",
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        )
        monkeypatch.setattr(
            auth_module.settings, "bootstrap_admin_email", "newcomer@uc.example"
        )

        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={
                "userinfo": {
                    "sub": "g-newcomer-1",
                    "email": "newcomer@uc.example",
                    "name": "Newcomer",
                }
            }
        )
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: google_mock)

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303

        user = db_session.execute(
            select(User).where(User.google_sub == "g-newcomer-1")
        ).scalar_one()
        assert user.role is None
        assert user.status is UserStatus.PENDING

        bootstrap_rows = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user.id,
                AuditLog.action == "user.bootstrap_admin_granted",
            )
        ).scalars().all()
        assert bootstrap_rows == []

    def test_callback_skips_bootstrap_when_email_does_not_match(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``BOOTSTRAP_ADMIN_EMAIL`` is configured but the OAuth sign-in is for
        someone else — the new user must stay pending and no bootstrap audit
        row is written.
        """
        monkeypatch.setattr(
            auth_module.settings, "bootstrap_admin_email", "owner@uc.example"
        )

        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={
                "userinfo": {
                    "sub": "g-stranger-1",
                    "email": "someone-else@example.com",
                    "name": "Stranger",
                }
            }
        )
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: google_mock)

        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303

        user = db_session.execute(
            select(User).where(User.google_sub == "g-stranger-1")
        ).scalar_one()
        assert user.role is None
        assert user.status is UserStatus.PENDING

        bootstrap_rows = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user.id,
                AuditLog.action == "user.bootstrap_admin_granted",
            )
        ).scalars().all()
        assert bootstrap_rows == []

    def test_callback_re_signin_does_not_emit_second_user_created_audit(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Re-signing in via the OAuth route must not emit a second
        ``user.created`` row for the same user. The unit test_users.py pins the
        helper-level idempotency; this pins it at the route boundary.
        """
        userinfo: dict[str, Any] = {
            "sub": "g-returning-1",
            "email": "returning-once@example.com",
            "name": "Returning Once",
        }
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(return_value={"userinfo": userinfo})
        monkeypatch.setattr(auth_module.oauth, "create_client", lambda _name: google_mock)

        # First sign-in: creates the user + writes user.created.
        resp1 = client.get("/auth/google/callback", follow_redirects=False)
        assert resp1.status_code == 303
        client.cookies.clear()

        # Second sign-in: same userinfo (same sub) → updates name/email but
        # must NOT write another user.created row.
        resp2 = client.get("/auth/google/callback", follow_redirects=False)
        assert resp2.status_code == 303

        user = db_session.execute(
            select(User).where(User.google_sub == "g-returning-1")
        ).scalar_one()

        created_rows = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user.id,
                AuditLog.action == "user.created",
            )
        ).scalars().all()
        assert len(created_rows) == 1


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Drift-catcher: _dev-login ↔ google_callback parity
# ---------------------------------------------------------------------------
#
# The Playwright e2e suite signs users in via /auth/_dev-login because no real
# Google tenant is available to the autonomous loop. That coverage only stands
# in for the real google_callback handler if the two routes produce equivalent
# post-userinfo state for the same userinfo payload. This class pins that
# equivalence as a forcing function so a future PR can't silently diverge one
# path without the other.


class TestDevLoginAndCallbackParity:
    """Both ``_dev-login`` and ``google_callback`` must funnel through
    ``upsert_user_from_userinfo`` and produce the same User row + audit row +
    session ``user_id`` for the same userinfo payload."""

    _USERINFO: ClassVar[dict[str, Any]] = {
        "sub": "g-drift-1",
        "email": "drift@example.com",
        "name": "Drift Tester",
    }

    def _patch_oauth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={"userinfo": self._USERINFO}
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )

    def _post_dev_login(self, client: TestClient) -> None:
        resp = client.post(
            "/auth/_dev-login",
            data={
                "email": self._USERINFO["email"],
                "name": self._USERINFO["name"],
                "sub": self._USERINFO["sub"],
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def _get_callback(self, client: TestClient) -> None:
        resp = client.get("/auth/google/callback", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"

    def test_both_routes_call_upsert_user_from_userinfo(self) -> None:
        """Source-text forcing function: both routes must reference the shared
        upsert helper. Catches a future PR that inlines the user-creation logic
        on either side and silently diverges the two paths.
        """
        callback_src = inspect.getsource(auth_module.google_callback)
        assert "upsert_user_from_userinfo" in callback_src, (
            "google_callback must funnel through upsert_user_from_userinfo so "
            "_dev-login (which Playwright uses as a stand-in) covers it."
        )
        # dev_login is conditionally defined inside `if settings.app_env in
        # {"dev", "test"}:` — accessible at module level under APP_ENV=test.
        assert hasattr(auth_module, "dev_login"), (
            "dev_login should be mounted under APP_ENV=test"
        )
        dev_login_src = inspect.getsource(auth_module.dev_login)
        assert "upsert_user_from_userinfo" in dev_login_src, (
            "dev_login must funnel through upsert_user_from_userinfo so its "
            "Playwright coverage validly stands in for the real callback."
        )

    def test_callback_and_dev_login_produce_equivalent_user_row(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same shape of userinfo payload through each route → identical User
        row state for the load-bearing fields (name, role, status). The email +
        sub differ between paths because users.email is unique-constrained and
        audit_log row-deletion is blocked by triggers, so we can't reuse the
        same identity. Every other field is the contract OAuth1-DC pins.
        """
        # Path A: dev-login.
        self._post_dev_login(client)
        user_a = db_session.execute(
            select(User).where(User.google_sub == self._USERINFO["sub"])
        ).scalar_one()
        snapshot_a = (user_a.name, user_a.role, user_a.status)

        # Path B: google_callback (mocked Authlib) with a distinct identity.
        userinfo_b = {
            "sub": "g-drift-2",
            "email": "drift-2@example.com",
            "name": self._USERINFO["name"],
        }
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={"userinfo": userinfo_b}
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )
        self._get_callback(client)
        user_b = db_session.execute(
            select(User).where(User.google_sub == "g-drift-2")
        ).scalar_one()
        snapshot_b = (user_b.name, user_b.role, user_b.status)

        assert snapshot_a == snapshot_b
        assert snapshot_a == (self._USERINFO["name"], None, UserStatus.PENDING)

    def test_callback_and_dev_login_produce_equivalent_audit_row(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Same userinfo payload through each route → identical audit row shape
        (action, entity_type, before, and the load-bearing email key in after).
        """
        # Path A: dev-login.
        self._post_dev_login(client)
        user_a = db_session.execute(
            select(User).where(User.google_sub == self._USERINFO["sub"])
        ).scalar_one()
        audit_a = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user_a.id,
                AuditLog.action == "user.created",
            )
        ).scalars().all()

        # Path B: google_callback with a distinct identity (users.email is
        # unique; audit_log immutability blocks DELETE on path A's row).
        userinfo_b = {
            "sub": "g-drift-3",
            "email": "drift-3@example.com",
            "name": self._USERINFO["name"],
        }
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={"userinfo": userinfo_b}
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )
        self._get_callback(client)
        user_b = db_session.execute(
            select(User).where(User.google_sub == "g-drift-3")
        ).scalar_one()
        audit_b = db_session.execute(
            select(AuditLog).where(
                AuditLog.entity_type == "user",
                AuditLog.entity_id == user_b.id,
                AuditLog.action == "user.created",
            )
        ).scalars().all()

        # Each path wrote exactly one user.created row of the same shape.
        assert len(audit_a) == 1
        assert len(audit_b) == 1
        row_a, row_b = audit_a[0], audit_b[0]
        assert row_a.action == row_b.action == "user.created"
        assert row_a.entity_type == row_b.entity_type == "user"
        assert row_a.before_json is None
        assert row_b.before_json is None
        # The structural contract: same set of keys in after_json across paths,
        # each carrying the path's own user identity. Email + role + status are
        # the canonical user.created shape per upsert_user_from_userinfo.
        assert row_a.after_json is not None
        assert row_b.after_json is not None
        assert set(row_a.after_json.keys()) == set(row_b.after_json.keys())
        assert "email" in row_a.after_json
        assert row_a.after_json["email"] == self._USERINFO["email"]
        assert row_b.after_json["email"] == "drift-3@example.com"

    def test_callback_and_dev_login_set_session_user_id(
        self,
        client: TestClient,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Both routes must set ``request.session['user_id']`` so a follow-up
        request resolves the new user as authenticated. Without this, the
        sign-in redirect lands the user back at the anonymous index page.
        """
        # Path A: dev-login. Use /auth/me to verify the session resolved.
        self._post_dev_login(client)
        me_a = client.get("/auth/me")
        # New user is pending → /auth/me returns 403, but the resolution is
        # observable: the response body says "account not active" rather than
        # "not signed in" (401). That confirms the session cookie was set.
        assert me_a.status_code == 403, (
            f"expected 403 (account not active) after dev-login, got "
            f"{me_a.status_code}: {me_a.text}"
        )

        # Path B: google_callback with a distinct sub + a fresh client so
        # cookies don't leak across the two paths.
        userinfo_b = dict(self._USERINFO)
        userinfo_b["sub"] = "g-drift-4"
        userinfo_b["email"] = "drift-b@example.com"
        google_mock = AsyncMock()
        google_mock.authorize_access_token = AsyncMock(
            return_value={"userinfo": userinfo_b}
        )
        monkeypatch.setattr(
            auth_module.oauth, "create_client", lambda _name: google_mock
        )
        # Drop the path-A cookie so the path-B test client is fresh.
        client.cookies.clear()
        self._get_callback(client)
        me_b = client.get("/auth/me")
        assert me_b.status_code == 403, (
            f"expected 403 (account not active) after google_callback, got "
            f"{me_b.status_code}: {me_b.text}"
        )


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
