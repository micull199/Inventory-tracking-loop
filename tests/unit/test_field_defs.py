"""Unit tests for ``TaxonomyFieldDef`` ORM model + ``FieldType`` enum.

Covers defaults and DB-level constraints:
- ``(node_id, name)`` and ``(node_id, key)`` are both unique within a node.
- Both indexes span active *and* archived rows (archiving must not free a name
  or key, because items will reference field defs by id and key for
  cross-version stability).
- The same name/key under *different* nodes is fine.

Route-level tests live in ``tests/integration/test_field_defs_routes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import FieldType, TaxonomyFieldDef, TaxonomyNode


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


class TestFieldDefDefaults:
    def test_minimal_field_def(self, db: Session) -> None:
        node = _make_node(db)
        f = TaxonomyFieldDef(node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT)
        db.add(f)
        db.commit()
        db.refresh(f)

        assert f.id is not None
        assert f.node_id == node.id
        assert f.name == "Karat"
        assert f.key == "karat"
        assert f.type == FieldType.TEXT
        assert f.required is False
        assert f.options_json is None
        assert f.sort_order == 0
        assert f.archived_at is None
        assert f.created_at is not None
        assert f.updated_at is not None

    def test_options_json_round_trips(self, db: Session) -> None:
        node = _make_node(db)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.SELECT,
            options_json=["9", "14", "18"],
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        assert f.options_json == ["9", "14", "18"]

    def test_required_true(self, db: Session) -> None:
        node = _make_node(db)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            required=True,
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        assert f.required is True

    def test_archived_at_can_be_set(self, db: Session) -> None:
        node = _make_node(db)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db.add(f)
        db.commit()
        db.refresh(f)
        assert f.archived_at is not None


class TestFieldDefConstraints:
    def test_name_unique_per_node(self, db: Session) -> None:
        node = _make_node(db)
        db.add(TaxonomyFieldDef(node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT))
        db.commit()

        db.add(TaxonomyFieldDef(node_id=node.id, name="Karat", key="karat_2", type=FieldType.TEXT))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_name_unique_includes_archived(self, db: Session) -> None:
        """Archiving does NOT free the name within a node."""
        node = _make_node(db)
        db.add(
            TaxonomyFieldDef(
                node_id=node.id,
                name="Karat",
                key="karat",
                type=FieldType.TEXT,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.commit()

        db.add(
            TaxonomyFieldDef(
                node_id=node.id,
                name="Karat",
                key="karat_2",
                type=FieldType.TEXT,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_key_unique_per_node(self, db: Session) -> None:
        node = _make_node(db)
        db.add(TaxonomyFieldDef(node_id=node.id, name="Karat 18", key="karat", type=FieldType.TEXT))
        db.commit()

        db.add(
            TaxonomyFieldDef(
                node_id=node.id,
                name="Karat 22",  # different name
                key="karat",  # same key — DB rejects
                type=FieldType.TEXT,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_key_unique_includes_archived(self, db: Session) -> None:
        node = _make_node(db)
        db.add(
            TaxonomyFieldDef(
                node_id=node.id,
                name="Karat 18",
                key="karat",
                type=FieldType.TEXT,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db.commit()

        db.add(
            TaxonomyFieldDef(
                node_id=node.id,
                name="Karat 22",
                key="karat",
                type=FieldType.TEXT,
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_same_name_under_different_nodes_is_allowed(self, db: Session) -> None:
        a = _make_node(db, "Raw Materials")
        b = _make_node(db, "Tools")
        db.add_all(
            [
                TaxonomyFieldDef(
                    node_id=a.id,
                    name="Karat",
                    key="karat",
                    type=FieldType.TEXT,
                ),
                TaxonomyFieldDef(
                    node_id=b.id,
                    name="Karat",
                    key="karat",
                    type=FieldType.TEXT,
                ),
            ]
        )
        db.commit()  # must not raise

    def test_node_id_required(self, db: Session) -> None:
        f = TaxonomyFieldDef(name="Karat", key="karat", type=FieldType.TEXT)  # type: ignore[call-arg]
        db.add(f)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
