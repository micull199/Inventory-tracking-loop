"""Integration tests: every existing mutating route writes the right audit rows.

Covers the routes wired up in slice F3:
- First sign-in via ``upsert_user_from_userinfo`` writes ``user.created``.
- Bootstrap admin first sign-in additionally writes ``user.bootstrap_admin_granted``.
- ``POST /admin/users/{id}/role`` writes ``user.role_assigned`` with before/after.
- ``POST /admin/users/{id}/status`` writes ``user.status_changed``.
- No audit row is written when the requested change is a no-op (idempotent POST).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import upsert_user_from_userinfo
from app.models import AuditLog, Role, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None = None,
    status: UserStatus = UserStatus.PENDING,
) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0],
        role=role,
        status=status,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _login_as(client: TestClient, user: User) -> None:
    resp = client.post(
        "/auth/_dev-login",
        data={"email": user.email, "sub": user.google_sub},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _csrf(client: TestClient) -> str:
    """Return the active CSRF token, bootstrapping the cookie if needed."""
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


class TestUpsertUserAudit:
    def test_first_sign_in_writes_user_created(self, db_session: Session) -> None:
        user = upsert_user_from_userinfo(
            db_session,
            {"sub": "g-1", "email": "newbie@x.test", "name": "Newbie"},
            bootstrap_admin_email=None,
        )
        db_session.commit()

        rows = _audit_rows(db_session, action="user.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == user.id  # user is the actor of their own creation
        assert row.entity_type == "user"
        assert row.entity_id == user.id
        assert row.before_json is None
        assert row.after_json == {
            "email": "newbie@x.test",
            "role": None,
            "status": "pending",
        }

    def test_bootstrap_admin_first_sign_in_writes_two_rows(self, db_session: Session) -> None:
        user = upsert_user_from_userinfo(
            db_session,
            {"sub": "g-2", "email": "boss@x.test", "name": "Boss"},
            bootstrap_admin_email="boss@x.test",
        )
        db_session.commit()

        created = _audit_rows(db_session, action="user.created")
        granted = _audit_rows(db_session, action="user.bootstrap_admin_granted")

        assert len(created) == 1
        assert created[0].actor_id == user.id
        assert created[0].after_json == {
            "email": "boss@x.test",
            "role": "admin",
            "status": "active",
        }

        assert len(granted) == 1
        # System action — no actor.
        assert granted[0].actor_id is None
        assert granted[0].before_json == {"role": None, "status": "pending"}
        assert granted[0].after_json == {"role": "admin", "status": "active"}

    def test_returning_user_does_not_write_user_created(self, db_session: Session) -> None:
        upsert_user_from_userinfo(
            db_session,
            {"sub": "g-3", "email": "back@x.test", "name": "Back"},
            bootstrap_admin_email=None,
        )
        db_session.commit()

        upsert_user_from_userinfo(
            db_session,
            {"sub": "g-3", "email": "back@x.test", "name": "Back Renamed"},
            bootstrap_admin_email=None,
        )
        db_session.commit()

        # Still exactly one user.created row across both sign-ins.
        assert len(_audit_rows(db_session, action="user.created")) == 1


class TestRoleChangeAudit:
    def test_role_assignment_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="t@x.test")
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "manager", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="user.role_assigned")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == admin.id
        assert row.entity_type == "user"
        assert row.entity_id == target.id
        assert row.before_json == {"role": None}
        assert row.after_json == {"role": "manager"}

    def test_clearing_role_records_before_and_after(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="ex@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="user.role_assigned")
        assert len(rows) == 1
        assert rows[0].before_json == {"role": "office"}
        assert rows[0].after_json == {"role": None}

    def test_no_audit_row_when_role_unchanged(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="t@x.test", role=Role.WORKSHOP, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "workshop", "csrf_token": _csrf(client)},  # same as current
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="user.role_assigned") == []

    def test_invalid_role_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(db_session, email="t@x.test")
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/role",
            data={"role": "wizard", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _audit_rows(db_session, action="user.role_assigned") == []


class TestStatusChangeAudit:
    def test_status_change_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="t@x.test", role=Role.WORKSHOP, status=UserStatus.PENDING
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "active", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="user.status_changed")
        assert len(rows) == 1
        assert rows[0].actor_id == admin.id
        assert rows[0].entity_id == target.id
        assert rows[0].before_json == {"status": "pending"}
        assert rows[0].after_json == {"status": "active"}

    def test_no_audit_row_when_status_unchanged(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        target = _make_user(
            db_session, email="t@x.test", role=Role.OFFICE, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{target.id}/status",
            data={"status": "active", "csrf_token": _csrf(client)},  # already active
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="user.status_changed") == []

    def test_blocked_self_status_change_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(
            db_session, email="admin@x.test", role=Role.ADMIN, status=UserStatus.ACTIVE
        )
        _login_as(client, admin)

        resp = client.post(
            f"/admin/users/{admin.id}/status",
            data={"status": "disabled", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _audit_rows(db_session, action="user.status_changed") == []
