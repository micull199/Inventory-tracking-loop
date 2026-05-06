"""Shared fixtures for unit + integration tests.

These do NOT apply to the e2e suite (which has its own conftest spinning up a
real uvicorn against a file-backed SQLite). Each test here gets an in-memory
database with a fresh schema.
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
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base, get_session
from app.main import app


@pytest.fixture
def db_session() -> Iterator[Session]:
    """An isolated in-memory SQLite session with all tables created.

    ``StaticPool`` + ``check_same_thread=False`` keeps the same in-memory
    database visible across threads, which TestClient relies on (it dispatches
    requests on a worker thread distinct from the test thread).
    """
    engine = create_engine(
        "sqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
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
