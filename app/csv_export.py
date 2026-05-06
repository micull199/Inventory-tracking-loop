"""CSV export helper (R5 — MISSION §3 "Export any list view to CSV").

A small, dependency-free helper that turns ``(headers, rows)`` into a
FastAPI ``Response`` with the right ``Content-Type`` and
``Content-Disposition`` for a downloadable CSV. The first cut lights up two
surfaces — the variance-trend report (``app/reports.py``) and the PO list
(``app/purchase_orders.py``); subsequent slices can extend the same pattern
to other list views without changing this module.

Design notes:

- Uses the stdlib ``csv`` module via an ``io.StringIO`` buffer. The default
  ``csv.writer`` dialect emits ``QUOTE_MINIMAL`` quoting and ``\\r\\n``
  line terminators, which is RFC 4180 + Excel-on-Windows compatible.
- Cell coercion is uniform across callers: ``None`` → ``""``; ``Decimal`` /
  ``int`` / ``float`` / ``bool`` → ``str(value)``; ``datetime`` / ``date``
  → ``isoformat()``; everything else → ``str(value)``. Callers that want a
  different shape (e.g. a "yes" / "no" cell instead of "True" / "False")
  should pre-coerce before passing the row in.
- Filename is sanitised — the ``Content-Disposition`` header has its own
  quoting rules and a stray ``"`` or ``;`` would break it. ``_safe_filename``
  replaces non-``[A-Za-z0-9_.-]`` chars with ``_``.
- The body is utf-8 encoded; the ``Content-Type`` carries the charset
  explicitly so a downstream consumer doesn't guess.

CSV-injection note: cell values aren't sanitised against spreadsheet formula
injection (e.g. a cell starting with ``=cmd|...``). v1 risk model is all-
internal users (Workshop is server-side blocked from these routes); the
mitigation can land later if the threat model changes.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from fastapi import Response

_FILENAME_SAFE_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789_-."
)


def _safe_filename(name: str) -> str:
    """Replace non-``[A-Za-z0-9_.-]`` chars with ``_``.

    Keeps the filename safe to embed in a ``Content-Disposition`` header
    without further escaping. Dots and dashes are preserved so ``.csv``
    extensions and standard separators survive.
    """
    return "".join(ch if ch in _FILENAME_SAFE_CHARS else "_" for ch in name)


def _coerce_cell(value: Any) -> str:
    """Coerce a single cell to its CSV string form."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def csv_response(
    *,
    filename: str,
    headers: list[str],
    rows: Iterable[Iterable[Any]],
) -> Response:
    """Build a downloadable CSV response.

    The body always starts with a header row, even when ``rows`` is empty —
    a header-only CSV is still a useful "we ran the report and got nothing"
    artefact for the receiver.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow(_coerce_cell(cell) for cell in row)
    body = buffer.getvalue()

    safe_name = _safe_filename(filename)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
        },
    )
