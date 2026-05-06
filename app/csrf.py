"""CSRF protection — double-submit-cookie pattern.

Why double-submit cookie (not session-stored token)
---------------------------------------------------
The session cookie is encrypted/signed and not directly readable by JS or
tests; storing the CSRF token there would force every caller (HTMX, Playwright,
API consumer) to first parse a rendered page to discover the token. The
double-submit pattern instead puts the token in a *separate* readable cookie
plus the form body / a request header. The browser's same-origin guarantee +
``SameSite=Lax`` ensures only same-site code can read the cookie, so a CSRF
attacker on another origin can't replay it.

Threat model
------------
We block: cross-site form posts, cross-site fetch/XHR with credentials, image
GETs that mutate (we only mutate on non-safe methods).
We do not block: an attacker who already has same-origin XSS — at that point
they can read the cookie too. XSS prevention is a separate concern (output
escaping in templates, no innerHTML from user input).

Implementation notes
--------------------
- Built as raw ASGI middleware so we can buffer the request body, parse
  ``application/x-www-form-urlencoded`` for the token field, then *replay*
  the body to the downstream app. ``BaseHTTPMiddleware`` would let us do this
  more ergonomically but has well-known performance and exception-handling
  caveats; the raw-ASGI form is small enough to keep here.
- Multipart bodies are out of scope until we have an upload route; today
  multipart POSTs would fail CSRF unless they set the header — that's a
  forcing function rather than a hidden hazard.
- Exempt set is hard-coded and small. New exemptions require touching this
  file (and a code review): no per-route opt-out via a decorator, on purpose.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from typing import cast
from urllib.parse import parse_qs

from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSRF_COOKIE_NAME = "csrftoken"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER_NAME = "x-csrf-token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# Exempt by exact path. Keep tiny.
DEFAULT_EXEMPT_PATHS = frozenset(
    {
        "/auth/google/callback",  # provider-initiated; can't carry a token
        "/auth/_dev-login",  # test/dev backdoor; CSRF on a backdoor protects nothing
    }
)


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def _extract_submitted_token(headers: dict[bytes, bytes], body: bytes) -> str | None:
    """Pull the submitted token from header or form body.

    Preference order: ``X-CSRF-Token`` header (HTMX / fetch), then
    ``csrf_token`` form field. JSON bodies and multipart bodies are not
    inspected — those callers must use the header.
    """
    header_value = headers.get(b"x-csrf-token")
    if header_value:
        return header_value.decode("latin-1")

    content_type = headers.get(b"content-type", b"").decode("latin-1").lower()
    if "application/x-www-form-urlencoded" in content_type:
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        parsed = parse_qs(decoded, keep_blank_values=True)
        values = parsed.get(CSRF_FORM_FIELD, [])
        return values[0] if values else None

    return None


def _make_replay_receive(body: bytes) -> Callable[[], Awaitable[Message]]:
    """Build a one-shot ``receive`` that emits the buffered body once."""
    sent = False

    async def receive() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


class CSRFMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        cookie_name: str = CSRF_COOKIE_NAME,
        exempt_paths: frozenset[str] = DEFAULT_EXEMPT_PATHS,
        secure: bool = False,
    ) -> None:
        self.app = app
        self.cookie_name = cookie_name
        self.exempt_paths = exempt_paths
        self.secure = secure

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        method = request.method
        path = request.url.path
        existing_cookie = request.cookies.get(self.cookie_name)

        # Decide which token to expose to the handler / template, and whether
        # the response needs to set the cookie.
        if existing_cookie:
            active_token = existing_cookie
            need_set_cookie = False
        else:
            active_token = _new_token()
            need_set_cookie = True

        # Make the active token visible to handlers + the template context
        # processor without a second cookie read.
        scope.setdefault("state", {})
        cast(dict[str, object], scope["state"])["csrf_token"] = active_token

        if method in SAFE_METHODS or path in self.exempt_paths:
            await self._call_with_cookie(
                scope, receive, send, active_token, need_set_cookie
            )
            return

        # Mutating, non-exempt request: validate before forwarding.
        body = await self._read_body(receive)
        headers_dict = {k: v for k, v in scope.get("headers", [])}
        submitted = _extract_submitted_token(headers_dict, body)

        valid = bool(
            existing_cookie
            and submitted
            and secrets.compare_digest(existing_cookie, submitted)
        )
        if not valid:
            response = Response(
                "CSRF token missing or invalid",
                status_code=403,
                media_type="text/plain",
            )
            await response(scope, receive, send)
            return

        replay_receive = _make_replay_receive(body)
        await self._call_with_cookie(
            scope, replay_receive, send, active_token, need_set_cookie
        )

    @staticmethod
    async def _read_body(receive: Receive) -> bytes:
        body = b""
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # http.disconnect or anything else: stop reading.
                break
            body += message.get("body", b"") or b""
            if not message.get("more_body", False):
                break
        return body

    async def _call_with_cookie(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        token: str,
        need_set: bool,
    ) -> None:
        if not need_set:
            await self.app(scope, receive, send)
            return

        cookie_value = self._build_cookie(token)

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", cookie_value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_wrapper)

    def _build_cookie(self, token: str) -> str:
        # Non-HttpOnly so HTMX / fetch can read it for the X-CSRF-Token header.
        # SameSite=Lax keeps cross-site form-posts from sending it.
        parts = [
            f"{self.cookie_name}={token}",
            "Path=/",
            "SameSite=Lax",
        ]
        if self.secure:
            parts.append("Secure")
        return "; ".join(parts)


def csrf_context_processor(request: Request) -> dict[str, str]:
    """Jinja2Templates context processor: makes ``csrf_token`` always available.

    Reads from ``request.state.csrf_token`` (set by :class:`CSRFMiddleware`),
    falling back to an empty string if the middleware isn't installed (which
    would only happen in tests that bypass the app).
    """
    token = getattr(request.state, "csrf_token", "")
    return {"csrf_token": token}
