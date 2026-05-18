"""Items-form helpers for SIDE_TABLE-backed catalog fields (spec §9).

The catalog dispatcher (``app.field_storage.read_catalog_value``) handles
the read path. This module owns the write path — taking the raw form data
from an items POST and turning it into upserts / deletes on the per-item
side tables (``item_ring_attrs``, ``item_engagement_attrs``, …).

Responsibilities:

- ``extract_side_table_payloads``: read a request's form data, pick out
  values for the catalog entries the leaf has picked that point at side
  tables, coerce them per ``FieldType``, return ``{side_table:
  {column: value, ...}, ...}``.
- ``apply_side_table_payloads``: for each side-table payload, upsert the
  per-item side row when any value is non-NULL, delete it when every
  value is NULL.
- ``side_table_form_values_for_item``: read existing side-row values for
  an item and return the stringified form-input shape the template needs
  for input echo.

Validation: ``DECIMAL`` rejects negatives only for derived "amount" fields
where it'd be unambiguous — here we accept any decimal because the
spec's side-table footprint is geometry (band width, drop length, …)
where zero is meaningful and negatives are never sensible *but* the
correct posture is "the route layer narrows what each catalog entry
accepts," not "decimal coercion blanket-rejects negatives." The
``HTTPException`` shape mirrors ``app.items._parse_decimal`` so error
flows feel uniform across both write paths.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.orm import Session, object_session

from app.field_catalog import CATALOG_BY_KEY, CatalogEntry, Storage
from app.field_storage import _side_model_for, get_side_row
from app.models import FieldType, Item


def _coerce_value(entry: CatalogEntry, raw: str) -> Any:
    """Coerce a form string to the catalog entry's column type, or ``None``.

    Blank strings always return ``None`` so a missing input or a cleared
    field is recorded as NULL — that's the trigger for
    ``apply_side_table_payloads`` to delete the side row when every value
    is empty.

    Raises ``HTTPException(400)`` on a non-blank value that can't be
    coerced. The error detail names the field so the operator can tell
    which input bounced. Same error surface as ``app.items._parse_decimal``.
    """

    cleaned = (raw or "").strip()
    if cleaned == "":
        return None

    field_label = entry.label

    if entry.type is FieldType.BOOLEAN:
        # Match the same wire vocabulary as the items-list filter:
        # yes/true/1 → True; no/false/0 → False; everything else 400.
        lowered = cleaned.lower()
        if lowered in ("yes", "true", "1", "on"):
            return True
        if lowered in ("no", "false", "0", "off"):
            return False
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_label} must be yes / no",
        )

    if entry.type is FieldType.NUMBER:
        try:
            return int(cleaned)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be a whole number",
            ) from exc

    if entry.type is FieldType.DECIMAL:
        try:
            return Decimal(cleaned)
        except InvalidOperation as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be a number",
            ) from exc

    if entry.type is FieldType.DATE:
        try:
            return datetime.fromisoformat(cleaned).date()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be an ISO date (YYYY-MM-DD)",
            ) from exc

    if entry.type is FieldType.SELECT:
        if cleaned not in entry.options:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be one of: {', '.join(entry.options)}",
            )
        return cleaned

    if entry.type is FieldType.MULTISELECT:
        # Match the existing items convention: pipe-delimited round-trip
        # (see ``field_storage.format_for_csv``). Empty already returned
        # above so cleaned is non-empty here.
        values = [v.strip() for v in cleaned.split("|") if v.strip()]
        unknown = [v for v in values if v not in entry.options]
        if unknown:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"{field_label}: unknown option(s) {', '.join(unknown)}; "
                    f"valid: {', '.join(entry.options)}"
                ),
            )
        return values

    # FieldType.TEXT and any future text-shaped type.
    return cleaned


def _picked_side_table_entries(picked_keys: set[str]) -> list[CatalogEntry]:
    """Return the catalog entries picked by ``picked_keys`` that target side tables.

    Filters ``FIELD_CATALOG`` down to ``Storage.SIDE_TABLE`` entries whose
    ``key`` is in ``picked_keys`` — the set surfaced by
    ``app.items._picked_built_in_keys`` for the item's leaf node.
    """

    entries: list[CatalogEntry] = []
    for key in picked_keys:
        entry = CATALOG_BY_KEY.get(key)
        if entry is None or entry.storage is not Storage.SIDE_TABLE:
            continue
        entries.append(entry)
    return entries


def extract_side_table_payloads(
    form_data: dict[str, str] | Any,
    picked_keys: set[str],
) -> dict[str, dict[str, Any]]:
    """Pull side-table values out of ``form_data`` for the picked catalog keys.

    Returns ``{side_table_name: {column_name: coerced_value | None}}``.
    Each entry in ``picked_keys`` that is a ``Storage.SIDE_TABLE`` catalog
    entry contributes one column to its side table's payload. Blank /
    missing values become ``None`` so the writer can decide whether the
    whole side row should be deleted.

    ``form_data`` is a ``starlette.datastructures.FormData`` (or any
    mapping returned by ``await request.form()``) — we only need
    ``.get(key) -> str | None``, so a plain dict works in tests.

    Raises ``HTTPException(400)`` on a type-coercion failure. The error
    surfaces the user-facing field label and the route layer wraps it in
    its re-render flow.
    """

    payloads: dict[str, dict[str, Any]] = {}
    for entry in _picked_side_table_entries(picked_keys):
        # The __post_init__ invariant guarantees side_table + side_column
        # are non-None for SIDE_TABLE entries.
        assert entry.side_table is not None
        assert entry.side_column is not None
        raw = form_data.get(entry.key, "")
        if raw is None:
            raw = ""
        # ``starlette.UploadFile`` would be a programming error in this
        # context (operators don't upload files into a text input); coerce
        # to string defensively rather than crash on .strip().
        if not isinstance(raw, str):
            raw = str(raw)
        coerced = _coerce_value(entry, raw)
        payloads.setdefault(entry.side_table, {})[entry.side_column] = coerced
    return payloads


def _all_empty(payload: dict[str, Any]) -> bool:
    """Return True iff every value in the payload is ``None``.

    Used by the writer to decide whether to upsert or delete the side row.
    A side table where every picked field comes back blank is a "no
    information" state; deleting the row keeps the table free of zombies.
    """
    return all(v is None for v in payload.values())


def apply_side_table_payloads(
    db: Session,
    item: Item,
    payloads: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Upsert / delete the side rows implied by ``payloads``.

    Returns a diff-shaped dict ``{side_table: {column: new_value}}`` for
    rows that actually changed (created, updated columns, or deleted).
    Callers feed this into the audit-log ``after`` payload so the
    item.update audit row carries the side-table changes alongside the
    column changes. The diff is computed against the *prior* side-row
    state (or ``{}`` if the row didn't exist) so the writer is a no-op
    when the operator didn't touch any side-table field.

    Stays within the caller's transaction; never calls commit.
    """

    diff: dict[str, dict[str, Any]] = {}

    for side_table, columns in payloads.items():
        cls = _side_model_for(side_table)
        if cls is None:
            # Defensive: a catalog entry naming an unregistered side table
            # is an author error caught at startup by the catalog tests.
            # Skip rather than crash here.
            continue

        existing = get_side_row(item, side_table)

        if _all_empty(columns):
            # No information from the form. Delete any existing row so the
            # next read returns None (rather than a zombie row of NULLs).
            if existing is not None:
                deleted_before = {
                    col: getattr(existing, col, None) for col in columns
                }
                # Only record the diff for columns that were previously
                # non-NULL — a delete that drops all-NULL state isn't a
                # meaningful audit event.
                if any(v is not None for v in deleted_before.values()):
                    diff[side_table] = {col: None for col in columns}
                db.delete(existing)
            continue

        if existing is None:
            # Insert a fresh row. We must pass item_id explicitly since
            # the FK is the PK and the ORM won't auto-fill it.
            kwargs: dict[str, Any] = {"item_id": item.id}
            for col, value in columns.items():
                kwargs[col] = value
            db.add(cls(**kwargs))
            db.flush()
            # Every non-None column on a new row is a change.
            change = {col: value for col, value in columns.items() if value is not None}
            if change:
                diff[side_table] = change
            continue

        # Update existing row column-by-column. Track only the columns
        # whose value actually changed (so a no-op POST writes nothing).
        per_table_change: dict[str, Any] = {}
        for col, value in columns.items():
            prior = getattr(existing, col, None)
            if prior != value:
                setattr(existing, col, value)
                per_table_change[col] = value
        if per_table_change:
            diff[side_table] = per_table_change

    # Force the ORM unit-of-work to emit the pending changes so a downstream
    # ``read_catalog_value`` in the same transaction sees them — matches
    # how ``items.py`` already calls ``db.flush()`` after the Item write.
    db.flush()
    return diff


def side_table_form_values_for_item(
    item: Item,
    picked_keys: set[str],
) -> dict[str, str]:
    """Return ``{catalog_key: stringified_value}`` for echo back into the form.

    Used by the items-form re-render path: when the user opens an existing
    item or when a POST 400s, the template needs the current value of each
    side-table-backed input to pre-populate it. Missing side rows + NULL
    columns both echo as ``""``. Booleans echo as ``"true"`` / ``""``
    matching the existing checkbox convention.
    """

    out: dict[str, str] = {}
    for entry in _picked_side_table_entries(picked_keys):
        assert entry.side_table is not None
        assert entry.side_column is not None
        side = get_side_row(item, entry.side_table)
        if side is None:
            out[entry.key] = ""
            continue
        value = getattr(side, entry.side_column, None)
        if value is None:
            out[entry.key] = ""
        elif entry.type is FieldType.BOOLEAN:
            out[entry.key] = "true" if value else ""
        elif entry.type is FieldType.MULTISELECT and isinstance(value, list):
            out[entry.key] = "|".join(str(v) for v in value)
        elif isinstance(value, (date, datetime)):
            out[entry.key] = value.isoformat()
        else:
            out[entry.key] = str(value)
    return out


__all__ = [
    "apply_side_table_payloads",
    "extract_side_table_payloads",
    "side_table_form_values_for_item",
]


# ``object_session`` is re-exported here so callers can quickly check
# whether an item is bound to a session before invoking the helpers;
# keeps the module's public surface self-contained.
_object_session = object_session
