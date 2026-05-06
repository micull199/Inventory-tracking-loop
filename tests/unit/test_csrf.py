"""Unit tests for the CSRF utility helpers."""

from __future__ import annotations

from app.csrf import _extract_submitted_token, _new_token


class TestNewToken:
    def test_token_is_unique_each_call(self) -> None:
        assert _new_token() != _new_token()

    def test_token_is_url_safe_and_long_enough(self) -> None:
        token = _new_token()
        # URL-safe chars only (no padding equals at the end of token_urlsafe(32)
        # because 32 bytes → 43 chars with no '=').
        assert all(c.isalnum() or c in "-_" for c in token)
        assert len(token) >= 32


class TestExtractSubmittedToken:
    def test_header_takes_precedence_over_form(self) -> None:
        headers = {
            b"x-csrf-token": b"header-tok",
            b"content-type": b"application/x-www-form-urlencoded",
        }
        body = b"csrf_token=form-tok"
        assert _extract_submitted_token(headers, body) == "header-tok"

    def test_form_field_extracted_when_no_header(self) -> None:
        headers = {b"content-type": b"application/x-www-form-urlencoded"}
        body = b"foo=bar&csrf_token=form-tok&baz=qux"
        assert _extract_submitted_token(headers, body) == "form-tok"

    def test_returns_none_when_no_form_field_present(self) -> None:
        headers = {b"content-type": b"application/x-www-form-urlencoded"}
        body = b"foo=bar"
        assert _extract_submitted_token(headers, body) is None

    def test_returns_none_for_json_body(self) -> None:
        headers = {b"content-type": b"application/json"}
        body = b'{"csrf_token": "ignored"}'
        # JSON callers must use the header — body is not parsed.
        assert _extract_submitted_token(headers, body) is None

    def test_returns_none_for_multipart_body(self) -> None:
        headers = {
            b"content-type": b"multipart/form-data; boundary=---abc"
        }
        body = b"---abc\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\ntok\r\n---abc--"
        # Multipart bodies aren't inspected; callers send the header.
        assert _extract_submitted_token(headers, body) is None

    def test_handles_empty_body(self) -> None:
        headers = {b"content-type": b"application/x-www-form-urlencoded"}
        assert _extract_submitted_token(headers, b"") is None
