"""Unit tests for the C1 ``Checkout`` ORM model.

The route layer (C2 onward) will enforce open-checkout uniqueness, the
qty-vs-unique invariant, and the ``requires_checkout=True`` precondition.
This file is about storage shape only: defaults, FK behaviour, the
return-state derivation from ``returned_at``.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import (
    Checkout,
    Item,
    ItemUnit,
    ItemUnitStatus,
    Role,
    TaxonomyNode,
    TrackingMode,
    User,
    UserStatus,
)


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    # SQLite ignores FK constraints by default; turn them on so the FK tests
    # actually exercise RESTRICT / SET NULL semantics.
    with engine.connect() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))
        conn.commit()
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(
        bind=engine, autoflush=False, autocommit=False, future=True
    )
    with SessionLocal() as session:
        session.execute(text("PRAGMA foreign_keys=ON"))
        yield session


def _user(db: Session, email: str = "tools@x.test") -> User:
    u = User(
        google_sub=f"sub-{email}",
        email=email,
        name="T User",
        role=Role.WORKSHOP,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _item(
    db: Session,
    *,
    sku: str = "TOOL-1",
    tracking_mode: TrackingMode = TrackingMode.UNIQUE,
    requires_checkout: bool = True,
) -> Item:
    node = TaxonomyNode(name=f"Cat-{sku}")
    db.add(node)
    db.commit()
    db.refresh(node)
    item = Item(
        sku=sku,
        name=f"Item {sku}",
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=tracking_mode,
        requires_checkout=requires_checkout,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _unit(db: Session, item: Item, label: str = "U-1") -> ItemUnit:
    u = ItemUnit(
        item_id=item.id,
        serial_or_label=label,
        status=ItemUnitStatus.AVAILABLE,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


class TestCheckoutDefaults:
    def test_minimal_open_checkout_round_trips(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        checked_out = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)

        co = Checkout(
            item_id=item.id,
            user_id=user.id,
            checked_out_at=checked_out,
        )
        db.add(co)
        db.commit()
        db.refresh(co)

        assert co.id is not None
        assert co.item_id == item.id
        assert co.item_unit_id is None
        assert co.user_id == user.id
        # SQLite drops tzinfo on round-trip; compare naive forms.
        assert co.checked_out_at.replace(tzinfo=None) == checked_out.replace(
            tzinfo=None
        )
        assert co.expected_return is None
        assert co.returned_at is None
        assert co.condition_note is None
        assert co.created_at is not None
        assert co.updated_at is not None

    def test_checkout_with_unit_and_expected_return(
        self, db: Session
    ) -> None:
        item = _item(db)
        unit = _unit(db, item)
        user = _user(db)
        checked_out = datetime(2026, 5, 7, 9, 0, tzinfo=UTC)
        expected = checked_out + timedelta(days=7)

        co = Checkout(
            item_id=item.id,
            item_unit_id=unit.id,
            user_id=user.id,
            checked_out_at=checked_out,
            expected_return=expected,
            condition_note="Test handle ahead of weekend casting run",
        )
        db.add(co)
        db.commit()
        db.refresh(co)

        assert co.item_unit_id == unit.id
        assert co.expected_return is not None
        assert co.expected_return.replace(tzinfo=None) == expected.replace(
            tzinfo=None
        )
        assert co.condition_note == "Test handle ahead of weekend casting run"

    def test_returned_checkout_round_trips(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        checked_out = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
        returned = datetime(2026, 5, 5, 16, 30, tzinfo=UTC)

        co = Checkout(
            item_id=item.id,
            user_id=user.id,
            checked_out_at=checked_out,
            returned_at=returned,
            condition_note="Returned with chip on edge",
        )
        db.add(co)
        db.commit()
        db.refresh(co)

        assert co.returned_at is not None
        assert co.returned_at.replace(tzinfo=None) == returned.replace(
            tzinfo=None
        )

    def test_long_condition_note_round_trips(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        long_note = "x" * 1500  # within 2000-char column

        co = Checkout(
            item_id=item.id,
            user_id=user.id,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
            condition_note=long_note,
        )
        db.add(co)
        db.commit()
        db.refresh(co)

        assert co.condition_note == long_note


class TestCheckoutForeignKeys:
    def test_item_id_is_required(self, db: Session) -> None:
        user = _user(db)
        co = Checkout(  # type: ignore[call-arg]
            user_id=user.id,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )
        db.add(co)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_checked_out_at_is_required(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        co = Checkout(  # type: ignore[call-arg]
            item_id=item.id,
            user_id=user.id,
        )
        db.add(co)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_orphan_item_id_is_rejected(self, db: Session) -> None:
        user = _user(db)
        co = Checkout(
            item_id=999_999,
            user_id=user.id,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )
        db.add(co)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_orphan_item_unit_id_is_rejected(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        co = Checkout(
            item_id=item.id,
            item_unit_id=999_999,
            user_id=user.id,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )
        db.add(co)
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()

    def test_user_id_can_be_null(self, db: Session) -> None:
        """A historical checkout whose user was hard-deleted (rare) keeps its row."""
        item = _item(db, tracking_mode=TrackingMode.QTY)
        co = Checkout(
            item_id=item.id,
            user_id=None,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )
        db.add(co)
        db.commit()
        db.refresh(co)
        assert co.user_id is None


class TestCheckoutReturnedDerivation:
    """``returned_at IS NULL`` is the open / closed signal — no enum column."""

    def test_open_checkout_has_null_returned_at(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        co = Checkout(
            item_id=item.id,
            user_id=user.id,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )
        db.add(co)
        db.commit()
        db.refresh(co)
        assert co.returned_at is None

    def test_returned_at_can_be_set_after_creation(self, db: Session) -> None:
        item = _item(db, tracking_mode=TrackingMode.QTY)
        user = _user(db)
        co = Checkout(
            item_id=item.id,
            user_id=user.id,
            checked_out_at=datetime(2026, 5, 7, 9, 0, tzinfo=UTC),
        )
        db.add(co)
        db.commit()
        db.refresh(co)
        assert co.returned_at is None

        co.returned_at = datetime(2026, 5, 9, 10, 0, tzinfo=UTC)
        co.condition_note = "Returned cleanly"
        db.commit()
        db.refresh(co)
        assert co.returned_at is not None
