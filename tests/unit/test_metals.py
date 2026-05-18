"""Unit tests for the S2 metal additions.

Covers:
- ``Metal`` (``metal_master``): defaults, code uniqueness across archived.
- ``MetalSpotPrice``: (metal_id, as_of_date) uniqueness.
- The new metal-related columns on ``Item``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    AlloyFamily,
    Archetype,
    Item,
    Metal,
    MetalColour,
    MetalSpotPrice,
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


def _make_metal(db: Session, code: str = "18KYG") -> Metal:
    metal = Metal(
        metal_code=code,
        name=f"Test {code}",
        alloy_family=AlloyFamily.GOLD,
        karat=18,
        purity_pct=Decimal("75.000"),
        colour=MetalColour.YELLOW,
    )
    db.add(metal)
    db.commit()
    db.refresh(metal)
    return metal


class TestMetal:
    def test_minimal_metal(self, db: Session) -> None:
        metal = _make_metal(db)
        assert metal.id is not None
        assert metal.alloy_family == AlloyFamily.GOLD
        assert metal.colour == MetalColour.YELLOW
        assert metal.archived_at is None

    def test_non_gold_metal_has_null_karat(self, db: Session) -> None:
        metal = Metal(
            metal_code="PLAT950",
            name="Platinum 950",
            alloy_family=AlloyFamily.PLATINUM,
            karat=None,
            purity_pct=Decimal("95.000"),
            colour=MetalColour.PLATINUM,
        )
        db.add(metal)
        db.commit()
        db.refresh(metal)
        assert metal.karat is None

    def test_metal_code_unique_across_archived(self, db: Session) -> None:
        archived = Metal(
            metal_code="18KYG",
            name="Archived 18K",
            alloy_family=AlloyFamily.GOLD,
            karat=18,
            purity_pct=Decimal("75.000"),
            colour=MetalColour.YELLOW,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(archived)
        db.commit()

        # Active row with same code — must fail.
        db.add(
            Metal(
                metal_code="18KYG",
                name="New 18K",
                alloy_family=AlloyFamily.GOLD,
                karat=18,
                purity_pct=Decimal("75.000"),
                colour=MetalColour.YELLOW,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()


class TestMetalSpotPrice:
    def test_minimal_price(self, db: Session) -> None:
        metal = _make_metal(db)
        price = MetalSpotPrice(
            metal_id=metal.id,
            as_of_date=date(2026, 5, 15),
            price_per_gram=Decimal("123.456789"),
            source="manual",
        )
        db.add(price)
        db.commit()
        db.refresh(price)
        assert price.id is not None
        assert price.price_per_gram == Decimal("123.456789")

    def test_one_price_per_metal_per_date(self, db: Session) -> None:
        metal = _make_metal(db)
        db.add(
            MetalSpotPrice(
                metal_id=metal.id,
                as_of_date=date(2026, 5, 15),
                price_per_gram=Decimal("100.0"),
                source="manual",
            )
        )
        db.commit()

        db.add(
            MetalSpotPrice(
                metal_id=metal.id,
                as_of_date=date(2026, 5, 15),
                price_per_gram=Decimal("105.0"),
                source="manual",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_different_dates_coexist(self, db: Session) -> None:
        metal = _make_metal(db)
        db.add_all(
            [
                MetalSpotPrice(
                    metal_id=metal.id,
                    as_of_date=date(2026, 5, 14),
                    price_per_gram=Decimal("100.0"),
                    source="manual",
                ),
                MetalSpotPrice(
                    metal_id=metal.id,
                    as_of_date=date(2026, 5, 15),
                    price_per_gram=Decimal("101.0"),
                    source="manual",
                ),
            ]
        )
        db.commit()


class TestItemMetalColumns:
    def test_item_round_trips_metal_fks(self, db: Session) -> None:
        primary = _make_metal(db, code="18KWG")
        secondary = _make_metal(db, code="18KYG")
        node = TaxonomyNode(
            name="Two-tone Rings", sku_prefix="TTR", archetype=Archetype.UNIQUE
        )
        db.add(node)
        db.commit()
        db.refresh(node)

        item = Item(
            sku="TTR-0001",
            name="Two-tone ring",
            taxonomy_node_id=node.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            metal_id=primary.id,
            secondary_metal_id=secondary.id,
            pure_metal_weight_g=Decimal("3.7500"),
        )
        db.add(item)
        db.commit()
        db.refresh(item)

        assert item.metal_id == primary.id
        assert item.secondary_metal_id == secondary.id
        assert item.pure_metal_weight_g == Decimal("3.7500")
