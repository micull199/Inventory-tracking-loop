"""Unit tests for the S3 (modified) designs additions.

Covers the ``Design`` model + the ``DSG-NNNN`` allocator. See
``docs/adr/003-designs-split-from-taxonomy.md`` for the architectural
context (Strategy A, items FK deferred, shared cross-location designs,
CAD versioning fields).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.designs import allocate_design_code
from app.models import (
    AlloyFamily,
    Design,
    Metal,
    MetalColour,
    SequenceCounter,
    StyleFamily,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        # Seed both counters — migration 0026 seeds stone_code, 0044 seeds
        # design_code. ``create_all`` doesn't run migration data.
        session.add(SequenceCounter(name="stone_code", next_value=1))
        session.add(SequenceCounter(name="design_code", next_value=1))
        session.commit()
        yield session


class TestDesignModel:
    def test_minimal_design(self, db: Session) -> None:
        design = Design(design_code="DSG-0001", name="Emma")
        db.add(design)
        db.commit()
        db.refresh(design)
        assert design.id is not None
        assert design.archived_at is None
        assert design.style_family is None
        assert design.default_metal_id is None

    def test_full_round_trip(self, db: Session) -> None:
        metal = Metal(
            metal_code="18KYG",
            name="18ct Yellow Gold",
            alloy_family=AlloyFamily.GOLD,
            karat=18,
            purity_pct=Decimal("75.000"),
            colour=MetalColour.YELLOW,
        )
        db.add(metal)
        db.commit()
        db.refresh(metal)
        design = Design(
            design_code="DSG-0001",
            name="Emma",
            collection="Bridal 2026",
            style_family=StyleFamily.SOLITAIRE,
            designer="Jane Smith",
            cad_file_path="/cad/emma.3dm",
            cad_version="v1.2",
            cad_updated_at=datetime(2026, 5, 15, 12, 0, tzinfo=UTC),
            default_metal_id=metal.id,
            intro_date=date(2026, 4, 1),
            standard_cost=Decimal("1250.00"),
        )
        db.add(design)
        db.commit()
        db.refresh(design)
        assert design.style_family == StyleFamily.SOLITAIRE
        assert design.cad_version == "v1.2"
        # SQLite drops tz info on round-trip (no native timestamptz); compare
        # naive components instead.
        assert design.cad_updated_at is not None
        assert design.cad_updated_at.replace(tzinfo=None) == datetime(
            2026, 5, 15, 12, 0
        )
        assert design.default_metal_id == metal.id
        assert design.standard_cost == Decimal("1250.00")

    def test_design_code_unique_across_archived(self, db: Session) -> None:
        archived = Design(
            design_code="DSG-0001",
            name="Emma (old)",
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(archived)
        db.commit()
        db.add(Design(design_code="DSG-0001", name="Emma (new)"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_same_name_different_code_allowed(self, db: Session) -> None:
        # Two different designs can share a name (e.g. "Emma" and a
        # later "Emma Heavy" variant that the operator chose to also
        # name "Emma"). Only ``design_code`` is unique.
        db.add(Design(design_code="DSG-0001", name="Emma"))
        db.add(Design(design_code="DSG-0002", name="Emma"))
        db.commit()


class TestDesignCodeAllocator:
    def test_allocates_sequential(self, db: Session) -> None:
        first = allocate_design_code(db)
        second = allocate_design_code(db)
        third = allocate_design_code(db)
        assert first == "DSG-0001"
        assert second == "DSG-0002"
        assert third == "DSG-0003"

    def test_advances_counter(self, db: Session) -> None:
        allocate_design_code(db)
        allocate_design_code(db)
        db.commit()
        counter = db.execute(
            select(SequenceCounter).where(SequenceCounter.name == "design_code")
        ).scalar_one()
        assert counter.next_value == 3

    def test_design_and_stone_counters_independent(self, db: Session) -> None:
        # The two allocators share infrastructure but spin separate rows;
        # advancing one must not affect the other.
        from app.stones import allocate_stone_code

        allocate_stone_code(db)
        allocate_stone_code(db)
        allocate_design_code(db)
        db.commit()
        stone_counter = db.execute(
            select(SequenceCounter).where(SequenceCounter.name == "stone_code")
        ).scalar_one()
        design_counter = db.execute(
            select(SequenceCounter).where(SequenceCounter.name == "design_code")
        ).scalar_one()
        assert stone_counter.next_value == 3
        assert design_counter.next_value == 2

    def test_raises_when_counter_missing(self, db: Session) -> None:
        db.execute(
            SequenceCounter.__table__.delete().where(
                SequenceCounter.name == "design_code"
            )
        )
        db.commit()
        with pytest.raises(RuntimeError, match="counter row missing"):
            allocate_design_code(db)
