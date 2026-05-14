"""Read + format helpers for catalog-backed item fields.

Every catalog entry is column-backed (post-0024). This module owns the read
+ display/CSV formatter used by the items list view; per-row writes happen
directly on the ``Item`` model in ``app/items.py``.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.field_catalog import CatalogEntry
from app.models import FieldType, Item


def read_catalog_value(item: Item, entry: CatalogEntry) -> Any:
    """Read the value for a catalog entry off the item's column."""
    return getattr(item, entry.column, None)


def format_for_display(entry: CatalogEntry, value: Any) -> str:
    """Render a catalog value as a string for the items-list table cell.

    Empty / missing values render as ``""``. Booleans render as ``"yes"`` /
    ``"no"`` for spreadsheet readability. Lists (multiselect) render as a
    comma-joined string. Dates and datetimes render as ISO.
    """
    if value is None or value == "":
        return ""
    if entry.type is FieldType.BOOLEAN:
        return "yes" if value else "no"
    if entry.type is FieldType.MULTISELECT and isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def format_for_csv(entry: CatalogEntry, value: Any) -> Any:
    """Render a catalog value for a CSV cell.

    Matches existing CSV conventions in ``app/csv_export.py``: ``None`` →
    empty string, booleans as ``"yes"`` / ``"no"``, decimals / dates
    pre-coerced to strings, multiselect lists joined with ``"|"`` so
    cell-level CSV parsing stays clean.
    """
    if value is None or value == "":
        return ""
    if entry.type is FieldType.BOOLEAN:
        return "yes" if value else "no"
    if entry.type is FieldType.MULTISELECT and isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value
