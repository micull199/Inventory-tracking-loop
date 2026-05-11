"""Unit tests for the ``TaxonomyNode`` ORM model.

Covers defaults and DB-level constraints (partial-unique on ``(name)`` for
top-level rows; partial-unique on ``(parent_id, name)`` for sub-categories;
both span archived rows). Route-level tests live in
``tests/integration/test_taxonomy_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import TaxonomyNode


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


class TestTaxonomyDefaults:
    def test_minimal_top_level_node(self, db: Session) -> None:
        node = TaxonomyNode(name="Raw Materials")
        db.add(node)
        db.commit()
        db.refresh(node)

        assert node.id is not None
        assert node.name == "Raw Materials"
        assert node.parent_id is None
        assert node.sort_order == 0
        assert node.archived_at is None
        assert node.created_at is not None
        assert node.updated_at is not None

    def test_sort_order_is_settable(self, db: Session) -> None:
        node = TaxonomyNode(name="Tools", sort_order=42)
        db.add(node)
        db.commit()
        db.refresh(node)
        assert node.sort_order == 42

    def test_archived_at_can_be_set(self, db: Session) -> None:
        node = TaxonomyNode(name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(node)
        db.commit()
        db.refresh(node)
        assert node.archived_at is not None
        assert node.archived_at.year == 2026

    def test_parent_id_can_be_set_for_sub_category(self, db: Session) -> None:
        """The schema accepts ``parent_id``; S3 routes don't expose it but S4 will."""
        parent = TaxonomyNode(name="Raw Materials")
        db.add(parent)
        db.commit()
        db.refresh(parent)

        child = TaxonomyNode(name="Silver", parent_id=parent.id)
        db.add(child)
        db.commit()
        db.refresh(child)
        assert child.parent_id == parent.id


class TestTaxonomyConstraints:
    def test_top_level_name_is_unique(self, db: Session) -> None:
        db.add(TaxonomyNode(name="Raw Materials"))
        db.commit()

        db.add(TaxonomyNode(name="Raw Materials"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_top_level_unique_applies_even_when_other_is_archived(self, db: Session) -> None:
        """Archiving doesn't free the name. Operator must rename or unarchive."""
        archived = TaxonomyNode(name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db.add(archived)
        db.commit()

        db.add(TaxonomyNode(name="Raw Materials"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_name_is_required(self, db: Session) -> None:
        node = TaxonomyNode()  # type: ignore[call-arg]
        db.add(node)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_sub_category_name_unique_within_parent(self, db: Session) -> None:
        """``(parent_id, name)`` partial unique index covers sibling sub-cats.

        Not exercised by S3's routes — but the schema is in place for S4. Test
        here so a future migration tweak doesn't silently weaken sibling
        uniqueness.
        """
        parent = TaxonomyNode(name="Raw Materials")
        db.add(parent)
        db.commit()
        db.refresh(parent)

        db.add(TaxonomyNode(name="Silver", parent_id=parent.id))
        db.commit()

        db.add(TaxonomyNode(name="Silver", parent_id=parent.id))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_sub_category_name_can_match_top_level(self, db: Session) -> None:
        """The two partial indexes don't collide: a top-level "Silver" and a
        sub-category "Silver" under some parent are independent rows."""
        top = TaxonomyNode(name="Silver")
        parent = TaxonomyNode(name="Raw Materials")
        db.add_all([top, parent])
        db.commit()
        db.refresh(parent)

        db.add(TaxonomyNode(name="Silver", parent_id=parent.id))
        db.commit()  # must not raise

    def test_same_name_under_different_parents_is_allowed(self, db: Session) -> None:
        a = TaxonomyNode(name="Raw Materials")
        b = TaxonomyNode(name="Consumables")
        db.add_all([a, b])
        db.commit()
        db.refresh(a)
        db.refresh(b)

        db.add_all(
            [
                TaxonomyNode(name="Silver", parent_id=a.id),
                TaxonomyNode(name="Silver", parent_id=b.id),
            ]
        )
        db.commit()  # must not raise
