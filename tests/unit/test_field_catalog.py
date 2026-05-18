"""Unit tests for ``app.field_catalog``.

The catalog is a closed, hardcoded list — these tests are forcing functions
that keep it internally consistent and aligned with the ORM models that
hold its values. Catalog entries come in two storage modes (post-S4):

- ``ITEM_COLUMN`` (legacy / default): the entry's ``column`` is a real
  column on the ``Item`` model.
- ``SIDE_TABLE`` (spec §9): the entry's ``side_table`` + ``side_column``
  resolve to a real column on a registered side-table model.

Both flavours go through the same invariant checks; the column-existence
check dispatches on storage.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import RelationshipProperty
from sqlalchemy.orm.attributes import InstrumentedAttribute

from app.db import Base
from app.field_catalog import (
    CATALOG_BY_KEY,
    FIELD_CATALOG,
    CatalogEntry,
    Storage,
    get_entry,
)
from app.models import FieldType, Item


def _model_for_tablename(tablename: str) -> type | None:
    """Resolve an ORM class by ``__tablename__`` via ``Base.registry``."""
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__tablename__", None) == tablename:
            return cls
    return None


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

    # Storage-mode shape: ITEM_COLUMN entries need a column; SIDE_TABLE
    # entries need both side_table and side_column. The dataclass's
    # __post_init__ enforces these at import time but re-asserting here
    # gives a per-key failure message in the parametrised report.
    if entry.storage is Storage.ITEM_COLUMN:
        assert entry.column, f"{entry.key}: ITEM_COLUMN entry needs a column"
        assert entry.side_table is None
        assert entry.side_column is None
    else:
        assert entry.column is None
        assert entry.side_table, f"{entry.key}: SIDE_TABLE entry needs side_table"
        assert entry.side_column, f"{entry.key}: SIDE_TABLE entry needs side_column"


@pytest.mark.parametrize(
    "entry",
    list(FIELD_CATALOG),
    ids=lambda e: e.key,
)
def test_entries_reference_real_columns(entry: CatalogEntry) -> None:
    """Every entry's column must resolve to a real mapper attribute.

    ITEM_COLUMN entries must point at a column on ``Item``. SIDE_TABLE
    entries must point at a column on the named side-table model. Either
    way the resolved attribute must be a mapped column, not a
    relationship or hybrid.
    """

    if entry.storage is Storage.ITEM_COLUMN:
        model: type = Item
        column_name = entry.column
    else:
        assert entry.side_table is not None
        model_or_none = _model_for_tablename(entry.side_table)
        assert model_or_none is not None, (
            f"{entry.key}: no model with __tablename__={entry.side_table!r}"
        )
        model = model_or_none
        column_name = entry.side_column

    assert column_name is not None
    attr = getattr(model, column_name, None)
    assert attr is not None, (
        f"{entry.key}: {model.__name__} has no attribute {column_name!r}"
    )
    assert isinstance(attr, InstrumentedAttribute), (
        f"{entry.key}: {model.__name__}.{column_name} is not a mapped column"
    )
    prop = attr.property
    assert not isinstance(prop, RelationshipProperty), (
        f"{entry.key}: {model.__name__}.{column_name} is a relationship, not a column"
    )


def test_tracking_mode_options_match_enum() -> None:
    """The catalog's ``tracking_mode`` options must match the ``TrackingMode``
    StrEnum verbatim — otherwise the form will submit values the model
    rejects."""

    from app.models import TrackingMode

    entry = CATALOG_BY_KEY["tracking_mode"]
    assert tuple(m.value for m in TrackingMode) == entry.options


class TestCatalogEntryConstruction:
    """The dataclass ``__post_init__`` rejects malformed entries."""

    def test_item_column_requires_column(self) -> None:
        with pytest.raises(ValueError, match="ITEM_COLUMN entries require a column"):
            CatalogEntry(key="bad", label="bad", type=FieldType.TEXT)

    def test_item_column_rejects_side_table_fields(self) -> None:
        with pytest.raises(ValueError, match="must leave side_table"):
            CatalogEntry(
                key="bad",
                label="bad",
                type=FieldType.TEXT,
                column="name",
                side_table="item_ring_attrs",
                side_column="ring_size",
            )

    def test_side_table_requires_both_pointers(self) -> None:
        with pytest.raises(ValueError, match="require both side_table and side_column"):
            CatalogEntry(
                key="bad",
                label="bad",
                type=FieldType.DECIMAL,
                storage=Storage.SIDE_TABLE,
                side_table="item_ring_attrs",
            )

    def test_side_table_rejects_column(self) -> None:
        with pytest.raises(ValueError, match="must leave column unset"):
            CatalogEntry(
                key="bad",
                label="bad",
                type=FieldType.DECIMAL,
                column="anything",
                storage=Storage.SIDE_TABLE,
                side_table="item_ring_attrs",
                side_column="ring_size",
            )
