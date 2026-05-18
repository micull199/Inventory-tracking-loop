"""Unit tests for ``app.field_storage`` — the catalog read-path dispatcher.

Two storage modes:

- ``ITEM_COLUMN``: reads ``getattr(item, entry.column)``.
- ``SIDE_TABLE``: looks up the side row via ``get_side_row(item, side_table)``
  (a PK lookup on ``item_id``) and reads the named column off it, returning
  ``None`` when the side row is absent.

The dispatcher is what lets the items-list + CSV templates consume
``read_catalog_value(item, entry)`` without caring which storage mode any
given field uses.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.field_catalog import CATALOG_BY_KEY, CatalogEntry, Storage
from app.field_storage import get_side_row, read_catalog_value
from app.models import (
    Archetype,
    FieldType,
    Item,
    ItemRingAttrs,
    TaxonomyNode,
    TrackingMode,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _make_item(db: Session, name: str = "Test ring") -> Item:
    node = TaxonomyNode(name=name, sku_prefix="TR", archetype=Archetype.UNIQUE)
    db.add(node)
    db.commit()
    db.refresh(node)
    item = Item(
        sku=f"TR-{name}",
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
        ring_size="6.5",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestReadItemColumn:
    def test_returns_column_value(self, db: Session) -> None:
        item = _make_item(db)
        entry = CATALOG_BY_KEY["ring_size"]
        assert read_catalog_value(item, entry) == "6.5"

    def test_returns_none_when_column_null(self, db: Session) -> None:
        item = _make_item(db)
        entry = CATALOG_BY_KEY["qr_code"]
        assert read_catalog_value(item, entry) is None


class TestReadSideTable:
    def test_returns_none_when_side_row_absent(self, db: Session) -> None:
        item = _make_item(db)
        entry = CATALOG_BY_KEY["band_width_mm"]
        # No item_ring_attrs row exists yet — must read as None, not error.
        assert read_catalog_value(item, entry) is None

    def test_returns_side_column_value(self, db: Session) -> None:
        item = _make_item(db)
        db.add(
            ItemRingAttrs(item_id=item.id, band_width_mm=Decimal("2.50"))
        )
        db.commit()
        entry = CATALOG_BY_KEY["band_width_mm"]
        assert read_catalog_value(item, entry) == Decimal("2.50")

    def test_returns_none_when_side_column_null(self, db: Session) -> None:
        item = _make_item(db)
        # Side row exists but the band_width_mm column on it is NULL.
        db.add(ItemRingAttrs(item_id=item.id))
        db.commit()
        entry = CATALOG_BY_KEY["band_width_mm"]
        assert read_catalog_value(item, entry) is None


class TestGetSideRow:
    def test_returns_none_when_absent(self, db: Session) -> None:
        item = _make_item(db)
        assert get_side_row(item, "item_ring_attrs") is None

    def test_returns_row_when_present(self, db: Session) -> None:
        item = _make_item(db)
        side = ItemRingAttrs(item_id=item.id, band_width_mm=Decimal("3.00"))
        db.add(side)
        db.commit()
        fetched = get_side_row(item, "item_ring_attrs")
        assert fetched is not None
        assert fetched.band_width_mm == Decimal("3.00")

    def test_returns_none_for_unknown_table(self, db: Session) -> None:
        item = _make_item(db)
        # Defensive: a malformed catalog entry naming a non-existent side
        # table must yield None, not a 500.
        assert get_side_row(item, "definitely_not_a_real_table") is None


class TestStorageEnum:
    def test_existing_entries_default_to_item_column(self) -> None:
        # Every pre-S4 catalog entry must continue resolving as ITEM_COLUMN
        # to preserve the legacy read path.
        for key in ("name", "unit", "supplier_id", "ring_size", "metal_id"):
            entry: CatalogEntry = CATALOG_BY_KEY[key]
            assert entry.storage is Storage.ITEM_COLUMN, key

    def test_side_table_entries_have_side_pointers(self) -> None:
        entry = CATALOG_BY_KEY["band_width_mm"]
        assert entry.storage is Storage.SIDE_TABLE
        assert entry.side_table == "item_ring_attrs"
        assert entry.side_column == "band_width_mm"
        assert entry.column is None
        assert entry.type is FieldType.DECIMAL
