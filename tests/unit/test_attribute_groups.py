"""Unit tests for the S4 per-category attribute groups.

Covers all seven side tables: ``item_ring_attrs``, ``item_engagement_attrs``,
``item_band_attrs``, ``item_earring_attrs``, ``item_chain_attrs``,
``item_pendant_attrs``, ``item_engraving_attrs``. Each one:

- has ``item_id`` as PK + CASCADE FK to ``items`` (delete the item, the side
  row goes too),
- round-trips its enums + decimals + booleans,
- can only have one row per item (the PK enforces it).
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    Archetype,
    BailType,
    BandProfile,
    BandSetStyle,
    ChainClosure,
    ChainStyle,
    EarringClosure,
    EarringSold,
    EarringStyle,
    EngravingStyle,
    GalleryStyle,
    Item,
    ItemBandAttrs,
    ItemChainAttrs,
    ItemEarringAttrs,
    ItemEngagementAttrs,
    ItemEngravingAttrs,
    ItemPendantAttrs,
    ItemRingAttrs,
    MetalFinish,
    ProngStyle,
    RingSizeStandard,
    SettingStyle,
    ShankStyle,
    TaxonomyNode,
    TrackingMode,
    WornAs,
)


@pytest.fixture
def db() -> Iterator[Session]:
    # ``foreign_keys=ON`` so the CASCADE / RESTRICT semantics actually fire on
    # SQLite (default off). Tests below depend on the FK behaviour to verify
    # the side rows clean up when their parent item is deleted.
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        session.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys=ON"))
        yield session


_NEXT_PREFIX_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _make_item(db: Session, name: str = "Ring 1") -> Item:
    existing = db.execute(select(TaxonomyNode)).scalars().all()
    suffix = _NEXT_PREFIX_CHARS[len(existing) % len(_NEXT_PREFIX_CHARS)]
    node = TaxonomyNode(
        name=f"Cat-{name}", sku_prefix=f"CT{suffix}", archetype=Archetype.UNIQUE
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    item = Item(
        sku=f"CT{suffix}-{name}",
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestItemRingAttrs:
    def test_round_trip(self, db: Session) -> None:
        item = _make_item(db)
        row = ItemRingAttrs(
            item_id=item.id,
            ring_size=Decimal("6.50"),
            size_standard=RingSizeStandard.US,
            band_width_mm=Decimal("2.50"),
            profile=BandProfile.COURT,
            finish=MetalFinish.POLISHED,
            comfort_fit=True,
            shank_style=ShankStyle.SOLID,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.ring_size == Decimal("6.50")
        assert row.size_standard == RingSizeStandard.US
        assert row.profile == BandProfile.COURT
        assert row.comfort_fit is True

    def test_pk_is_item_id_only(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemRingAttrs(item_id=item.id, ring_size=Decimal("6.50")))
        db.commit()
        # Second row for same item — must fail (PK violation).
        db.add(ItemRingAttrs(item_id=item.id, ring_size=Decimal("7.00")))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_cascade_on_item_delete(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemRingAttrs(item_id=item.id, ring_size=Decimal("6.50")))
        db.commit()
        # Deleting the parent item must drop the side row via CASCADE.
        db.delete(item)
        db.commit()
        assert db.execute(select(ItemRingAttrs)).first() is None


class TestItemEngagementAttrs:
    def test_round_trip_with_paired_band(self, db: Session) -> None:
        engagement = _make_item(db, name="Engagement")
        band = _make_item(db, name="Band")
        row = ItemEngagementAttrs(
            item_id=engagement.id,
            setting_style=SettingStyle.SOLITAIRE,
            setting_variation="cathedral",
            prong_count=6,
            prong_style=ProngStyle.CLAW,
            gallery_style=GalleryStyle.OPEN,
            under_bezel=False,
            pairs_with_wedding_band_item_id=band.id,
            mount_price=Decimal("450.00"),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.setting_style == SettingStyle.SOLITAIRE
        assert row.pairs_with_wedding_band_item_id == band.id
        assert row.mount_price == Decimal("450.00")


class TestItemBandAttrs:
    def test_round_trip_inverse_pairing(self, db: Session) -> None:
        band = _make_item(db, name="Band-X")
        engagement = _make_item(db, name="Eng-X")
        row = ItemBandAttrs(
            item_id=band.id,
            band_set_style=BandSetStyle.HALF_ETERNITY,
            pairs_with_engagement_item_id=engagement.id,
            matching_set_id="his-2026-05-15",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.band_set_style == BandSetStyle.HALF_ETERNITY
        assert row.pairs_with_engagement_item_id == engagement.id
        assert row.matching_set_id == "his-2026-05-15"


class TestItemEarringAttrs:
    def test_round_trip_hoop(self, db: Session) -> None:
        item = _make_item(db, name="Hoop")
        row = ItemEarringAttrs(
            item_id=item.id,
            sold_as=EarringSold.PAIR,
            closure_type=EarringClosure.HUGGIE,
            style=EarringStyle.HOOP,
            hoop_diameter_mm=Decimal("15.00"),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.style == EarringStyle.HOOP
        assert row.hoop_diameter_mm == Decimal("15.00")
        assert row.drop_length_mm is None


class TestItemChainAttrs:
    def test_round_trip_adjustable(self, db: Session) -> None:
        item = _make_item(db, name="Chain")
        row = ItemChainAttrs(
            item_id=item.id,
            chain_style=ChainStyle.CABLE,
            length_mm=Decimal("450.00"),
            adjustable=True,
            min_length_mm=Decimal("400.00"),
            max_length_mm=Decimal("500.00"),
            link_width_mm=Decimal("1.50"),
            closure_type=ChainClosure.LOBSTER,
            worn_as=WornAs.NECKLACE,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.chain_style == ChainStyle.CABLE
        assert row.adjustable is True
        assert row.worn_as == WornAs.NECKLACE


class TestItemPendantAttrs:
    def test_round_trip_with_default_chain(self, db: Session) -> None:
        pendant = _make_item(db, name="Pendant")
        chain = _make_item(db, name="Chain-P")
        row = ItemPendantAttrs(
            item_id=pendant.id,
            length_mm=Decimal("20.00"),
            width_mm=Decimal("15.00"),
            bail_type=BailType.HINGED,
            bail_opening_mm=Decimal("4.50"),
            includes_chain=True,
            default_chain_item_id=chain.id,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.bail_type == BailType.HINGED
        assert row.default_chain_item_id == chain.id


class TestItemEngravingAttrs:
    def test_round_trip(self, db: Session) -> None:
        item = _make_item(db, name="Engr-ring")
        row = ItemEngravingAttrs(
            item_id=item.id,
            engraving_available=True,
            max_chars_inside=30,
            engraving_text="Forever",
            engraving_font="Script",
            engraving_style=EngravingStyle.LASER,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        assert row.engraving_available is True
        assert row.engraving_style == EngravingStyle.LASER

    def test_engraving_available_defaults_false(self, db: Session) -> None:
        item = _make_item(db)
        db.add(ItemEngravingAttrs(item_id=item.id))
        db.commit()
        row = db.execute(select(ItemEngravingAttrs)).scalar_one()
        # Server default 0 -> False for the always-meaningful availability flag.
        assert row.engraving_available is False
