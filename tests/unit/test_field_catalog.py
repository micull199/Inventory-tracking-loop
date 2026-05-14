"""Unit tests for ``app.field_catalog``.

The catalog is a closed, hardcoded list — these tests are forcing functions
that keep it internally consistent and aligned with the ORM model that
holds its values. Every catalog entry maps to a column on ``Item`` post-0024.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.field_catalog import (
    CATALOG_BY_KEY,
    FIELD_CATALOG,
    CatalogEntry,
    get_entry,
)
from app.models import FieldType, Item


def test_catalog_is_non_empty() -> None:
    assert len(FIELD_CATALOG) > 0


def test_keys_are_unique() -> None:
    keys = [e.key for e in FIELD_CATALOG]
    assert len(keys) == len(set(keys)), "duplicate catalog keys"


def test_catalog_by_key_round_trip() -> None:
    for entry in FIELD_CATALOG:
        assert CATALOG_BY_KEY[entry.key] is entry
        assert get_entry(entry.key) is entry
    assert get_entry("definitely-not-a-real-key") is None


@pytest.mark.parametrize("entry", FIELD_CATALOG, ids=lambda e: e.key)
def test_entry_invariants(entry: CatalogEntry) -> None:
    # Keys are short, lowercase, snake-cased.
    assert entry.key, f"empty key on {entry!r}"
    assert entry.key == entry.key.strip().lower()
    assert all(c.isalnum() or c == "_" for c in entry.key)
    assert len(entry.key) <= 64

    # Labels are non-empty.
    assert entry.label.strip(), f"empty label on {entry!r}"

    # SELECT / MULTISELECT must carry options; other types must not.
    if entry.type in (FieldType.SELECT, FieldType.MULTISELECT):
        assert entry.options, f"{entry.key}: select/multiselect needs options"
        assert len(entry.options) == len(set(entry.options)), f"{entry.key}: duplicate options"
    else:
        assert not entry.options, f"{entry.key}: non-select must not carry options"

    # Every entry references a real Item column.
    assert entry.column, f"{entry.key}: every entry needs a column name"


@pytest.mark.parametrize(
    "entry",
    list(FIELD_CATALOG),
    ids=lambda e: e.key,
)
def test_entries_reference_real_item_columns(entry: CatalogEntry) -> None:
    """Every entry's ``column`` must be a real ``Item`` mapper attribute
    (not a relationship or hybrid)."""

    attr = getattr(Item, entry.column, None)
    assert attr is not None, f"{entry.key}: Item has no attribute {entry.column!r}"
    assert isinstance(attr, InstrumentedAttribute), (
        f"{entry.key}: Item.{entry.column} is not a mapped column"
    )
    prop = attr.property
    assert not isinstance(prop, RelationshipProperty), (
        f"{entry.key}: Item.{entry.column} is a relationship, not a column"
    )


def test_tracking_mode_options_match_enum() -> None:
    """The catalog's ``tracking_mode`` options must match the ``TrackingMode``
    StrEnum verbatim — otherwise the form will submit values the model
    rejects."""

    from app.models import TrackingMode

    entry = CATALOG_BY_KEY["tracking_mode"]
    assert tuple(m.value for m in TrackingMode) == entry.options
