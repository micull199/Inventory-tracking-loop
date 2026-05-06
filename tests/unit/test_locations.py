"""Unit tests for the ``Location`` ORM model.

Covers defaults and DB-level constraints (unique name, even across archived
rows) without going through the HTTP surface. Route-level tests live in
``tests/integration/test_locations_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Location


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


class TestLocationDefaults:
    def test_minimal_location_has_only_name(self, db: Session) -> None:
        loc = Location(name="Workshop Bench")
        db.add(loc)
        db.commit()
        db.refresh(loc)

        assert loc.id is not None
        assert loc.name == "Workshop Bench"
        assert loc.notes is None
        assert loc.archived_at is None
        assert loc.created_at is not None
        assert loc.updated_at is not None

    def test_optional_fields_round_trip(self, db: Session) -> None:
        loc = Location(name="Vault", notes="Combination on file")
        db.add(loc)
        db.commit()
        db.refresh(loc)
        assert loc.notes == "Combination on file"

    def test_archived_at_can_be_set(self, db: Session) -> None:
        loc = Location(name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(loc)
        db.commit()
        db.refresh(loc)
        assert loc.archived_at is not None
        assert loc.archived_at.year == 2026


class TestLocationConstraints:
    def test_name_is_unique(self, db: Session) -> None:
        db.add(Location(name="Workshop Bench"))
        db.commit()

        db.add(Location(name="Workshop Bench"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_unique_constraint_applies_even_when_other_is_archived(
        self, db: Session
    ) -> None:
        """Archiving doesn't free the name. Operator must rename or unarchive."""
        archived = Location(
            name="Workshop Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db.add(archived)
        db.commit()

        db.add(Location(name="Workshop Bench"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_name_is_required(self, db: Session) -> None:
        loc = Location()  # type: ignore[call-arg]
        db.add(loc)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
