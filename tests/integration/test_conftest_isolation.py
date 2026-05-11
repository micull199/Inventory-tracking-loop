"""Tests for ``_make_isolated_session`` + ``db_session`` dispatch in
``tests/conftest.py``.

These pin CONFTEST2's DoD #11 progress step: per-test isolation on shared
backends via the SQLAlchemy 2.0 SAVEPOINT pattern. The helper itself is private
(``_make_isolated_session``) but is imported here to verify the load-bearing
behaviour.

The savepoint pattern is dialect-agnostic — SQLAlchemy emits SAVEPOINT
statements that every supported backend handles. So SQLite-file engines drive
the validation tests without needing a running Postgres. The dispatch on URL
prefix is what decides which fixture path to use; the helper itself works the
same on any dialect.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Connection, Engine, create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import User, UserStatus
from tests.conftest import _make_isolated_session


@pytest.fixture
def sqlite_file_engine(tmp_path: Path) -> Iterator[Engine]:
    """A SQLite file engine for savepoint-pattern tests.

    SQLite memory + StaticPool would also work, but a file-backed engine is
    closer to the Postgres scenario (separate connections see committed state)
    and lets us re-open the engine after teardown to assert "the data is
    really gone".

    Installs the SQLAlchemy-recommended SQLite-fix events so the savepoint
    pattern works correctly on the pysqlite driver:
    https://docs.sqlalchemy.org/en/20/dialects/sqlite.html#serializable-isolation-savepoints-transactional-ddl
    Postgres has no such quirk so its engine doesn't need these events.
    """
    db_path = tmp_path / "isolation_check.db"
    url = f"sqlite:///{db_path}"
    engine = create_engine(url, future=True)

    @event.listens_for(engine, "connect")
    def _disable_pysqlite_isolation(dbapi_conn: object, _record: object) -> None:
        # Disables pysqlite's automatic BEGIN/COMMIT, letting SQLAlchemy
        # control transaction boundaries.
        dbapi_conn.isolation_level = None  # type: ignore[attr-defined]

    @event.listens_for(engine, "begin")
    def _begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN")

    yield engine
    engine.dispose()


def _open_fresh_session(engine: Engine) -> Session:
    """Open a non-savepointed session on the engine, to inspect what survived
    teardown of an isolated-session helper run."""
    SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)
    return SessionLocal()


class TestMakeIsolatedSession:
    def test_yields_writable_session_against_sqlite_file(self, sqlite_file_engine: Engine) -> None:
        with _make_isolated_session(sqlite_file_engine) as session:
            session.add(
                User(
                    google_sub="iso-sub-write",
                    email="iso-write@example.com",
                    name="Iso Write",
                    status=UserStatus.PENDING,
                )
            )
            session.flush()
            row = session.execute(
                select(User).filter_by(email="iso-write@example.com")
            ).scalar_one()
            assert row.google_sub == "iso-sub-write"

    def test_commit_does_not_escape_outer_transaction(self, sqlite_file_engine: Engine) -> None:
        # Inside the helper: write + commit. The commit operates on a SAVEPOINT
        # inside the outer transaction (because of join_transaction_mode=
        # "create_savepoint"), not on the outer transaction itself.
        with _make_isolated_session(sqlite_file_engine) as session:
            session.add(
                User(
                    google_sub="iso-sub-commit",
                    email="iso-commit@example.com",
                    name="Iso Commit",
                    status=UserStatus.PENDING,
                )
            )
            session.commit()

        # After teardown the outer transaction was rolled back. Open a fresh
        # session on the same engine and confirm the row never persisted.
        with _open_fresh_session(sqlite_file_engine) as fresh:
            assert (
                fresh.execute(select(User).filter_by(email="iso-commit@example.com")).first()
                is None
            )

    def test_rollback_inside_helper_preserves_outer_transaction(
        self, sqlite_file_engine: Engine
    ) -> None:
        # Write + rollback (pops the savepoint) + write a different row +
        # commit (new savepoint). After teardown, neither row should persist.
        with _make_isolated_session(sqlite_file_engine) as session:
            session.add(
                User(
                    google_sub="iso-sub-rollback-1",
                    email="iso-rollback-1@example.com",
                    name="Iso R1",
                    status=UserStatus.PENDING,
                )
            )
            session.rollback()

            session.add(
                User(
                    google_sub="iso-sub-rollback-2",
                    email="iso-rollback-2@example.com",
                    name="Iso R2",
                    status=UserStatus.PENDING,
                )
            )
            session.commit()

        with _open_fresh_session(sqlite_file_engine) as fresh:
            assert (
                fresh.execute(select(User).filter_by(email="iso-rollback-1@example.com")).first()
                is None
            )
            assert (
                fresh.execute(select(User).filter_by(email="iso-rollback-2@example.com")).first()
                is None
            )

    def test_integrity_error_recovery(self, sqlite_file_engine: Engine) -> None:
        # Mirrors the ~30 unit tests that commit-then-attempt-duplicate-then-
        # rollback. The savepoint pattern must let those keep working: the
        # IntegrityError rolls back the failed savepoint; calling rollback()
        # rolls back to the outer savepoint; further writes work on a fresh
        # savepoint.
        with _make_isolated_session(sqlite_file_engine) as session:
            session.add(
                User(
                    google_sub="iso-dup-1",
                    email="iso-dup@example.com",
                    name="Iso Dup",
                    status=UserStatus.PENDING,
                )
            )
            session.commit()

            session.add(
                User(
                    google_sub="iso-dup-2",
                    email="iso-dup@example.com",  # duplicate email, unique constraint
                    name="Iso Dup Two",
                    status=UserStatus.PENDING,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            # Session is usable for further writes after rollback.
            session.add(
                User(
                    google_sub="iso-dup-3",
                    email="iso-dup-recovered@example.com",
                    name="Iso Dup Three",
                    status=UserStatus.PENDING,
                )
            )
            session.commit()

            # Inside the helper, the recovered row IS visible.
            assert (
                session.execute(
                    select(User).filter_by(email="iso-dup-recovered@example.com")
                ).first()
                is not None
            )

        # And after teardown, every row from the test is gone.
        with _open_fresh_session(sqlite_file_engine) as fresh:
            for email in (
                "iso-dup@example.com",
                "iso-dup-recovered@example.com",
            ):
                assert fresh.execute(select(User).filter_by(email=email)).first() is None

    def test_uses_savepoint_mode(self) -> None:
        # Forcing function: a future PR that drops the join_transaction_mode
        # parameter (which would silently break isolation — route handler
        # commits would escape the outer transaction) fails the suite.
        source = inspect.getsource(_make_isolated_session)
        assert 'join_transaction_mode="create_savepoint"' in source

    def test_session_is_bound_to_a_connection_not_an_engine(
        self, sqlite_file_engine: Engine
    ) -> None:
        # The savepoint pattern requires the session bind to be a Connection so
        # the outer transaction can be controlled separately. Engine-bound
        # sessions would each open their own connection + transaction.
        with _make_isolated_session(sqlite_file_engine) as session:
            assert isinstance(session.get_bind(), Connection)


class TestDbSessionDispatch:
    """The ``db_session`` fixture chooses between the per-engine SQLite path and
    the savepoint path based on URL prefix. SQLite default behaviour (every
    other test in the suite) must stay unchanged.
    """

    def test_db_session_uses_sqlite_path_by_default(self, db_session: Session) -> None:
        # Default env config: TEST_DATABASE_URL unset → sqlite:///:memory: →
        # SQLite path → bind is an Engine (not a Connection).
        bind = db_session.get_bind()
        assert isinstance(bind, Engine)
        assert not isinstance(bind, Connection)
        assert bind.dialect.name == "sqlite"

    def test_db_session_dispatch_source_branches_on_url_prefix(self) -> None:
        # Source-text inspection: the dispatch logic checks the URL prefix
        # against "sqlite". A future PR that changes the dispatch (e.g. to a
        # different env var or a dialect check) would force a deliberate update
        # of this assertion + the documentation alongside it.
        from tests import conftest as conftest_module

        source = inspect.getsource(conftest_module.db_session)
        assert 'url.startswith("sqlite")' in source
        assert "_make_isolated_session" in source


class TestSavepointPatternBehaviour:
    """Behavioural pinning for the savepoint pattern at a slightly lower level
    than ``TestMakeIsolatedSession``. These complement those tests by exercising
    the connection-side state directly.
    """

    def test_outer_transaction_is_rolled_back_at_teardown(self, sqlite_file_engine: Engine) -> None:
        # Listen for ROLLBACK events on the underlying connection so we can
        # confirm the outer transaction's rollback actually fires when the
        # context manager exits.
        rollbacks: list[str] = []

        @event.listens_for(sqlite_file_engine, "rollback")
        def _on_rollback(_conn: Connection) -> None:
            rollbacks.append("rollback")

        with _make_isolated_session(sqlite_file_engine) as session:
            session.add(
                User(
                    google_sub="iso-rollback-event",
                    email="iso-rollback-event@example.com",
                    name="Iso RE",
                    status=UserStatus.PENDING,
                )
            )
            session.commit()  # commit-savepoint, not outer

        # The outer-transaction rollback fired during teardown.
        assert rollbacks, "expected at least one ROLLBACK at teardown"
