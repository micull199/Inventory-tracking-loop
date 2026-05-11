"""Unit tests for the ``Supplier`` ORM model.

These cover defaults and DB-level constraints (unique name) without going
through the HTTP surface. The route-level tests live in
``tests/integration/test_suppliers_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Supplier


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


class TestSupplierDefaults:
    def test_minimal_supplier_has_only_name(self, db: Session) -> None:
        s = Supplier(name="Acme Wax Co")
        db.add(s)
        db.commit()
        db.refresh(s)

        assert s.id is not None
        assert s.name == "Acme Wax Co"
        assert s.email is None
        assert s.phone is None
        assert s.notes is None
        assert s.archived_at is None
        assert s.created_at is not None
        assert s.updated_at is not None

    def test_optional_fields_round_trip(self, db: Session) -> None:
        s = Supplier(
            name="Brindleys",
            email="orders@brindleys.test",
            phone="020 7946 0000",
            notes="Trade account #4421",
        )
        db.add(s)
        db.commit()
        db.refresh(s)

        assert s.email == "orders@brindleys.test"
        assert s.phone == "020 7946 0000"
        assert s.notes == "Trade account #4421"

    def test_archived_at_can_be_set(self, db: Session) -> None:
        s = Supplier(name="Old Vendor", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(s)
        db.commit()
        db.refresh(s)
        assert s.archived_at is not None
        assert s.archived_at.year == 2026


class TestSupplierConstraints:
    def test_name_is_unique(self, db: Session) -> None:
        db.add(Supplier(name="Acme Wax Co"))
        db.commit()

        db.add(Supplier(name="Acme Wax Co"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_unique_constraint_applies_even_when_other_is_archived(self, db: Session) -> None:
        """Archiving doesn't free the name. Operator must rename or unarchive."""
        archived = Supplier(name="Acme Wax Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(archived)
        db.commit()

        db.add(Supplier(name="Acme Wax Co"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_name_is_required(self, db: Session) -> None:
        s = Supplier()  # type: ignore[call-arg]
        db.add(s)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
