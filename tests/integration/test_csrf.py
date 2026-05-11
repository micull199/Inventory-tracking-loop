"""Integration tests for the CSRF middleware.

We use the real FastAPI app so the middleware is exercised exactly as it ships
to prod. The double-submit-cookie pattern is verified end-to-end:

- A GET issues a fresh ``csrftoken`` cookie when none is present.
- POST/PUT/PATCH/DELETE require a matching token in the form body or
  ``X-CSRF-Token`` header.
- A small set of paths is exempt: the OAuth callback (provider-initiated, can't
  carry a token) and ``/auth/_dev-login`` (test-only backdoor).
- Templates render the active token into a hidden form field so server-rendered
  forms work without any JS.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Role, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str = "u@example.com",
    role: Role | None = Role.ADMIN,
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


class TestCSRFCookieIssuance:
    def test_get_request_sets_csrftoken_cookie(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        assert "csrftoken" in client.cookies
        assert len(client.cookies["csrftoken"]) >= 32

    def test_subsequent_get_keeps_existing_token(self, client: TestClient) -> None:
        client.get("/")
        token1 = client.cookies["csrftoken"]
        client.get("/")
        token2 = client.cookies["csrftoken"]
        assert token1 == token2


class TestCSRFRequiredOnMutations:
    def test_post_logout_without_token_is_403(self, client: TestClient) -> None:
        # No cookie at all → reject. The session cookie still flows because
        # SameSite=Lax + a same-origin POST, but the CSRF check must trip.
        resp = client.post("/auth/logout", follow_redirects=False)
        assert resp.status_code == 403

    def test_post_logout_with_mismatched_token_is_403(self, client: TestClient) -> None:
        client.get("/")  # bootstrap cookie
        resp = client.post(
            "/auth/logout",
            data={"csrf_token": "definitely-not-the-real-token"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_post_logout_with_valid_form_token_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="x@example.com", role=Role.WORKSHOP)
        # Sign in via the dev-login backdoor (exempt from CSRF).
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        # GET to ensure the csrftoken cookie has been issued for this client.
        client.get("/")
        token = client.cookies["csrftoken"]

        resp = client.post(
            "/auth/logout",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_post_with_valid_header_token_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        """HTMX requests will set ``X-CSRF-Token`` rather than a form field."""
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        client.post(
            "/auth/_dev-login",
            data={"email": admin.email, "sub": admin.google_sub},
            follow_redirects=False,
        )
        client.get("/")
        token = client.cookies["csrftoken"]

        target = _make_user(db_session, email="target@x.test", role=None, status=UserStatus.PENDING)
        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "workshop"},  # no csrf_token field
            headers={"X-CSRF-Token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303


class TestCSRFExemptPaths:
    def test_dev_login_does_not_require_csrf(self, client: TestClient, db_session: Session) -> None:
        """The dev-login backdoor is mounted only in test/dev. CSRF on a backdoor
        protects nothing — keep it exempt so Playwright's synthetic form post
        still works without first round-tripping for a token."""
        resp = client.post(
            "/auth/_dev-login",
            data={"email": "x@example.com", "sub": "g-x"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_oauth_callback_does_not_require_csrf(
        self, client: TestClient, monkeypatch: object
    ) -> None:
        """Google initiates the callback redirect; a CSRF token can't ride along.
        We don't actually exercise OAuth here — only verify the middleware lets
        the request reach the handler (which then 503s because OAuth is not
        configured in the test env)."""
        resp = client.get("/auth/google/callback", follow_redirects=False)
        # Reaches the handler (status 503 = "Google SSO is not configured").
        # The relevant assertion is "not 403 from CSRF".
        assert resp.status_code != 403


class TestCSRFTokenInTemplates:
    def test_admin_users_form_renders_csrf_field(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _make_user(db_session, email="other@x.test", role=Role.WORKSHOP)
        client.post(
            "/auth/_dev-login",
            data={"email": admin.email, "sub": admin.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/admin/users")
        assert resp.status_code == 200
        token = client.cookies["csrftoken"]
        # The token in the cookie matches the one rendered in the form.
        assert f'name="csrf_token" value="{token}"' in resp.text

    def test_signout_form_renders_csrf_field(self, client: TestClient, db_session: Session) -> None:
        user = _make_user(db_session, email="signed@x.test", role=Role.OFFICE)
        client.post(
            "/auth/_dev-login",
            data={"email": user.email, "sub": user.google_sub},
            follow_redirects=False,
        )
        resp = client.get("/")
        assert resp.status_code == 200
        token = client.cookies["csrftoken"]
        assert 'action="/auth/logout"' in resp.text
        assert f'name="csrf_token" value="{token}"' in resp.text


class TestCSRFOnNonFormBody:
    def test_post_with_json_body_and_no_token_is_403(self, client: TestClient) -> None:
        """JSON callers must use the X-CSRF-Token header — no token, 403."""
        client.get("/")  # bootstrap cookie
        resp = client.post(
            "/auth/logout",
            json={"some": "payload"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
