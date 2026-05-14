"""Unit tests for the slim ``TaxonomyFieldDef`` (visibility-selector) model.

Covers defaults + the one DB-level uniqueness invariant:

- ``(node_id, key)`` is unique within a node.
- The same key under *different* nodes is fine.

Route-level tests live in ``tests/integration/test_field_defs_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import TaxonomyFieldDef, TaxonomyNode


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _make_node(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    node = TaxonomyNode(name=name)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def test_minimal_field_def(db: Session) -> None:
    node = _make_node(db)
    f = TaxonomyFieldDef(node_id=node.id, key="ring_size")
    db.add(f)
    db.commit()
    db.refresh(f)

    assert f.id is not None
    assert f.node_id == node.id
    assert f.key == "ring_size"
    assert f.required is False
    assert f.sort_order == 0
    assert f.created_at is not None
    assert f.updated_at is not None


def test_required_flag(db: Session) -> None:
    node = _make_node(db)
    f = TaxonomyFieldDef(node_id=node.id, key="ring_size", required=True)
    db.add(f)
    db.commit()
    db.refresh(f)
    assert f.required is True


def test_node_key_uniqueness(db: Session) -> None:
    node = _make_node(db)
    db.add(TaxonomyFieldDef(node_id=node.id, key="ring_size"))
    db.commit()
    db.add(TaxonomyFieldDef(node_id=node.id, key="ring_size"))
    with pytest.raises(IntegrityError):
        db.commit()


def test_same_key_on_different_nodes_is_fine(db: Session) -> None:
    a = _make_node(db, "Rings")
    b = _make_node(db, "Pendants")
    db.add_all(
        [
            TaxonomyFieldDef(node_id=a.id, key="ring_size"),
            TaxonomyFieldDef(node_id=b.id, key="ring_size"),
        ]
    )
    db.commit()
