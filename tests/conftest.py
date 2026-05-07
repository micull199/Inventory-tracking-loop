"""Shared fixtures for unit + integration tests.

These do NOT apply to the e2e suite (which has its own conftest spinning up a
real uvicorn against a file-backed SQLite). Each test here gets an isolated
database with a fresh schema.

The fixture engine URL is resolved from the ``TEST_DATABASE_URL`` env var,
defaulting to ``sqlite:///:memory:``. Setting ``TEST_DATABASE_URL`` to a
Postgres URL (e.g. ``postgresql+psycopg:///test_uc``) lets a developer smoke-
test the suite against Postgres without code changes — DoD #11's "runs in
cloud config on Postgres with no code changes (env vars only)" half.

Per-test isolation strategy:

- **SQLite (default)**: each test creates its own engine pointing at a fresh
  in-memory or file DB. Isolation is automatic; no transaction wrapping needed.
- **Non-SQLite (Postgres)**: each test reuses the shared backend, so isolation
  needs the SQLAlchemy 2.0 "outer transaction + savepoint" pattern. The session
  is bound to a connection inside an outer transaction; ``session.commit()``
  and ``session.rollback()`` operate on a SAVEPOINT (via
  ``join_transaction_mode="create_savepoint"``) so they never escape; teardown
  rolls back the outer transaction, discarding all the test's writes.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

# Force a known-good config before any app imports happen.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-fixed-for-tests")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import app

_DEFAULT_TEST_DATABASE_URL = "sqlite:///:memory:"


def _resolve_test_database_url() -> str:
    """Resolve the test-fixture engine URL from ``TEST_DATABASE_URL`` env var.

    Defaults to ``sqlite:///:memory:`` when unset. Decoupled from the app's
    ``DATABASE_URL`` (which drives ``app.config.settings.database_url``) so a
    developer can point only the fixture at Postgres for a smoke test.
    """
    return os.environ.get("TEST_DATABASE_URL", _DEFAULT_TEST_DATABASE_URL)


def _make_test_engine(url: str) -> Engine:
    """Build a fixture engine with dialect-aware pool / connect_args.

    SQLite URLs (the default) need ``StaticPool`` + ``check_same_thread=False``
    so the in-memory DB is visible across threads (``TestClient`` dispatches on
    a worker thread distinct from the test thread). Non-SQLite URLs use
    SQLAlchemy's default pool (``QueuePool``) with no special connect_args.

    Mirrors ``app/db.py::_engine_kwargs`` so app and fixture stay aligned on
    the dialect-aware pattern.
    """
    if url.startswith("sqlite"):
        return create_engine(
            url,
            future=True,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_engine(url, future=True)


@contextmanager
def _make_isolated_session(engine: Engine) -> Iterator[Session]:
    """Yield a session whose writes never escape an outer transaction.

    Used for shared-backend testing (Postgres) where each test must roll back
    its writes for isolation. The pattern: open a connection, begin an outer
    transaction, bind a sessionmaker to that connection with
    ``join_transaction_mode="create_savepoint"`` so the route handler's
    ``session.commit()`` / ``session.rollback()`` operate on a SAVEPOINT inside
    the outer transaction. At teardown, roll back the outer transaction —
    every committed savepoint is discarded, no rows persist.

    The helper is dialect-agnostic (SAVEPOINTs are supported on every
    SQLAlchemy-supported backend that matters for this project), so SQLite-file
    engines can drive validation tests without needing a running Postgres.
    """
    Base.metadata.create_all(engine)
    connection = engine.connect()
    transaction = connection.begin()
    SessionLocal = sessionmaker(
        bind=connection,
        autoflush=False,
        autocommit=False,
        future=True,
        join_transaction_mode="create_savepoint",
    )
    try:
        with SessionLocal() as session:
            yield session
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()


@pytest.fixture
def db_session() -> Iterator[Session]:
    """An isolated database session with all tables created.

    SQLite URLs (the default): each test gets its own engine pointing at a
    fresh in-memory or file DB. Isolation is automatic.

    Non-SQLite URLs (e.g. Postgres): each test reuses the shared backend, so
    the savepoint pattern wraps the session in an outer transaction that's
    rolled back at teardown. See ``_make_isolated_session``.
    """
    url = _resolve_test_database_url()
    engine = _make_test_engine(url)
    if url.startswith("sqlite"):
        Base.metadata.create_all(engine)
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with SessionLocal() as session:
            yield session
        return
    with _make_isolated_session(engine) as session:
        yield session


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient with ``get_session`` overridden to share the test session.

    Binds ``OverrideSession`` to whatever ``db_session`` is bound to:

    - SQLite path: the engine. Each route-handler request opens its own session
      on the engine — same DB visible because of StaticPool's single
      connection.
    - Savepoint path: the connection. Each route-handler request opens its own
      session that joins the same outer transaction via
      ``join_transaction_mode="create_savepoint"``. Route-handler commits land
      inside the test's savepoint and are discarded at teardown.
    """
    bind = db_session.get_bind()
    sessionmaker_kwargs: dict[str, object] = {
        "bind": bind,
        "autoflush": False,
        "autocommit": False,
        "future": True,
    }
    if isinstance(bind, Connection):
        sessionmaker_kwargs["join_transaction_mode"] = "create_savepoint"
    OverrideSession = sessionmaker(**sessionmaker_kwargs)  # type: ignore[arg-type]

    def _override() -> Iterator[Session]:
        with OverrideSession() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
