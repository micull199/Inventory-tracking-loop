"""Integration tests for app/oauth_test_stub.py.

The stub router is only mounted in the live app when ``OAUTH_STUB_MODE=1``.
In the default integration-test env (``OAUTH_STUB_MODE`` unset) the routes
don't appear in ``app.main.app``'s route table, so the audit-coverage sweep
never sees them.

These tests create a minimal FastAPI test app that includes the stub router and
patches ``settings.oauth_stub_mode = True`` (``APP_ENV=test`` is already set by
the integration conftest), then exercise each endpoint directly.

A separate test class asserts the guard behaviour: if either flag is off the
stub returns 404.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import oauth_test_stub as stub_module
from app.oauth_test_stub import STUB_USER, stub_router

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """Return a TestClient for a mini app with the stub router and stub mode on."""
    monkeypatch.setattr(stub_module.settings, "oauth_stub_mode", True)
    mini = FastAPI()
    mini.include_router(stub_router)
    return TestClient(mini, follow_redirects=False)


# ---------------------------------------------------------------------------
# Constant: stub user shape
# ---------------------------------------------------------------------------


class TestStubUserConstant:
    def test_stub_user_has_sub(self) -> None:
        assert "sub" in STUB_USER

    def test_stub_user_has_email(self) -> None:
        assert "email" in STUB_USER

    def test_stub_user_has_name(self) -> None:
        assert "name" in STUB_USER

    def test_stub_user_email_is_string(self) -> None:
        assert isinstance(STUB_USER["email"], str)
        assert "@" in STUB_USER["email"]


# ---------------------------------------------------------------------------
# Guard: stub not allowed when oauth_stub_mode is off
# ---------------------------------------------------------------------------


class TestStubGuard:
    """When oauth_stub_mode=False the endpoints return 404 regardless of app_env."""

    def _guarded_client(self, monkeypatch: pytest.MonkeyPatch) -> TestClient:
        monkeypatch.setattr(stub_module.settings, "oauth_stub_mode", False)
        mini = FastAPI()
        mini.include_router(stub_router)
        return TestClient(mini, follow_redirects=False)

    def test_authorize_returns_404_when_stub_disabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = self._guarded_client(monkeypatch)
        resp = client.get("/auth/_stub/authorize?redirect_uri=/cb&state=abc")
        assert resp.status_code == 404

    def test_token_returns_404_when_stub_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._guarded_client(monkeypatch)
        resp = client.post("/auth/_stub/token")
        assert resp.status_code == 404

    def test_userinfo_returns_404_when_stub_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = self._guarded_client(monkeypatch)
        resp = client.get("/auth/_stub/userinfo")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Authorize endpoint
# ---------------------------------------------------------------------------


class TestStubAuthorize:
    def test_authorize_returns_redirect(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/auth/_stub/authorize?redirect_uri=http://app/cb&state=abc123")
        assert resp.status_code == 302

    def test_authorize_echoes_state_in_location(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/auth/_stub/authorize?redirect_uri=http://app/cb&state=test-state")
        location = resp.headers["location"]
        assert "state=test-state" in location

    def test_authorize_includes_stub_code_in_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/auth/_stub/authorize?redirect_uri=http://app/cb&state=s")
        location = resp.headers["location"]
        assert "code=stub-auth-code" in location

    def test_authorize_redirects_to_redirect_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get(
            "/auth/_stub/authorize?redirect_uri=http://app/auth/google/callback&state=s"
        )
        location = resp.headers["location"]
        assert location.startswith("http://app/auth/google/callback")

    def test_authorize_handles_missing_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/auth/_stub/authorize?redirect_uri=http://app/cb")
        assert resp.status_code == 302
        assert "state=" in resp.headers["location"]


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------


class TestStubToken:
    def test_token_returns_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.post("/auth/_stub/token")
        assert resp.status_code == 200

    def test_token_returns_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.post("/auth/_stub/token")
        assert resp.headers["content-type"].startswith("application/json")

    def test_token_response_has_access_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        data = client.post("/auth/_stub/token").json()
        assert "access_token" in data
        assert data["access_token"] == "stub-access-token"  # noqa: S105

    def test_token_response_has_token_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        data = client.post("/auth/_stub/token").json()
        assert data.get("token_type") == "Bearer"

    def test_token_response_has_no_id_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """No id_token means Authlib won't attempt JWT verification."""
        client = _make_client(monkeypatch)
        data = client.post("/auth/_stub/token").json()
        assert "id_token" not in data


# ---------------------------------------------------------------------------
# Userinfo endpoint
# ---------------------------------------------------------------------------


class TestStubUserinfo:
    def test_userinfo_returns_200(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/auth/_stub/userinfo")
        assert resp.status_code == 200

    def test_userinfo_returns_json(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        resp = client.get("/auth/_stub/userinfo")
        assert resp.headers["content-type"].startswith("application/json")

    def test_userinfo_returns_stub_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        data = client.get("/auth/_stub/userinfo").json()
        assert data["sub"] == STUB_USER["sub"]
        assert data["email"] == STUB_USER["email"]
        assert data["name"] == STUB_USER["name"]

    def test_userinfo_sub_matches_stub_user_constant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        client = _make_client(monkeypatch)
        data = client.get("/auth/_stub/userinfo").json()
        assert data == dict(STUB_USER)
