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

from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import FieldType, ItemFieldValue, TaxonomyFieldDef

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
