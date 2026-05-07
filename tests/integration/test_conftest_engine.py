"""Tests for the test-suite engine helpers in ``tests/conftest.py``.

These pin the DoD #11 progress step: ``TEST_DATABASE_URL`` env var override
with dialect-aware pool / connect_args. The helpers themselves are private
(``_resolve_test_database_url`` / ``_make_test_engine``) but are imported here
to verify the load-bearing behaviour. The default path stays SQLite in-memory
+ ``StaticPool`` so existing tests are unaffected.

Postgres-side helper checks use ``create_engine`` lazily — SQLAlchemy doesn't
connect until the first query, so we can build a Postgres engine without a
running Postgres server and inspect dialect + pool class.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool, StaticPool

from app.models import User, UserStatus
from tests.conftest import (
    _DEFAULT_TEST_DATABASE_URL,
    _make_test_engine,
    _resolve_test_database_url,
)


class TestResolveTestDatabaseUrl:
    def test_default_when_env_var_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
        assert _resolve_test_database_url() == "sqlite:///:memory:"
        # Default constant stays aligned with the documented fallback so a
        # future PR that drifts one without the other fails the suite.
        assert _DEFAULT_TEST_DATABASE_URL == "sqlite:///:memory:"

    def test_respects_test_database_url_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TEST_DATABASE_URL", "postgresql+psycopg:///test_uc")
        assert _resolve_test_database_url() == "postgresql+psycopg:///test_uc"

    def test_passes_arbitrary_url_through_verbatim(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        weird = "sqlite:///./tmp_some_file.db?cache=shared"
        monkeypatch.setenv("TEST_DATABASE_URL", weird)
        assert _resolve_test_database_url() == weird


class TestMakeTestEngine:
    def test_sqlite_memory_url_yields_sqlite_dialect(self) -> None:
        engine = _make_test_engine("sqlite:///:memory:")
        assert isinstance(engine, Engine)
        assert engine.dialect.name == "sqlite"

    def test_sqlite_memory_url_uses_static_pool(self) -> None:
        engine = _make_test_engine("sqlite:///:memory:")
        # StaticPool is required so the in-memory DB is visible across threads
        # (TestClient dispatches on a worker thread distinct from the test
        # thread).
        assert isinstance(engine.pool, StaticPool)

    def test_sqlite_file_url_also_uses_static_pool(self) -> None:
        # File-backed SQLite still benefits from StaticPool in tests so cross-
        # thread visibility doesn't require manual flushing.
        engine = _make_test_engine("sqlite:///./tmp_unused.db")
        assert isinstance(engine.pool, StaticPool)
        assert engine.dialect.name == "sqlite"

    def test_postgres_url_yields_postgres_dialect_lazily(self) -> None:
        # SQLAlchemy create_engine is lazy — building a Postgres engine doesn't
        # require a running Postgres server. The dialect is set from the URL.
        # Uses the psycopg v3 driver scheme (``postgresql+psycopg://``) since
        # that is the project's installed Postgres driver per pyproject.toml.
        engine = _make_test_engine(
            "postgresql+psycopg://user:pw@localhost:5432/test_uc"
        )
        assert engine.dialect.name == "postgresql"

    def test_postgres_url_does_not_use_static_pool(self) -> None:
        # Non-SQLite URLs use SQLAlchemy's default pool, which is QueuePool for
        # Postgres. StaticPool would defeat connection sharing across requests.
        engine = _make_test_engine(
            "postgresql+psycopg://user:pw@localhost:5432/test_uc"
        )
        assert not isinstance(engine.pool, StaticPool)
        assert isinstance(engine.pool, QueuePool)


class TestDbSessionFixtureStillWorks:
    """Sanity checks that the conftest refactor preserves the existing behaviour
    for SQLite (the default for every test in the suite). If these fail, the
    refactor regressed something every other test depends on.
    """

    def test_db_session_yields_writable_sqlite_session(
        self, db_session: Session
    ) -> None:
        # Engine the fixture handed us is a SQLite engine (default env config).
        bind = db_session.get_bind()
        assert isinstance(bind, Engine)
        assert bind.dialect.name == "sqlite"

        # Session is writable + readable round-trip.
        u = User(
            google_sub="conftest-engine-sub",
            email="conftest-engine@example.com",
            name="Conftest Engine",
            status=UserStatus.PENDING,
        )
        db_session.add(u)
        db_session.commit()

        round_tripped = (
            db_session.query(User).filter_by(email="conftest-engine@example.com").one()
        )
        assert round_tripped.id == u.id
        assert round_tripped.status is UserStatus.PENDING

    def test_db_session_engine_uses_static_pool(self, db_session: Session) -> None:
        # Forcing-function for a future PR that breaks the SQLite default's
        # cross-thread visibility (e.g. by switching to NullPool) — the
        # TestClient → fixture data-sharing contract depends on StaticPool.
        bind = db_session.get_bind()
        assert isinstance(bind, Engine)
        assert isinstance(bind.pool, StaticPool)
