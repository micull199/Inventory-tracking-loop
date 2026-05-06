"""Unit tests for ``upsert_user_from_userinfo`` — bootstrap admin + idempotent upsert."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.auth import upsert_user_from_userinfo
from app.db import Base
from app.models import Role, User, UserStatus


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _userinfo(sub: str, email: str, name: str = "Pat Tester") -> dict[str, str]:
    return {"sub": sub, "email": email, "name": name}


class TestUpsertUser:
    def test_creates_pending_user_for_unknown_sub(self, db: Session) -> None:
        user = upsert_user_from_userinfo(
            db,
            _userinfo("g-100", "pat@example.com"),
            bootstrap_admin_email=None,
        )
        db.commit()

        assert user.id is not None
        assert user.google_sub == "g-100"
        assert user.email == "pat@example.com"
        assert user.name == "Pat Tester"
        assert user.role is None
        assert user.status is UserStatus.PENDING

    def test_second_call_with_same_sub_updates_email_and_name(self, db: Session) -> None:
        u1 = upsert_user_from_userinfo(
            db, _userinfo("g-200", "old@example.com", "Old Name"), bootstrap_admin_email=None
        )
        db.commit()
        original_id = u1.id

        u2 = upsert_user_from_userinfo(
            db, _userinfo("g-200", "new@example.com", "New Name"), bootstrap_admin_email=None
        )
        db.commit()

        assert u2.id == original_id
        assert u2.email == "new@example.com"
        assert u2.name == "New Name"
        # Existing role/status are preserved on re-login.
        assert u2.status is UserStatus.PENDING
        assert db.query(User).count() == 1

    def test_bootstrap_admin_email_promotes_to_admin_active(self, db: Session) -> None:
        user = upsert_user_from_userinfo(
            db,
            _userinfo("g-300", "boss@uc.example"),
            bootstrap_admin_email="boss@uc.example",
        )
        db.commit()

        assert user.role is Role.ADMIN
        assert user.status is UserStatus.ACTIVE

    def test_bootstrap_admin_match_is_case_insensitive(self, db: Session) -> None:
        user = upsert_user_from_userinfo(
            db,
            _userinfo("g-301", "Boss@UC.Example"),
            bootstrap_admin_email="boss@uc.example",
        )
        db.commit()

        assert user.role is Role.ADMIN
        assert user.status is UserStatus.ACTIVE

    def test_non_bootstrap_email_is_not_promoted(self, db: Session) -> None:
        user = upsert_user_from_userinfo(
            db,
            _userinfo("g-400", "rando@example.com"),
            bootstrap_admin_email="boss@uc.example",
        )
        db.commit()

        assert user.role is None
        assert user.status is UserStatus.PENDING

    def test_bootstrap_does_not_fire_when_an_admin_already_exists(self, db: Session) -> None:
        """Bootstrap is a one-shot seed. Once any admin exists, it stops promoting.

        Reason: ``BOOTSTRAP_ADMIN_EMAIL`` is meant to seed the very first admin only.
        If the env var is later changed (or left set after the seed), a different
        person matching the new value must NOT be silently promoted — admin access
        from that point on must be granted explicitly via the admin UI.
        """
        # First admin is seeded the normal way.
        first = upsert_user_from_userinfo(
            db,
            _userinfo("g-600", "first-admin@uc.example"),
            bootstrap_admin_email="first-admin@uc.example",
        )
        db.commit()
        assert first.role is Role.ADMIN

        # Operator changes the bootstrap email afterwards. New user signs in.
        second = upsert_user_from_userinfo(
            db,
            _userinfo("g-601", "second-admin@uc.example"),
            bootstrap_admin_email="second-admin@uc.example",
        )
        db.commit()

        # Bootstrap slot is already filled — second is just pending.
        assert second.role is None
        assert second.status is UserStatus.PENDING
