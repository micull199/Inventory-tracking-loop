"""Shared fixtures for unit + integration tests.

These do NOT apply to the e2e suite (which has its own conftest spinning up a
real uvicorn against a file-backed SQLite). Each test here gets an isolated
database with a fresh schema.

The fixture engine URL is resolved from the ``TEST_DATABASE_URL`` env var,
defaulting to ``sqlite:///:memory:``. Setting ``TEST_DATABASE_URL`` to a
Postgres URL (e.g. ``postgresql:///test_uc``) lets a developer smoke-test the
suite against Postgres without code changes — DoD #11's "runs in cloud config
on Postgres with no code changes (env vars only)" half. Per-test isolation on
a shared Postgres backend (transaction rollback or schema-per-test) is a
separate follow-up.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

# Force a known-good config before any app imports happen.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SECRET_KEY", "test-secret-key-fixed-for-tests")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
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


@pytest.fixture
def db_session() -> Iterator[Session]:
    """An isolated database session with all tables created.

    Defaults to in-memory SQLite (one engine per test) so existing tests stay
    isolated. Honours ``TEST_DATABASE_URL`` for Postgres smoke tests; per-test
    isolation on a shared Postgres backend is a follow-up.
    """
    engine = _make_test_engine(_resolve_test_database_url())
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    with SessionLocal() as session:
        yield session


@pytest.fixture
def client(db_session: Session) -> Iterator[TestClient]:
    """A TestClient with ``get_session`` overridden to share the test session.

    Uses the same engine as ``db_session`` so writes done via the API are
    visible to assertions made directly through ``db_session``.
    """
    bind = db_session.get_bind()
    OverrideSession = sessionmaker(bind=bind, autoflush=False, autocommit=False, future=True)

    def _override() -> Iterator[Session]:
        with OverrideSession() as s:
            yield s

    app.dependency_overrides[get_session] = _override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
