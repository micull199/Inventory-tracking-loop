"""Unit tests for ``app.app_settings_store``.

Settings are operator-tunable: get-with-default semantics keep the app
running when a key is missing / unparseable rather than 500ing the
routes that depend on it. Tests pin those branches.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.app_settings_store import (
    STONES_COLOURED_STONE_CT_THRESHOLD,
    STONES_COST_FLOOR_AUD,
    get_setting_decimal,
    stones_coloured_stone_ct_threshold,
    stones_cost_floor_aud,
)
from app.db import Base
from app.models import AppSetting


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


class TestGetSettingDecimal:
    def test_returns_parsed_value(self, db: Session) -> None:
        db.add(AppSetting(key="x.y.z", value="42.5"))
        db.commit()
        assert get_setting_decimal(db, "x.y.z", Decimal("0")) == Decimal("42.5")

    def test_returns_default_when_missing(self, db: Session) -> None:
        assert get_setting_decimal(db, "no.such.key", Decimal("9.99")) == Decimal("9.99")

    def test_returns_default_when_unparseable(self, db: Session) -> None:
        db.add(AppSetting(key="x.y.z", value="not-a-number"))
        db.commit()
        # Falls back rather than raising — keeps the route up while
        # somebody fixes the corrupted setting.
        assert get_setting_decimal(db, "x.y.z", Decimal("1")) == Decimal("1")


class TestStonesAccessors:
    def test_cost_floor_default(self, db: Session) -> None:
        assert stones_cost_floor_aud(db) == Decimal("500")

    def test_cost_floor_overridden(self, db: Session) -> None:
        db.add(AppSetting(key=STONES_COST_FLOOR_AUD, value="250"))
        db.commit()
        assert stones_cost_floor_aud(db) == Decimal("250")

    def test_coloured_stone_threshold_default(self, db: Session) -> None:
        assert stones_coloured_stone_ct_threshold(db) == Decimal("0.50")

    def test_coloured_stone_threshold_overridden(self, db: Session) -> None:
        db.add(AppSetting(key=STONES_COLOURED_STONE_CT_THRESHOLD, value="1.00"))
        db.commit()
        assert stones_coloured_stone_ct_threshold(db) == Decimal("1.00")
