"""Unit tests for the shared CSV import helper.

Pure helpers in ``app/csv_import.py``. No DB writes, no fixtures, no app
wiring. The per-domain integration tests exercise the route surfaces
end-to-end; this file pins the transport-layer behaviour (decoding, size
caps, header checks, duplicate detection) in isolation.
"""

from __future__ import annotations

import pytest

from app.csv_import import (
    MAX_DATA_ROWS,
    MAX_UPLOAD_BYTES,
    CsvUploadError,
    RowResult,
    check_required_and_known_headers,
    mark_intra_file_duplicates,
    read_upload,
    row_to_dict,
    sha256_hex,
)


class TestReadUpload:
    def test_reads_headers_and_body(self) -> None:
        data = b"name,email\nAlice,a@x.test\nBob,b@x.test\n"
        sha, headers, body = read_upload(data)
        assert headers == ["name", "email"]
        assert body == [["Alice", "a@x.test"], ["Bob", "b@x.test"]]
        assert sha == sha256_hex(data)

    def test_trims_header_whitespace(self) -> None:
        data = b" name , email \nA,b\n"
        _sha, headers, _body = read_upload(data)
        assert headers == ["name", "email"]

    def test_drops_trailing_blank_rows(self) -> None:
        data = b"name\nA\n\n\n"
        _sha, _h, body = read_upload(data)
        assert body == [["A"]]

    def test_rejects_non_utf8(self) -> None:
        # 0xFF is not valid UTF-8 anywhere.
        data = b"name\n\xff\n"
        with pytest.raises(CsvUploadError, match="UTF-8"):
            read_upload(data)

    def test_accepts_utf8_bom(self) -> None:
        # Excel often saves UTF-8 with a BOM; should still parse cleanly.
        data = "﻿name\nAlice\n".encode()
        _sha, headers, _body = read_upload(data)
        assert headers == ["name"]

    def test_rejects_empty_file(self) -> None:
        with pytest.raises(CsvUploadError, match="empty"):
            read_upload(b"")

    def test_rejects_too_large_file(self) -> None:
        big = b"a\n" + b"x\n" * (MAX_UPLOAD_BYTES // 2)
        with pytest.raises(CsvUploadError, match="too large"):
            read_upload(big)

    def test_rejects_too_many_rows(self) -> None:
        rows = "\n".join("x" for _ in range(MAX_DATA_ROWS + 1))
        data = ("name\n" + rows + "\n").encode("utf-8")
        with pytest.raises(CsvUploadError, match="too many"):
            read_upload(data)

    def test_sha256_is_stable(self) -> None:
        data = b"name\nAlice\n"
        s1, _h, _b = read_upload(data)
        s2, _h, _b = read_upload(data)
        assert s1 == s2
        # Tiny edit changes the hash.
        s3, _h, _b = read_upload(b"name\nBob\n")
        assert s3 != s1


class TestCheckHeaders:
    def test_passes_on_known_required(self) -> None:
        check_required_and_known_headers(
            ["id", "name", "email"],
            known={"id", "name", "email"},
            required={"name"},
        )

    def test_unknown_column_raises(self) -> None:
        with pytest.raises(CsvUploadError, match="unknown column"):
            check_required_and_known_headers(
                ["id", "name", "bogus"],
                known={"id", "name"},
                required=set(),
            )

    def test_missing_required_raises(self) -> None:
        with pytest.raises(CsvUploadError, match="missing required"):
            check_required_and_known_headers(
                ["id"],
                known={"id", "name"},
                required={"name"},
            )

    def test_duplicate_columns_raise(self) -> None:
        with pytest.raises(CsvUploadError, match="duplicate"):
            check_required_and_known_headers(
                ["id", "name", "name"],
                known={"id", "name"},
                required=set(),
            )

    def test_extra_predicate_allows_dynamic_columns(self) -> None:
        check_required_and_known_headers(
            ["id", "cf_metal", "cf_weight"],
            known={"id"},
            required=set(),
            extra_predicate=lambda h: h.startswith("cf_"),
        )

    def test_extra_predicate_does_not_excuse_unknown(self) -> None:
        with pytest.raises(CsvUploadError):
            check_required_and_known_headers(
                ["id", "bogus"],
                known={"id"},
                required=set(),
                extra_predicate=lambda h: h.startswith("cf_"),
            )


class TestRowToDict:
    def test_aligns_by_position(self) -> None:
        d = row_to_dict(["a", "b", "c"], ["1", "2", "3"])
        assert d == {"a": "1", "b": "2", "c": "3"}

    def test_pads_short_row_with_blanks(self) -> None:
        d = row_to_dict(["a", "b", "c"], ["1"])
        assert d == {"a": "1", "b": "", "c": ""}

    def test_drops_extra_cells(self) -> None:
        d = row_to_dict(["a"], ["1", "2", "3"])
        assert d == {"a": "1"}


class TestMarkDuplicates:
    def test_flags_both_dupe_rows_as_errors(self) -> None:
        rows = [
            RowResult(row_number=2, raw={"sku": "ABC"}, tag="new", payload={}),
            RowResult(row_number=3, raw={"sku": "XYZ"}, tag="new", payload={}),
            RowResult(row_number=4, raw={"sku": "ABC"}, tag="new", payload={}),
        ]
        mark_intra_file_duplicates(rows, key="sku")
        assert rows[0].tag == "error"
        assert rows[1].tag == "new"
        assert rows[2].tag == "error"
        # Each error message names the *other* row(s).
        assert "4" in rows[0].error_message
        assert "2" in rows[2].error_message

    def test_skips_already_errored_rows(self) -> None:
        rows = [
            RowResult(row_number=2, raw={"name": "A"}, tag="error"),
            RowResult(row_number=3, raw={"name": "A"}, tag="new", payload={}),
        ]
        mark_intra_file_duplicates(rows, key="name")
        # The second row's "A" doesn't see the first (errored) row as a dupe.
        assert rows[1].tag == "new"

    def test_ignores_blank_keys(self) -> None:
        rows = [
            RowResult(row_number=2, raw={"name": ""}, tag="new", payload={}),
            RowResult(row_number=3, raw={"name": ""}, tag="new", payload={}),
        ]
        mark_intra_file_duplicates(rows, key="name")
        assert all(r.tag == "new" for r in rows)

    def test_case_insensitive_mode(self) -> None:
        rows = [
            RowResult(row_number=2, raw={"name": "Alice"}, tag="new", payload={}),
            RowResult(row_number=3, raw={"name": "ALICE"}, tag="new", payload={}),
        ]
        mark_intra_file_duplicates(rows, key="name", case_insensitive=True)
        assert rows[0].tag == "error"
        assert rows[1].tag == "error"


class TestNumbersIntCellNormalisation:
    """Numbers stores Python ints as floats (``1`` -> ``1.0``). The CSV
    conversion must collapse int-valued floats back to integer strings so
    the downstream ``int()`` parser on id columns doesn't blow up.
    """

    def test_int_valued_float_renders_as_int_string(self) -> None:
        import pathlib
        import tempfile

        numbers_parser = pytest.importorskip("numbers_parser")

        from app.csv_import import _numbers_to_csv_bytes

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "t.numbers"
            doc = numbers_parser.Document()
            table = doc.sheets[0].tables[0]
            table.write(0, 0, "id")
            table.write(0, 1, "qty")
            table.write(1, 0, 42)  # int -> float in Numbers
            table.write(1, 1, 2.5)  # genuine float
            doc.save(str(path))
            data = path.read_bytes()

        csv = _numbers_to_csv_bytes(data).decode("utf-8")
        lines = [ln for ln in csv.splitlines() if ln]
        assert lines[0] == "id,qty"
        # ``42`` round-trips as ``"42"`` (int-collapsed); ``2.5`` stays as ``"2.5"``.
        assert lines[1] == "42,2.5", lines[1]
