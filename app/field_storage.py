"""Read + format helpers for catalog-backed item fields.

Catalog entries come in two storage modes (see ``app.field_catalog``):

- ``ITEM_COLUMN`` (default / legacy): value lives on the ``items`` row.
- ``SIDE_TABLE`` (S4 / spec §9): value lives on a per-item side row
  (``item_ring_attrs``, ``item_engagement_attrs``, ...) joined by
  ``item_id``. Side rows are nullable everywhere; a missing row reads as
  ``None`` for every field on that side table.

``read_catalog_value`` dispatches on ``entry.storage``. The items-list
templates + CSV export consume this single accessor so they don't have to
know which storage mode any given field uses.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.field_catalog import CatalogEntry, Storage
from app.models import FieldType, Item

# Map side-table model ``__tablename__`` → the ORM class. Filled lazily on
# first read so the import cycle stays simple (models can grow without
# touching this module). The lookup is a single hash hit; the cost of
# rebuilding the map on every miss is negligible compared to the DB read.
_SIDE_MODEL_CACHE: dict[str, type[Any]] = {}


def _side_model_for(tablename: str) -> type[Any] | None:
    """Resolve a side-table's ORM class by ``__tablename__``.

    Uses ``Base.registry`` to iterate every registered mapper rather than
    hardcoding a switch — adding a new side table doesn't require touching
    this module. Returns ``None`` (rather than raising) if the table is
    unknown, so a malformed catalog entry yields a clean ``None`` read
    instead of a 500 on the items-list page.
    """
    if tablename in _SIDE_MODEL_CACHE:
        return _SIDE_MODEL_CACHE[tablename]
    from app.db import Base

    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__tablename__", None) == tablename:
            _SIDE_MODEL_CACHE[tablename] = cls
            return cls
    return None


def get_side_row(item: Item, side_table: str) -> Any | None:
    """Return the side-table row joined to ``item``, or ``None`` if absent.

    Uses the active session bound to ``item`` (so the lookup respects the
    test's savepoint isolation). Each side table's PK *is* ``item_id`` —
    a primary-key lookup is the cheapest possible read and avoids both
    a relationship configuration on ``Item`` and an extra round-trip
    compared to a ``select().where(...)`` query.
    """
    from sqlalchemy.orm import object_session

    cls = _side_model_for(side_table)
    if cls is None:
        return None
    session = object_session(item)
    if session is None:
        return None
    return session.get(cls, item.id)


def read_catalog_value(item: Item, entry: CatalogEntry) -> Any:
    """Read the value for a catalog entry, dispatching on ``entry.storage``.

    Returns ``None`` for any field that isn't populated — either because
    the side row doesn't exist yet (SIDE_TABLE) or because the column
    itself is NULL (both modes). Side-table reads are guarded by a single
    PK lookup; no cross-row joins.
    """
    if entry.storage is Storage.SIDE_TABLE:
        # The __post_init__ invariant guarantees both side_table and
        # side_column are non-None here; the asserts are belt-and-braces.
        assert entry.side_table is not None
        assert entry.side_column is not None
        side = get_side_row(item, entry.side_table)
        if side is None:
            return None
        return getattr(side, entry.side_column, None)
    # Storage.ITEM_COLUMN. The __post_init__ invariant guarantees column
    # is non-None; the assert keeps the type-narrowing explicit.
    assert entry.column is not None
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
