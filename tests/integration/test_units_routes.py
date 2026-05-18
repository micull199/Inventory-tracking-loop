"""Integration tests for ``/admin/units`` CRUD.

Mirrors the stone-shapes admin test shape — units is the simplest S5
lookup (code + name + sort_order). Spot checks the unique-across-
archived semantics and audit diff content.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, Unit, User, UserStatus


def _make_user(db: Session, *, email: str, role: Role | None = None) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        status=UserStatus.ACTIVE,
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
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.entity_type == "unit").order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


class TestRoleEnforcement:
    def test_anonymous_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/units").status_code == 401

    def test_workshop_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        assert client.get("/admin/units").status_code == 403

    def test_manager_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        assert client.get("/admin/units").status_code == 200


class TestCreate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/units",
            data={
                "code": "OZ",  # uppercase input — should be lowercased
                "name": "ounce",
                "sort_order": "11",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = db_session.execute(select(Unit).where(Unit.code == "oz")).scalar_one()
        assert row.code == "oz"
        assert row.sort_order == 11
        audit = _audit_rows(db_session, action="unit.created")
        assert len(audit) == 1

    def test_blank_code_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/units",
            data={"code": "   ", "name": "x", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_duplicate_code_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Unit(code="oz", name="ounce"))
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/units",
            data={"code": "oz", "name": "ounce 2", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_archived_code_still_blocks(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            Unit(
                code="oz",
                name="ounce-archived",
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/units",
            data={"code": "oz", "name": "ounce-new", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestEdit:
    def test_happy_update(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        unit = Unit(code="oz", name="ounce")
        db_session.add(unit)
        db_session.commit()
        db_session.refresh(unit)
        _login_as(client, u)
        resp = client.post(
            f"/admin/units/{unit.id}",
            data={
                "code": "oz",
                "name": "Ounce (renamed)",
                "sort_order": "12",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.name == "Ounce (renamed)"
        assert unit.sort_order == 12

    def test_noop_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        unit = Unit(code="oz", name="ounce")
        db_session.add(unit)
        db_session.commit()
        db_session.refresh(unit)
        _login_as(client, u)
        resp = client.post(
            f"/admin/units/{unit.id}",
            data={
                "code": "oz",
                "name": "ounce",
                "sort_order": "0",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="unit.updated") == []


class TestArchive:
    def test_archive_unarchive_cycle(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        unit = Unit(code="oz", name="ounce")
        db_session.add(unit)
        db_session.commit()
        db_session.refresh(unit)
        _login_as(client, u)
        resp = client.post(
            f"/admin/units/{unit.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.archived_at is not None
        resp = client.post(
            f"/admin/units/{unit.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.archived_at is None
