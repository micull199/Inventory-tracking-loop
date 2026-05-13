"""Encapsulates how field values are read from / written to storage.

Today a field value is always a sparse row in ``item_field_values`` — one row
per ``(item, field_def)`` pair, with the value living in the type-specific
column (``value_text``, ``value_number``, …). Slice 6 of the catalog-driven
refactor introduces a second storage target: values that live directly on a
column of the ``items`` table (e.g. the catalog entry for ``unit`` writes to
``items.unit``). This module is where that dispatch will land.

For slice 2 the public surface is intentionally narrow: it owns the
type → column mapping and the read / insert / update primitives that the
``app.items`` orchestration helpers call into. Slice 6 will extend the API
with ``column``-storage variants without renaming anything.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.field_catalog import CatalogEntry
from app.models import FieldType, Item, ItemFieldValue, TaxonomyFieldDef

# Stable map from a field def's type to the ``ItemFieldValue`` column that
# stores its value. ``SELECT`` keeps its chosen option as plain text;
# ``MULTISELECT`` keeps a list of chosen options in JSON.
VALUE_COLUMN_BY_TYPE: Final[dict[FieldType, str]] = {
    FieldType.TEXT: "value_text",
    FieldType.NUMBER: "value_number",
    FieldType.DECIMAL: "value_decimal",
    FieldType.DATE: "value_date",
    FieldType.BOOLEAN: "value_bool",
    FieldType.SELECT: "value_text",
    FieldType.MULTISELECT: "value_json",
}


def value_column_for(field_type: FieldType) -> str:
    """Return the ``ItemFieldValue.value_*`` column name for ``field_type``."""

    return VALUE_COLUMN_BY_TYPE[field_type]


def read_stored_value(field_def: TaxonomyFieldDef, row: ItemFieldValue) -> Any:
    """Return the populated value off a stored ``ItemFieldValue`` row.

    Caller is responsible for ensuring ``row`` belongs to ``field_def``
    (i.e. ``row.field_def_id == field_def.id``).
    """

    return getattr(row, VALUE_COLUMN_BY_TYPE[field_def.type])


def write_new_value(
    db: Session,
    *,
    item_id: int,
    field_def: TaxonomyFieldDef,
    value: Any,
) -> ItemFieldValue:
    """Create + add a new ``ItemFieldValue`` row carrying ``value``.

    The row is added to the session but not flushed — callers usually flush
    later as part of a larger commit so the audit-log row joins the same
    transaction. The new row is returned so callers can stash it (e.g. for
    audit diffing) without an extra round-trip.
    """

    ifv = ItemFieldValue(item_id=item_id, field_def_id=field_def.id)
    setattr(ifv, VALUE_COLUMN_BY_TYPE[field_def.type], value)
    db.add(ifv)
    return ifv


def update_existing_value(
    field_def: TaxonomyFieldDef,
    row: ItemFieldValue,
    value: Any,
) -> None:
    """Overwrite the populated value column of an existing row in place.

    No-ops on the other ``value_*`` columns — they should already be NULL
    (one and only one column is populated per row, by type).
    """

    setattr(row, VALUE_COLUMN_BY_TYPE[field_def.type], value)


def load_rows_for_item(db: Session, item_id: int) -> dict[int, ItemFieldValue]:
    """Return existing ``ItemFieldValue`` rows for an item, keyed by ``field_def_id``."""

    stmt = select(ItemFieldValue).where(ItemFieldValue.item_id == item_id)
    return {row.field_def_id: row for row in db.execute(stmt).scalars().all()}


def read_catalog_value(
    item: Item,
    entry: CatalogEntry,
    *,
    field_def: TaxonomyFieldDef | None = None,
    ifv_by_key: dict[str, ItemFieldValue] | None = None,
) -> Any:
    """Read the value for a catalog entry off the right storage target.

    ``column``-storage entries read straight off the ``Item``. ``field_value``-
    storage entries look up the row in ``ifv_by_key`` (keyed by the def's
    ``key``, not its id, so the same lookup table works across catalog and
    legacy rows). Returns ``None`` when there is no value.

    ``ifv_by_key`` is callable's responsibility — typically derived from
    ``load_rows_for_item`` followed by a re-key on ``field_def.key``.
    """

    if entry.storage == "column":
        assert entry.column is not None  # invariant enforced by the catalog tests
        return getattr(item, entry.column, None)
    # field_value storage — needs both the def (for type dispatch) and the
    # cached row.
    if field_def is None or ifv_by_key is None:
        return None
    row = ifv_by_key.get(field_def.key)
    if row is None:
        return None
    return read_stored_value(field_def, row)


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
