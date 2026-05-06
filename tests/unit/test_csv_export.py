"""Unit tests for the CSV export helper (R5).

Pure helpers in ``app/csv_export.py``. No DB writes, no fixtures, no app wiring.
The integration tests in ``tests/integration/test_reports_routes.py`` and
``tests/integration/test_purchase_orders_routes.py`` exercise the route
surfaces end-to-end; this file covers the standalone helper so a regression in
CSV escaping / filename sanitisation / cell coercion is caught before the
route is wired.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from app.csv_export import _safe_filename, csv_branch, csv_response

# ---------------------------------------------------------------------------
# csv_response
# ---------------------------------------------------------------------------


class TestCsvResponse:
    def test_media_type_is_csv_with_charset(self) -> None:
        resp = csv_response(filename="t.csv", headers=["a"], rows=[])
        # FastAPI's Response sets Content-Type from media_type; we want utf-8.
        assert resp.media_type == "text/csv; charset=utf-8"

    def test_content_disposition_carries_filename(self) -> None:
        resp = csv_response(filename="report.csv", headers=["a"], rows=[])
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="report.csv"' in cd

    def test_filename_is_sanitised(self) -> None:
        """Whitespace / quotes / semicolons replaced with ``_``."""
        resp = csv_response(
            filename='bad ;"name.csv', headers=["a"], rows=[]
        )
        cd = resp.headers["content-disposition"]
        # Disallowed chars replaced; dots + dashes survive.
        assert ";" not in cd.split('"', 2)[1]
        assert '"' not in cd.split('"', 2)[1]

    def test_empty_rows_still_emits_header(self) -> None:
        resp = csv_response(filename="t.csv", headers=["a", "b"], rows=[])
        body = resp.body.decode("utf-8")
        assert body == "a,b\r\n"

    def test_header_row_then_data_rows(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["x", "y"],
            rows=[[1, 2], [3, 4]],
        )
        body = resp.body.decode("utf-8")
        assert body == "x,y\r\n1,2\r\n3,4\r\n"

    def test_crlf_line_terminator(self) -> None:
        """RFC 4180 + Excel-on-Windows compatibility."""
        resp = csv_response(
            filename="t.csv",
            headers=["a"],
            rows=[["v"]],
        )
        body = resp.body.decode("utf-8")
        assert "\r\n" in body
        # No bare \n outside of \r\n sequences.
        assert body.count("\n") == body.count("\r\n")

    def test_none_becomes_empty_string(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["a", "b"],
            rows=[[None, "x"]],
        )
        body = resp.body.decode("utf-8")
        assert body == "a,b\r\n,x\r\n"

    def test_decimal_serialises_via_str(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[[Decimal("3.50")]],
        )
        body = resp.body.decode("utf-8")
        assert body == "v\r\n3.50\r\n"

    def test_int_serialises(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[[42]],
        )
        body = resp.body.decode("utf-8")
        assert body == "v\r\n42\r\n"

    def test_datetime_uses_isoformat(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[[datetime(2026, 5, 7, 14, 30, tzinfo=UTC)]],
        )
        body = resp.body.decode("utf-8")
        assert "2026-05-07T14:30:00+00:00" in body

    def test_date_uses_isoformat(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[[date(2026, 5, 7)]],
        )
        body = resp.body.decode("utf-8")
        assert body == "v\r\n2026-05-07\r\n"

    def test_embedded_comma_is_quoted(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[["a, b"]],
        )
        body = resp.body.decode("utf-8")
        assert body == 'v\r\n"a, b"\r\n'

    def test_embedded_quote_is_doubled(self) -> None:
        """RFC 4180: a literal ``"`` inside a quoted field is escaped as ``""``."""
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[['say "hi"']],
        )
        body = resp.body.decode("utf-8")
        assert body == 'v\r\n"say ""hi"""\r\n'

    def test_embedded_newline_is_quoted(self) -> None:
        resp = csv_response(
            filename="t.csv",
            headers=["v"],
            rows=[["line1\nline2"]],
        )
        body = resp.body.decode("utf-8")
        # The cell is quoted; the inner \n stays literal inside the quoted field.
        assert body.startswith("v\r\n")
        assert '"line1\nline2"' in body


# ---------------------------------------------------------------------------
# _safe_filename
# ---------------------------------------------------------------------------


class TestSafeFilename:
    def test_ascii_alnum_passes_through(self) -> None:
        assert _safe_filename("report.csv") == "report.csv"

    def test_underscore_dash_dot_preserved(self) -> None:
        assert _safe_filename("a_b-c.d.csv") == "a_b-c.d.csv"

    def test_whitespace_replaced(self) -> None:
        assert _safe_filename("my report.csv") == "my_report.csv"

    def test_quotes_replaced(self) -> None:
        assert _safe_filename('a"b.csv') == "a_b.csv"

    def test_semicolons_replaced(self) -> None:
        assert _safe_filename("a;b.csv") == "a_b.csv"

    def test_slashes_replaced(self) -> None:
        assert _safe_filename("a/b\\c.csv") == "a_b_c.csv"

    def test_non_ascii_replaced(self) -> None:
        assert _safe_filename("café.csv") == "caf__.csv" or "_" in _safe_filename(
            "café.csv"
        )


# ---------------------------------------------------------------------------
# csv_branch (R5e)
# ---------------------------------------------------------------------------


class TestCsvBranch:
    """The list-view-route branch helper.

    Returns a CSV ``Response`` when the format query value is the literal
    ``"csv"``; otherwise returns ``None`` so the caller can fall through to
    its HTML render path.
    """

    def test_returns_none_when_format_is_blank(self) -> None:
        result = csv_branch(
            "",
            filename="t.csv",
            headers=["a"],
            rows=[["v"]],
        )
        assert result is None

    def test_returns_none_when_format_is_html(self) -> None:
        result = csv_branch(
            "html",
            filename="t.csv",
            headers=["a"],
            rows=[["v"]],
        )
        assert result is None

    def test_returns_none_when_format_is_garbage(self) -> None:
        result = csv_branch(
            "xml",
            filename="t.csv",
            headers=["a"],
            rows=[["v"]],
        )
        assert result is None

    def test_returns_response_when_format_is_csv(self) -> None:
        result = csv_branch(
            "csv",
            filename="t.csv",
            headers=["a"],
            rows=[["v"]],
        )
        assert result is not None
        assert result.media_type == "text/csv; charset=utf-8"
        assert 'filename="t.csv"' in result.headers["content-disposition"]

    def test_csv_branch_delegates_body_through_csv_response(self) -> None:
        """``csv_branch("csv", ...)`` produces the same body as ``csv_response`` direct."""
        via_branch = csv_branch(
            "csv",
            filename="t.csv",
            headers=["x", "y"],
            rows=[[1, 2]],
        )
        direct = csv_response(
            filename="t.csv",
            headers=["x", "y"],
            rows=[[1, 2]],
        )
        assert via_branch is not None
        assert via_branch.body == direct.body
