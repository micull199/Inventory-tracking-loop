"""OAuth test stub — fake Google OAuth 2.0 provider for Playwright e2e tests.

Mounted only when ``APP_ENV=test`` AND ``OAUTH_STUB_MODE=1``. Provides three
endpoints that mimic a minimal OAuth 2.0 / OIDC flow without any external
network calls or JWT signing:

``GET  /auth/_stub/authorize`` — echoes the ``state`` param and redirects the
browser back to ``redirect_uri?code=stub-auth-code&state={state}``.

``POST /auth/_stub/token`` — called server-side by Authlib during the callback.
Returns a minimal token response (no ``id_token``) so Authlib never attempts
JWT verification.  The missing ``userinfo`` key causes the callback handler's
fallback branch (``google.userinfo(token=token)``) to fire.

``GET  /auth/_stub/userinfo`` — returns the fixed stub user dict. Called by
the Authlib client via the ``userinfo_endpoint`` registered in stub mode.

Security: every handler checks that both ``app_env == "test"`` and
``oauth_stub_mode`` are set before responding; any other configuration gets a
404.  The router itself is only mounted in ``app/main.py`` when both flags are
active, so the paths don't even exist in prod.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse

from app.config import settings

# Fixed stub identity returned for every OAuth flow in a stub-enabled session.
STUB_USER: dict[str, str] = {
    "sub": "oauth-stub-sub-1",
    "email": "oauthstub@uc.example",
    "name": "OAuth Stub User",
}

stub_router = APIRouter(prefix="/auth/_stub", tags=["oauth-stub"])


def _check_stub_allowed() -> None:
    if settings.app_env != "test" or not settings.oauth_stub_mode:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


@stub_router.get("/authorize")
async def authorize(request: Request) -> RedirectResponse:
    """Redirect the browser straight back to redirect_uri with a stub code."""
    _check_stub_allowed()
    redirect_uri = request.query_params.get("redirect_uri", "/")
    state = request.query_params.get("state", "")
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        url=f"{redirect_uri}{sep}code=stub-auth-code&state={state}",
        status_code=302,
    )


@stub_router.post("/token")
async def token_endpoint(request: Request) -> JSONResponse:
    """Return a minimal token response; no id_token so no JWT verification."""
    _check_stub_allowed()
    return JSONResponse(
        {
            "access_token": "stub-access-token",
            "token_type": "Bearer",
        }
    )


@stub_router.get("/userinfo")
async def userinfo_endpoint() -> JSONResponse:
    """Return the fixed stub user dict."""
    _check_stub_allowed()
    return JSONResponse(STUB_USER)
