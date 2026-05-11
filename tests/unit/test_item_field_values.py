"""Unit tests for the ``ItemFieldValue`` ORM model (I2).

Covers per-type round-trip into the right value column, sparse-storage
defaults (every other column NULL), and the ``(item_id, field_def_id)``
unique index. Route-level tests live in
``tests/integration/test_items_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    FieldType,
    Item,
    ItemFieldValue,
    TaxonomyFieldDef,
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


def _seed_node(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _seed_item(db: Session, node: TaxonomyNode, *, sku: str = "RM-001") -> Item:
    item = Item(
        sku=sku,
        name="Item",
        taxonomy_node_id=node.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _seed_field_def(
    db: Session,
    node: TaxonomyNode,
    *,
    name: str,
    field_type: FieldType,
    options: list[str] | None = None,
    required: bool = False,
    sort_order: int = 0,
    key_override: str | None = None,
) -> TaxonomyFieldDef:
    fd = TaxonomyFieldDef(
        node_id=node.id,
        name=name,
        key=key_override or name.lower().replace(" ", "_"),
        type=field_type,
        options_json=options,
        required=required,
        sort_order=sort_order,
    )
    db.add(fd)
    db.commit()
    db.refresh(fd)
    return fd


class TestItemFieldValueDefaults:
    def test_minimal_row_has_only_fk_columns_set(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Alloy", field_type=FieldType.TEXT)

        ifv = ItemFieldValue(item_id=item.id, field_def_id=fd.id)
        db.add(ifv)
        db.commit()
        db.refresh(ifv)

        assert ifv.id is not None
        assert ifv.item_id == item.id
        assert ifv.field_def_id == fd.id
        assert ifv.value_text is None
        assert ifv.value_number is None
        assert ifv.value_decimal is None
        assert ifv.value_date is None
        assert ifv.value_bool is None
        assert ifv.value_json is None
        assert ifv.created_at is not None
        assert ifv.updated_at is not None


class TestItemFieldValuePerType:
    def test_text_round_trip(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Alloy", field_type=FieldType.TEXT)
        db.add(ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_text="silver"))
        db.commit()
        ifv = db.query(ItemFieldValue).one()
        assert ifv.value_text == "silver"
        assert ifv.value_number is None

    def test_number_round_trip(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Karat", field_type=FieldType.NUMBER)
        db.add(ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_number=18))
        db.commit()
        ifv = db.query(ItemFieldValue).one()
        assert ifv.value_number == 18

    def test_decimal_round_trip_preserves_precision(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Density", field_type=FieldType.DECIMAL)
        db.add(
            ItemFieldValue(
                item_id=item.id,
                field_def_id=fd.id,
                value_decimal=Decimal("10.4900"),
            )
        )
        db.commit()
        ifv = db.query(ItemFieldValue).one()
        assert ifv.value_decimal == Decimal("10.4900")

    def test_date_round_trip(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Last Calibrated", field_type=FieldType.DATE)
        db.add(
            ItemFieldValue(
                item_id=item.id,
                field_def_id=fd.id,
                value_date=date(2026, 1, 15),
            )
        )
        db.commit()
        ifv = db.query(ItemFieldValue).one()
        assert ifv.value_date == date(2026, 1, 15)

    def test_boolean_round_trip(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Hazardous", field_type=FieldType.BOOLEAN)
        # Both True and False round-trip cleanly.
        db.add(ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_bool=False))
        db.commit()
        ifv = db.query(ItemFieldValue).one()
        assert ifv.value_bool is False

    def test_multiselect_json_round_trip(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(
            db,
            node,
            name="Tags",
            field_type=FieldType.MULTISELECT,
            options=["a", "b", "c"],
        )
        db.add(
            ItemFieldValue(
                item_id=item.id,
                field_def_id=fd.id,
                value_json=["a", "c"],
            )
        )
        db.commit()
        ifv = db.query(ItemFieldValue).one()
        assert ifv.value_json == ["a", "c"]


class TestItemFieldValueUniqueness:
    def test_one_row_per_item_field_def_pair(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        fd = _seed_field_def(db, node, name="Alloy", field_type=FieldType.TEXT)

        db.add(ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_text="silver"))
        db.commit()

        # Second row for the same (item, field_def) is rejected by the unique
        # index.
        db.add(ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_text="gold"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_same_field_def_on_different_items_is_fine(self, db: Session) -> None:
        node = _seed_node(db)
        a = _seed_item(db, node, sku="RM-A")
        b = _seed_item(db, node, sku="RM-B")
        fd = _seed_field_def(db, node, name="Alloy", field_type=FieldType.TEXT)

        db.add_all(
            [
                ItemFieldValue(item_id=a.id, field_def_id=fd.id, value_text="silver"),
                ItemFieldValue(item_id=b.id, field_def_id=fd.id, value_text="gold"),
            ]
        )
        db.commit()
        assert db.query(ItemFieldValue).count() == 2

    def test_different_field_defs_on_same_item_is_fine(self, db: Session) -> None:
        node = _seed_node(db)
        item = _seed_item(db, node)
        a = _seed_field_def(db, node, name="Alloy", field_type=FieldType.TEXT, key_override="alloy")
        b = _seed_field_def(
            db, node, name="Karat", field_type=FieldType.NUMBER, key_override="karat"
        )
        db.add_all(
            [
                ItemFieldValue(item_id=item.id, field_def_id=a.id, value_text="silver"),
                ItemFieldValue(item_id=item.id, field_def_id=b.id, value_number=18),
            ]
        )
        db.commit()
        assert db.query(ItemFieldValue).count() == 2
