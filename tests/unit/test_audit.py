"""Unit tests for ``app.audit.record_audit`` and the JSON-coercion helper."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.audit import _to_jsonable, record_audit
from app.db import Base
from app.models import AuditLog, Role, User, UserStatus


@pytest.fixture
def db() -> Iterator[Session]:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _make_user(db: Session, *, email: str = "actor@x.test", role: Role = Role.ADMIN) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0],
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


class TestToJsonable:
    def test_passes_through_primitives(self) -> None:
        assert _to_jsonable(None) is None
        assert _to_jsonable("x") == "x"
        assert _to_jsonable(42) == 42
        assert _to_jsonable(3.5) == 3.5
        assert _to_jsonable(True) is True

    def test_collapses_enum_to_value(self) -> None:
        assert _to_jsonable(Role.MANAGER) == "manager"
        assert _to_jsonable(UserStatus.PENDING) == "pending"

    def test_serialises_datetime_to_isoformat(self) -> None:
        moment = datetime(2026, 5, 6, 12, 30, 0, tzinfo=UTC)
        assert _to_jsonable(moment) == moment.isoformat()

    def test_walks_dicts_recursively(self) -> None:
        before = {"role": Role.ADMIN, "nested": {"status": UserStatus.DISABLED, "ok": True}}
        assert _to_jsonable(before) == {
            "role": "admin",
            "nested": {"status": "disabled", "ok": True},
        }

    def test_walks_lists_recursively(self) -> None:
        assert _to_jsonable([Role.OFFICE, Role.WORKSHOP]) == ["office", "workshop"]


class TestRecordAudit:
    def test_writes_row_with_actor_and_payload(self, db: Session) -> None:
        actor = _make_user(db)

        entry = record_audit(
            db,
            actor=actor,
            action="user.role_assigned",
            entity_type="user",
            entity_id=actor.id,
            before={"role": None},
            after={"role": Role.MANAGER},
        )
        db.commit()

        assert entry.id is not None
        fetched = db.get(AuditLog, entry.id)
        assert fetched is not None
        assert fetched.actor_id == actor.id
        assert fetched.action == "user.role_assigned"
        assert fetched.entity_type == "user"
        assert fetched.entity_id == actor.id
        assert fetched.before_json == {"role": None}
        # Enum coerced to its string value (matches how the User table stores it).
        assert fetched.after_json == {"role": "manager"}
        assert fetched.created_at is not None

    def test_actor_none_is_persisted_for_system_events(self, db: Session) -> None:
        target = _make_user(db, email="target@x.test", role=Role.WORKSHOP)

        entry = record_audit(
            db,
            actor=None,
            action="user.bootstrap_admin_granted",
            entity_type="user",
            entity_id=target.id,
            before={"role": None, "status": UserStatus.PENDING},
            after={"role": Role.ADMIN, "status": UserStatus.ACTIVE},
        )
        db.commit()

        fetched = db.get(AuditLog, entry.id)
        assert fetched is not None
        assert fetched.actor_id is None
        assert fetched.before_json == {"role": None, "status": "pending"}
        assert fetched.after_json == {"role": "admin", "status": "active"}

    def test_before_after_default_to_none(self, db: Session) -> None:
        actor = _make_user(db)
        entry = record_audit(
            db,
            actor=actor,
            action="user.created",
            entity_type="user",
            entity_id=actor.id,
        )
        db.commit()
        fetched = db.get(AuditLog, entry.id)
        assert fetched is not None
        assert fetched.before_json is None
        assert fetched.after_json is None

    def test_round_trips_through_json_column(self, db: Session) -> None:
        """Ensure SQLAlchemy's JSON column can round-trip the coerced payload.

        Without ``_to_jsonable`` collapsing the enum, the raw enum object would
        fail to serialise. This test would catch a regression where the helper
        stops normalising before insert.
        """
        actor = _make_user(db)
        record_audit(
            db,
            actor=actor,
            action="x",
            entity_type="user",
            entity_id=actor.id,
            after={"role": Role.OFFICE, "status": UserStatus.ACTIVE},
        )
        db.commit()
        db.expire_all()

        rows = db.execute(select(AuditLog)).scalars().all()
        assert len(rows) == 1
        assert rows[0].after_json == {"role": "office", "status": "active"}
