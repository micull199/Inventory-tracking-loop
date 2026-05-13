"""Unit tests for ``app.field_storage``.

Slice 2's storage abstraction layer. Currently scoped to the
``ItemFieldValue`` (sparse-row) path — slice 6 extends to column-backed
catalog entries.

Forcing function: if the dispatch table or any read/write primitive drifts,
the existing items / field-value integration suites will turn red. These
tests cover the abstraction directly so regressions surface in unit scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import field_storage
from app.db import Base
from app.models import (
    FieldType,
    Item,
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


def _seed_node(db: Session) -> TaxonomyNode:
    node = TaxonomyNode(name="Raw Materials")
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _seed_item(db: Session, node: TaxonomyNode, *, sku: str = "RM-001") -> Item:
    item = Item(
        sku=sku,
        name="X",
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
    field_type: FieldType,
    key: str,
    options: list[str] | None = None,
) -> TaxonomyFieldDef:
    fd = TaxonomyFieldDef(
        node_id=node.id,
        name=key.replace("_", " ").title(),
        key=key,
        type=field_type,
        options_json=options,
    )
    db.add(fd)
    db.commit()
    db.refresh(fd)
    return fd


# ---------------------------------------------------------------------------
# value_column_for
# ---------------------------------------------------------------------------


_TYPE_TO_COLUMN: list[tuple[FieldType, str]] = [
    (FieldType.TEXT, "value_text"),
    (FieldType.NUMBER, "value_number"),
    (FieldType.DECIMAL, "value_decimal"),
    (FieldType.DATE, "value_date"),
    (FieldType.BOOLEAN, "value_bool"),
    (FieldType.SELECT, "value_text"),
    (FieldType.MULTISELECT, "value_json"),
]


@pytest.mark.parametrize(("field_type", "expected"), _TYPE_TO_COLUMN)
def test_value_column_for_each_type(field_type: FieldType, expected: str) -> None:
    assert field_storage.value_column_for(field_type) == expected


def test_value_column_by_type_covers_every_field_type() -> None:
    """The dispatch map must include every ``FieldType`` member — missing one
    would crash route handlers at runtime with a ``KeyError``."""

    seen = set(field_storage.VALUE_COLUMN_BY_TYPE.keys())
    for member in FieldType:
        assert member in seen


# ---------------------------------------------------------------------------
# write_new_value + read_stored_value round-trip
# ---------------------------------------------------------------------------


_ROUND_TRIP_CASES: list[tuple[FieldType, list[str] | None, Any]] = [
    (FieldType.TEXT, None, "hello"),
    (FieldType.NUMBER, None, 42),
    (FieldType.DECIMAL, None, Decimal("3.1415")),
    (FieldType.DATE, None, date(2026, 5, 13)),
    (FieldType.BOOLEAN, None, True),
    (FieldType.BOOLEAN, None, False),
    (FieldType.SELECT, ["A", "B", "C"], "B"),
    (FieldType.MULTISELECT, ["A", "B", "C"], ["A", "C"]),
]


@pytest.mark.parametrize(
    ("field_type", "options", "value"),
    _ROUND_TRIP_CASES,
    ids=lambda v: repr(v),
)
def test_write_and_read_round_trip(
    db: Session,
    field_type: FieldType,
    options: list[str] | None,
    value: Any,
) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd = _seed_field_def(db, node, field_type=field_type, key="x", options=options)

    ifv = field_storage.write_new_value(db, item_id=item.id, field_def=fd, value=value)
    db.commit()
    db.refresh(ifv)

    # Stored on the correct column …
    column = field_storage.value_column_for(field_type)
    assert getattr(ifv, column) == value
    # … and read_stored_value rounds it back out.
    assert field_storage.read_stored_value(fd, ifv) == value


def test_write_new_value_leaves_other_columns_null(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd = _seed_field_def(db, node, field_type=FieldType.NUMBER, key="qty_per_pack")
    ifv = field_storage.write_new_value(db, item_id=item.id, field_def=fd, value=99)
    db.commit()
    db.refresh(ifv)

    assert ifv.value_number == 99
    # Every other ``value_*`` column stays NULL — sparse storage invariant.
    assert ifv.value_text is None
    assert ifv.value_decimal is None
    assert ifv.value_date is None
    assert ifv.value_bool is None
    assert ifv.value_json is None


# ---------------------------------------------------------------------------
# update_existing_value
# ---------------------------------------------------------------------------


def test_update_existing_value_overwrites_in_place(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd = _seed_field_def(db, node, field_type=FieldType.TEXT, key="alloy")
    ifv = field_storage.write_new_value(db, item_id=item.id, field_def=fd, value="silver")
    db.commit()

    field_storage.update_existing_value(fd, ifv, "gold")
    db.commit()
    db.refresh(ifv)

    assert ifv.value_text == "gold"
    assert field_storage.read_stored_value(fd, ifv) == "gold"


def test_update_existing_value_does_not_populate_other_columns(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd = _seed_field_def(db, node, field_type=FieldType.DECIMAL, key="purity")
    ifv = field_storage.write_new_value(
        db, item_id=item.id, field_def=fd, value=Decimal("0.925")
    )
    db.commit()

    field_storage.update_existing_value(fd, ifv, Decimal("0.999"))
    db.commit()
    db.refresh(ifv)

    assert ifv.value_decimal == Decimal("0.999")
    assert ifv.value_text is None
    assert ifv.value_number is None


# ---------------------------------------------------------------------------
# load_rows_for_item
# ---------------------------------------------------------------------------


def test_load_rows_for_item_returns_empty_when_no_values(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    assert field_storage.load_rows_for_item(db, item.id) == {}


def test_load_rows_for_item_keys_by_field_def_id(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd_text = _seed_field_def(db, node, field_type=FieldType.TEXT, key="alloy")
    fd_num = _seed_field_def(db, node, field_type=FieldType.NUMBER, key="qty_per_pack")
    field_storage.write_new_value(db, item_id=item.id, field_def=fd_text, value="silver")
    field_storage.write_new_value(db, item_id=item.id, field_def=fd_num, value=12)
    db.commit()

    rows = field_storage.load_rows_for_item(db, item.id)

    assert set(rows.keys()) == {fd_text.id, fd_num.id}
    assert rows[fd_text.id].value_text == "silver"
    assert rows[fd_num.id].value_number == 12


def test_load_rows_for_item_scopes_to_the_requested_item(db: Session) -> None:
    """A second item's field values must not leak through."""

    node = _seed_node(db)
    item_a = _seed_item(db, node, sku="A")
    item_b = _seed_item(db, node, sku="B")
    fd = _seed_field_def(db, node, field_type=FieldType.TEXT, key="alloy")
    field_storage.write_new_value(db, item_id=item_a.id, field_def=fd, value="silver")
    field_storage.write_new_value(db, item_id=item_b.id, field_def=fd, value="gold")
    db.commit()

    rows_a = field_storage.load_rows_for_item(db, item_a.id)
    rows_b = field_storage.load_rows_for_item(db, item_b.id)

    assert rows_a[fd.id].value_text == "silver"
    assert rows_b[fd.id].value_text == "gold"


# ---------------------------------------------------------------------------
# Defensive: SELECT and MULTISELECT use the right value columns even though
# they aren't 1:1 with FieldType.
# ---------------------------------------------------------------------------


def test_select_uses_text_column(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd = _seed_field_def(
        db, node, field_type=FieldType.SELECT, key="finish", options=["matte", "polished"]
    )
    ifv = field_storage.write_new_value(
        db, item_id=item.id, field_def=fd, value="polished"
    )
    db.commit()
    db.refresh(ifv)

    assert ifv.value_text == "polished"
    assert ifv.value_json is None


def test_multiselect_uses_json_column(db: Session) -> None:
    node = _seed_node(db)
    item = _seed_item(db, node)
    fd = _seed_field_def(
        db,
        node,
        field_type=FieldType.MULTISELECT,
        key="tags",
        options=["urgent", "rework", "qc"],
    )
    ifv = field_storage.write_new_value(
        db, item_id=item.id, field_def=fd, value=["urgent", "qc"]
    )
    db.commit()
    db.refresh(ifv)

    assert ifv.value_json == ["urgent", "qc"]
    assert ifv.value_text is None
