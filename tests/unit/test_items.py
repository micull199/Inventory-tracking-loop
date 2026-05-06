"""Unit tests for the ``Item`` ORM model.

Cover defaults and DB-level constraints (sku unique across archived,
qr_code partial unique). Route-level tests live in
``tests/integration/test_items_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Item, TaxonomyNode, TrackingMode


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    with SessionLocal() as session:
        yield session


def _node(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


class TestItemDefaults:
    def test_minimal_item_has_required_fields_only(self, db: Session) -> None:
        node = _node(db)
        item = Item(
            sku="RM-001",
            name="Silver wire",
            taxonomy_node_id=node.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db.add(item)
        db.commit()
        db.refresh(item)

        assert item.id is not None
        assert item.sku == "RM-001"
        assert item.name == "Silver wire"
        assert item.taxonomy_node_id == node.id
        assert item.unit == "g"
        assert item.tracking_mode is TrackingMode.QTY
        assert item.requires_checkout is False
        assert item.current_qty == Decimal("0")
        assert item.reorder_threshold == Decimal("0")
        assert item.reorder_qty == Decimal("0")
        assert item.supplier_id is None
        assert item.location_id is None
        assert item.qr_code is None
        assert item.notes is None
        assert item.archived_at is None
        assert item.created_at is not None
        assert item.updated_at is not None

    def test_decimals_round_trip(self, db: Session) -> None:
        node = _node(db)
        item = Item(
            sku="RM-002",
            name="Polishing compound",
            taxonomy_node_id=node.id,
            unit="ml",
            tracking_mode=TrackingMode.QTY,
            current_qty=Decimal("1234.5678"),
            reorder_threshold=Decimal("100"),
            reorder_qty=Decimal("500"),
        )
        db.add(item)
        db.commit()
        db.refresh(item)

        assert item.current_qty == Decimal("1234.5678")
        assert item.reorder_threshold == Decimal("100")
        assert item.reorder_qty == Decimal("500")

    def test_unique_tracking_mode_round_trips(self, db: Session) -> None:
        node = _node(db)
        item = Item(
            sku="T-001",
            name="Specific mould",
            taxonomy_node_id=node.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            requires_checkout=True,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        assert item.tracking_mode is TrackingMode.UNIQUE
        assert item.requires_checkout is True


class TestItemSkuUniqueness:
    def test_sku_is_unique(self, db: Session) -> None:
        node = _node(db)
        db.add(
            Item(
                sku="RM-001",
                name="A",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
            )
        )
        db.commit()
        db.add(
            Item(
                sku="RM-001",
                name="B",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_sku_uniqueness_spans_archived(self, db: Session) -> None:
        """Archiving must not free the SKU. Operator renames or unarchives."""
        node = _node(db)
        db.add(
            Item(
                sku="RM-001",
                name="Old",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.commit()
        db.add(
            Item(
                sku="RM-001",
                name="New",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestItemQrUniqueness:
    def test_qr_is_unique_when_set(self, db: Session) -> None:
        node = _node(db)
        db.add(
            Item(
                sku="A",
                name="A",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                qr_code="qr-123",
            )
        )
        db.commit()
        db.add(
            Item(
                sku="B",
                name="B",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                qr_code="qr-123",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_qr_null_does_not_collide_with_other_null(
        self, db: Session
    ) -> None:
        """Partial unique index — multiple NULL qr_code rows allowed."""
        node = _node(db)
        db.add_all(
            [
                Item(
                    sku="A",
                    name="A",
                    taxonomy_node_id=node.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="B",
                    name="B",
                    taxonomy_node_id=node.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="C",
                    name="C",
                    taxonomy_node_id=node.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
            ]
        )
        db.commit()  # all NULL — should succeed.

    def test_qr_uniqueness_spans_archived(self, db: Session) -> None:
        node = _node(db)
        db.add(
            Item(
                sku="A",
                name="A",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                qr_code="qr-archived",
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.commit()
        db.add(
            Item(
                sku="B",
                name="B",
                taxonomy_node_id=node.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                qr_code="qr-archived",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestItemRequiredColumns:
    def test_taxonomy_node_id_is_required(self, db: Session) -> None:
        item = Item(  # type: ignore[call-arg]
            sku="X",
            name="X",
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db.add(item)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
