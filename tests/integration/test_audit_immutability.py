"""DB-level immutability tests: UPDATE and DELETE on ``audit_log`` must fail.

These exercise the SQLite triggers installed by ``apply_immutability_triggers``.
The Postgres equivalents use the same helper and the same SQL pattern, so this
test plus the helper's own dialect-dispatch logic is sufficient coverage for v1.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.audit import apply_immutability_triggers, record_audit
from app.db import Base
from app.models import AuditLog, Role, User, UserStatus


@pytest.fixture
def db_with_triggers() -> Iterator[Session]:
    """In-memory SQLite + ``apply_immutability_triggers`` installed."""
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        apply_immutability_triggers(conn)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


def _make_actor(db: Session) -> User:
    user = User(
        google_sub="g-actor",
        email="actor@x.test",
        name="Actor",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _seed_audit_row(db: Session) -> AuditLog:
    actor = _make_actor(db)
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
    return entry


class TestAuditLogImmutability:
    def test_inserts_still_work(self, db_with_triggers: Session) -> None:
        """Sanity check: triggers must not block INSERTs."""
        entry = _seed_audit_row(db_with_triggers)
        assert entry.id is not None

    def test_update_via_orm_is_blocked(self, db_with_triggers: Session) -> None:
        entry = _seed_audit_row(db_with_triggers)
        entry.action = "tampered"
        with pytest.raises(IntegrityError) as exc:
            db_with_triggers.commit()
        assert "audit_log is append-only" in str(exc.value)
        db_with_triggers.rollback()

    def test_update_via_core_statement_is_blocked(self, db_with_triggers: Session) -> None:
        entry = _seed_audit_row(db_with_triggers)
        # SQLite fires BEFORE-triggers as the statement runs, so ``execute`` raises
        # before we even reach commit.
        stmt = update(AuditLog).where(AuditLog.id == entry.id).values(action="tampered")
        with pytest.raises(IntegrityError) as exc:
            db_with_triggers.execute(stmt)
        assert "audit_log is append-only" in str(exc.value)
        db_with_triggers.rollback()

    def test_delete_via_orm_is_blocked(self, db_with_triggers: Session) -> None:
        entry = _seed_audit_row(db_with_triggers)
        db_with_triggers.delete(entry)
        with pytest.raises(IntegrityError) as exc:
            db_with_triggers.commit()
        assert "audit_log is append-only" in str(exc.value)
        db_with_triggers.rollback()

    def test_delete_via_core_statement_is_blocked(self, db_with_triggers: Session) -> None:
        entry = _seed_audit_row(db_with_triggers)
        stmt = delete(AuditLog).where(AuditLog.id == entry.id)
        with pytest.raises(IntegrityError) as exc:
            db_with_triggers.execute(stmt)
        assert "audit_log is append-only" in str(exc.value)
        db_with_triggers.rollback()

    def test_row_persists_after_blocked_attempts(self, db_with_triggers: Session) -> None:
        entry = _seed_audit_row(db_with_triggers)
        entry_id = entry.id

        stmt = delete(AuditLog).where(AuditLog.id == entry_id)
        with pytest.raises(IntegrityError):
            db_with_triggers.execute(stmt)
        db_with_triggers.rollback()

        # The row still exists, unchanged.
        survivor = db_with_triggers.get(AuditLog, entry_id)
        assert survivor is not None
        assert survivor.action == "user.role_assigned"
