"""CSV import helper — shared parse/validate machinery for the four upload
domains (items, suppliers, locations, taxonomy). Mirror of ``csv_export``.

Design:

- This module owns *transport-layer* concerns: decoding the upload to UTF-8,
  enforcing the 5 MB / 5000-row caps, splitting the CSV into a header row +
  body, and hashing the bytes (so the per-upload summary audit row carries a
  ``file_sha256`` — a re-upload of the same file is identifiable).
- Each domain owns its own row-validation + creator logic. The route calls
  ``read_upload`` to get ``(sha, headers, body)`` and feeds them to its
  per-domain validator; the validator returns a list of ``RowResult`` rows
  tagged ``new`` / ``skip`` / ``error``. The route renders the preview or
  commits.
- The route boilerplate is tiny; the heavy lifting is in the parsers.

CSV-injection note: cell values are *not* sanitised against spreadsheet
formula injection on the way *in*. Same posture as ``csv_export`` (v1 risk
model is all-internal users).
"""

from __future__ import annotations

import csv
import hashlib
import io
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Caps from the spec ("File size + safety"). Configurable later if needed.
MAX_UPLOAD_BYTES: int = 5 * 1024 * 1024  # 5 MB
MAX_DATA_ROWS: int = 5_000

# Numbers (.numbers) files are zip archives. The standard zip magic number
# is ``PK\x03\x04``. Detection by *both* extension and magic bytes catches
# files renamed to ``.csv`` *and* files dragged in without the extension.
_ZIP_MAGIC: bytes = b"PK\x03\x04"


class CsvUploadError(Exception):
    """The upload as a whole is unprocessable.

    Raised for transport-level failures (file too big, non-UTF-8, header
    mismatch, empty file). Per-row validation failures are *not* an
    ``CsvUploadError`` — they're captured as ``RowResult(tag="error")`` so
    the preview can render them in context.
    """


# ---------------------------------------------------------------------------
# Per-row result
# ---------------------------------------------------------------------------


@dataclass
class RowResult:
    """Per-row outcome from a validation pass.

    ``row_number`` is 1-based and counts the header (so the first data row is
    row 2). This matches what a spreadsheet user sees in Excel/Sheets.

    ``tag`` is one of:

    - ``"new"`` — would be created on commit; ``payload`` carries the
      validated row dict.
    - ``"skip"`` — already exists (id-based idempotent re-upload); no write.
    - ``"error"`` — validation failed; ``error_field`` / ``error_message``
      explain why; commit is blocked.

    ``warnings`` accumulates non-blocking notes (e.g. "sku column ignored on
    create"). Renders alongside the tag in the preview.
    """

    row_number: int
    raw: dict[str, str]
    tag: str
    error_field: str = ""
    error_message: str = ""
    payload: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Bytes → headers + body
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _decode_utf8(data: bytes) -> str:
    if len(data) > MAX_UPLOAD_BYTES:
        raise CsvUploadError(
            f"file is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
        )
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CsvUploadError("file is not valid UTF-8") from exc


def _parse_csv(text: str) -> tuple[list[str], list[list[str]]]:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise CsvUploadError("file is empty (no header row)")
    headers = [h.strip() for h in rows[0]]
    body = list(rows[1:])
    # Strip *trailing* all-blank rows ("\n" at EOF is common from spreadsheets).
    # In-the-middle blank rows are kept — they're a user mistake and surface as
    # per-row validation errors rather than being silently dropped.
    while body and (not body[-1] or all((c or "").strip() == "" for c in body[-1])):
        body.pop()
    if len(body) > MAX_DATA_ROWS:
        raise CsvUploadError(f"too many data rows (max {MAX_DATA_ROWS})")
    return headers, body


def _is_numbers_upload(data: bytes, filename: str | None) -> bool:
    """True iff ``data`` looks like an Apple Numbers (``.numbers``) file.

    Numbers files are zip archives, so they always start with the standard
    ``PK\\x03\\x04`` magic. To avoid mis-classifying every zip (e.g. an
    ``.xlsx``) as Numbers, we *also* require the upload to be flagged as
    ``.numbers`` by extension. Users renaming a foreign zip to ``.numbers``
    would still flow through to ``numbers-parser`` and surface a clean
    ``CsvUploadError`` there.
    """
    if not data.startswith(_ZIP_MAGIC):
        return False
    if filename is None:
        return False
    return filename.lower().endswith(".numbers")


def _numbers_to_csv_bytes(data: bytes) -> bytes:
    """Convert a Numbers file's first sheet/table to CSV-encoded bytes.

    ``numbers-parser`` takes a file path on disk, not bytes — so the upload
    is materialised into a temp file for the duration of the parse and
    deleted on exit. Cells:

    - ``None`` → empty string.
    - ``int`` / ``float`` / ``Decimal`` / ``date`` / ``datetime`` /
      everything-else → ``str(...)`` (no special formatting; matches the
      Numbers-display value).
    - Trailing all-empty *columns* in the header row are dropped (the
      default Numbers table is 12x8 with mostly empty cells; we don't want
      eight phantom columns showing up as "unknown column" errors).
    - Trailing all-empty rows are dropped; in-the-middle blanks are kept
      and surface as per-row validation errors.

    Only the **first sheet's first table** is read. Multi-sheet uploads are
    not supported in v1 — same posture as the CSV path (one table per file).
    """
    try:
        from numbers_parser import Document  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover — dep is in pyproject
        raise CsvUploadError(
            "Numbers file support is not installed on this server "
            "(missing dependency: numbers-parser)"
        ) from exc

    if len(data) > MAX_UPLOAD_BYTES:
        raise CsvUploadError(
            f"file is too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)"
        )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "upload.numbers"
        path.write_bytes(data)
        try:
            doc = Document(str(path))
        except Exception as exc:
            raise CsvUploadError(
                f"could not read Numbers file ({type(exc).__name__})"
            ) from exc
        if not doc.sheets:
            raise CsvUploadError("Numbers file has no sheets")
        sheet = doc.sheets[0]
        if not sheet.tables:
            raise CsvUploadError("Numbers file's first sheet has no tables")
        table = sheet.tables[0]

        raw_rows: list[tuple[Any, ...]] = list(table.iter_rows(values_only=True))

    if not raw_rows:
        raise CsvUploadError("Numbers table is empty (no header row)")

    header_row = list(raw_rows[0])
    # Drop trailing empty columns based on the header row — the default
    # Numbers table is padded with None on the right. ``None``/``""`` cells
    # at the right edge are treated as "no column defined".
    last_used = -1
    for i, cell in enumerate(header_row):
        if cell is not None and str(cell).strip() != "":
            last_used = i
    if last_used < 0:
        raise CsvUploadError("Numbers table has no header column")
    col_count = last_used + 1

    def _cell(v: Any) -> str:
        # Numbers stores numeric-looking cells as Python floats — even when
        # the user (or a CSV download) wrote them as ints. ``str(1.0)`` ==
        # ``"1.0"`` would then fail ``int()`` parsing in id columns and
        # silently truncate trailing zeros elsewhere. When a float has no
        # fractional part, collapse it to the integer form so the CSV
        # downstream sees the same string a native CSV download would emit.
        if v is None:
            return ""
        if isinstance(v, float):
            import math

            if math.isfinite(v) and v.is_integer():
                return str(int(v))
        return str(v)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([_cell(c) for c in header_row[:col_count]])
    for row in raw_rows[1:]:
        cells = [_cell(c) for c in list(row)[:col_count]]
        # Pad short rows (defensive; numbers-parser returns uniform tuples).
        while len(cells) < col_count:
            cells.append("")
        writer.writerow(cells)
    return buf.getvalue().encode("utf-8")


def read_upload(
    data: bytes, *, filename: str | None = None
) -> tuple[str, list[str], list[list[str]]]:
    """Decode bytes → ``(file_sha256, headers, body_rows)``.

    ``filename`` (when supplied by the route from ``UploadFile.filename``)
    enables Apple Numbers (``.numbers``) dispatch: a Numbers file is
    converted in-memory to CSV bytes via ``numbers-parser`` and then fed
    through the same parser as a native CSV. The returned ``file_sha256``
    always hashes the *raw upload* (not the converted intermediate) so a
    re-upload of the same Numbers file is identifiable in the audit log.

    ``body_rows`` excludes blank trailing rows. Each row's cell count is
    *not* normalised here — the per-domain validator decides whether
    "header has N cols and this row has N-1" is an error or fine (e.g. a
    spreadsheet that drops trailing empty cells).
    """
    sha = sha256_hex(data)
    csv_bytes = (
        _numbers_to_csv_bytes(data) if _is_numbers_upload(data, filename) else data
    )
    text = _decode_utf8(csv_bytes)
    headers, body = _parse_csv(text)
    return sha, headers, body


# ---------------------------------------------------------------------------
# Header validation helpers (used by per-domain validators)
# ---------------------------------------------------------------------------


def check_required_and_known_headers(
    actual: list[str],
    *,
    known: set[str],
    required: set[str],
    extra_predicate: object = None,
) -> None:
    """Raise ``CsvUploadError`` on unknown or missing-required headers.

    ``known`` enumerates the always-recognised column names. ``required`` is
    the subset that *must* appear. ``extra_predicate`` (optional) is a
    callable ``(name) -> bool`` that returns True for additionally-accepted
    columns (e.g. ``cf_<key>`` for items). When supplied, columns matching it
    bypass the unknown check.

    Duplicate column names (case-sensitive) are themselves an error — a
    spreadsheet round-trip should never produce them, and reading the file
    becomes ambiguous if they do.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for h in actual:
        if h in seen:
            duplicates.append(h)
        seen.add(h)
    if duplicates:
        raise CsvUploadError(f"duplicate column(s) in header: {', '.join(sorted(set(duplicates)))}")
    unknown: list[str] = []
    for h in actual:
        if h in known:
            continue
        if extra_predicate is not None and bool(extra_predicate(h)):  # type: ignore[operator]
            continue
        unknown.append(h)
    if unknown:
        raise CsvUploadError(f"unknown column(s): {', '.join(unknown)}")
    missing = required - set(actual)
    if missing:
        raise CsvUploadError(f"missing required column(s): {', '.join(sorted(missing))}")


# ---------------------------------------------------------------------------
# Row → dict helpers
# ---------------------------------------------------------------------------


def row_to_dict(headers: list[str], row: list[str]) -> dict[str, str]:
    """Build a ``{header: cell}`` dict from a positional row.

    Trailing cells beyond the header count are dropped (a permissive read).
    Missing trailing cells are filled with ``""`` — a spreadsheet that
    rstrips empty cells should still upload cleanly.
    """
    out: dict[str, str] = {}
    for i, h in enumerate(headers):
        out[h] = row[i] if i < len(row) else ""
    return out


# ---------------------------------------------------------------------------
# Cross-row duplicate detection
# ---------------------------------------------------------------------------


def mark_intra_file_duplicates(
    results: list[RowResult],
    *,
    key: str,
    case_insensitive: bool = False,
) -> None:
    """Mark rows that share the same ``key`` value as errors *both* ways.

    Skips rows already tagged ``error`` (their key may be invalid) and rows
    where the ``key`` cell is blank. Cross-row duplicate detection only fires
    among rows that would otherwise be ``new`` — a re-upload of an existing
    row that resolves to ``skip`` is fine.

    Both rows get an error tag with a message that names the *other* row, so
    the user can correlate them in the preview.
    """
    by_key: dict[str, list[RowResult]] = {}
    for r in results:
        if r.tag != "new":
            continue
        raw_value = r.raw.get(key, "")
        if not raw_value.strip():
            continue
        k = raw_value.strip()
        if case_insensitive:
            k = k.lower()
        by_key.setdefault(k, []).append(r)
    for matches in by_key.values():
        if len(matches) <= 1:
            continue
        row_numbers = [m.row_number for m in matches]
        for m in matches:
            others = [str(n) for n in row_numbers if n != m.row_number]
            m.tag = "error"
            m.error_field = key
            m.error_message = (
                f"duplicate {key} inside this file (also on row "
                f"{', '.join(others)})"
            )
            m.payload = None
