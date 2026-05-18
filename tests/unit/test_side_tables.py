"""Unit tests for ``app.side_tables``.

Covers the three helpers that turn raw form data into side-row upserts /
deletes for the catalog dispatcher's write path:

- ``extract_side_table_payloads``: form → ``{side_table: {col: coerced}}``,
  with per-FieldType coercion and 400 on bad input.
- ``apply_side_table_payloads``: payloads → upsert / delete, returning a
  diff suitable for the audit-log ``after`` shape.
- ``side_table_form_values_for_item``: existing row → stringified form
  shape for input echo.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.field_catalog import CatalogEntry, Storage
from app.models import (
    Archetype,
    FieldType,
    Item,
    ItemRingAttrs,
    TaxonomyNode,
    TrackingMode,
)
from app.side_tables import _coerce_value as coerce  # private but exercised
from app.side_tables import (
    apply_side_table_payloads,
    extract_side_table_payloads,
    side_table_form_values_for_item,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _make_item(db: Session, name: str = "ring") -> Item:
    node = TaxonomyNode(name=f"Cat-{name}", sku_prefix="CT", archetype=Archetype.UNIQUE)
    db.add(node)
    db.commit()
    db.refresh(node)
    item = Item(
        sku=f"CT-{name}",
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# A small set of ad-hoc catalog entries used in the coerce tests. These
# don't go through CATALOG_BY_KEY — we instantiate them inline so each
# FieldType branch in ``_coerce_value`` is exercised independently.
def _entry(field_type: FieldType, *, options: tuple[str, ...] = ()) -> CatalogEntry:
    return CatalogEntry(
        key="test_field",
        label="Test field",
        type=field_type,
        options=options,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="ring_size",
    )


class TestCoerceValue:
    def test_blank_returns_none(self) -> None:
        assert coerce(_entry(FieldType.TEXT), "") is None
        assert coerce(_entry(FieldType.TEXT), "   ") is None

    def test_text_strips_whitespace(self) -> None:
        assert coerce(_entry(FieldType.TEXT), "  hello  ") == "hello"

    def test_decimal_parses(self) -> None:
        assert coerce(_entry(FieldType.DECIMAL), "2.50") == Decimal("2.50")

    def test_decimal_rejects_garbage(self) -> None:
        with pytest.raises(HTTPException) as exc:
            coerce(_entry(FieldType.DECIMAL), "abc")
        assert exc.value.status_code == 400
        assert "Test field" in exc.value.detail

    def test_number_parses_int(self) -> None:
        assert coerce(_entry(FieldType.NUMBER), "42") == 42

    def test_number_rejects_decimal_string(self) -> None:
        with pytest.raises(HTTPException):
            coerce(_entry(FieldType.NUMBER), "1.5")

    def test_boolean_yes_no(self) -> None:
        e = _entry(FieldType.BOOLEAN)
        for truthy in ("yes", "true", "1", "on", "YES", "True"):
            assert coerce(e, truthy) is True
        for falsy in ("no", "false", "0", "off"):
            assert coerce(e, falsy) is False

    def test_boolean_rejects_unknown(self) -> None:
        with pytest.raises(HTTPException):
            coerce(_entry(FieldType.BOOLEAN), "maybe")

    def test_date_iso(self) -> None:
        from datetime import date

        assert coerce(_entry(FieldType.DATE), "2026-05-15") == date(2026, 5, 15)

    def test_date_rejects_bad(self) -> None:
        with pytest.raises(HTTPException):
            coerce(_entry(FieldType.DATE), "tomorrow")

    def test_select_accepts_known(self) -> None:
        e = _entry(FieldType.SELECT, options=("a", "b", "c"))
        assert coerce(e, "b") == "b"

    def test_select_rejects_unknown(self) -> None:
        e = _entry(FieldType.SELECT, options=("a", "b", "c"))
        with pytest.raises(HTTPException) as exc:
            coerce(e, "z")
        assert "a, b, c" in exc.value.detail

    def test_multiselect_pipe_split(self) -> None:
        e = _entry(FieldType.MULTISELECT, options=("a", "b", "c"))
        assert coerce(e, "a|b") == ["a", "b"]

    def test_multiselect_rejects_unknown(self) -> None:
        e = _entry(FieldType.MULTISELECT, options=("a", "b", "c"))
        with pytest.raises(HTTPException):
            coerce(e, "a|z")


class TestExtractPayloads:
    def test_only_picked_entries_extracted(self) -> None:
        # The catalog has ``band_width_mm`` as the lone SIDE_TABLE entry today.
        form = {"band_width_mm": "2.50", "ring_size": "6.5", "name": "Foo"}
        payloads = extract_side_table_payloads(form, picked_keys={"band_width_mm", "ring_size"})
        assert payloads == {"item_ring_attrs": {"band_width_mm": Decimal("2.50")}}

    def test_unpicked_side_entry_not_extracted(self) -> None:
        form = {"band_width_mm": "2.50"}
        # ring_size is picked but it's an ITEM_COLUMN entry — irrelevant.
        payloads = extract_side_table_payloads(form, picked_keys={"ring_size"})
        assert payloads == {}

    def test_blank_value_becomes_none(self) -> None:
        form = {"band_width_mm": ""}
        payloads = extract_side_table_payloads(form, picked_keys={"band_width_mm"})
        assert payloads == {"item_ring_attrs": {"band_width_mm": None}}

    def test_missing_key_treated_as_blank(self) -> None:
        # The picked entry isn't in the form at all (e.g. a hidden field).
        payloads = extract_side_table_payloads({}, picked_keys={"band_width_mm"})
        assert payloads == {"item_ring_attrs": {"band_width_mm": None}}

    def test_coercion_error_raises(self) -> None:
        with pytest.raises(HTTPException):
            extract_side_table_payloads(
                {"band_width_mm": "not-a-number"},
                picked_keys={"band_width_mm"},
            )


class TestApplyPayloads:
    def test_insert_new_side_row(self, db: Session) -> None:
        item = _make_item(db)
        diff = apply_side_table_payloads(
            db,
            item,
            {"item_ring_attrs": {"band_width_mm": Decimal("2.50")}},
        )
        db.commit()
        row = db.execute(select(ItemRingAttrs)).scalar_one()
        assert row.item_id == item.id
        assert row.band_width_mm == Decimal("2.50")
        assert diff == {"item_ring_attrs": {"band_width_mm": Decimal("2.50")}}

    def test_update_existing_side_row(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemRingAttrs(item_id=item.id, band_width_mm=Decimal("2.00")))
        db.commit()
        diff = apply_side_table_payloads(
            db,
            item,
            {"item_ring_attrs": {"band_width_mm": Decimal("3.00")}},
        )
        db.commit()
        row = db.execute(select(ItemRingAttrs)).scalar_one()
        assert row.band_width_mm == Decimal("3.00")
        assert diff == {"item_ring_attrs": {"band_width_mm": Decimal("3.00")}}

    def test_noop_when_value_unchanged(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemRingAttrs(item_id=item.id, band_width_mm=Decimal("2.00")))
        db.commit()
        diff = apply_side_table_payloads(
            db,
            item,
            {"item_ring_attrs": {"band_width_mm": Decimal("2.00")}},
        )
        # Same value → empty diff. Audit-log path uses this to skip
        # writing a no-op item.updated row.
        assert diff == {}

    def test_delete_side_row_when_all_empty(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemRingAttrs(item_id=item.id, band_width_mm=Decimal("2.50")))
        db.commit()
        diff = apply_side_table_payloads(
            db,
            item,
            {"item_ring_attrs": {"band_width_mm": None}},
        )
        db.commit()
        assert db.execute(select(ItemRingAttrs)).first() is None
        # Diff captures the side-row clearing for the audit row.
        assert diff == {"item_ring_attrs": {"band_width_mm": None}}

    def test_delete_is_noop_when_row_already_absent(self, db: Session) -> None:
        item = _make_item(db)
        diff = apply_side_table_payloads(
            db,
            item,
            {"item_ring_attrs": {"band_width_mm": None}},
        )
        # No row existed → no diff (no audit event).
        assert diff == {}


class TestFormValuesForItem:
    def test_no_side_row_echoes_blank(self, db: Session) -> None:
        item = _make_item(db)
        result = side_table_form_values_for_item(item, {"band_width_mm"})
        assert result == {"band_width_mm": ""}

    def test_side_row_echoes_value(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemRingAttrs(item_id=item.id, band_width_mm=Decimal("2.50")))
        db.commit()
        result = side_table_form_values_for_item(item, {"band_width_mm"})
        assert result == {"band_width_mm": "2.50"}

    def test_unpicked_entries_omitted(self, db: Session) -> None:
        item = _make_item(db)
        result = side_table_form_values_for_item(item, set())
        assert result == {}
