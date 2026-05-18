"""Integration tests for the Manager-owned ``/admin/stone-shapes`` CRUD routes.

Mirrors ``test_locations_routes.py`` — the route shape is intentionally
identical (simpler than suppliers: just name + sort_order). Covers role
enforcement, list filters, create / edit / archive happy paths, audit
diff content, archive-doesn't-free-the-name semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, StoneShape, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None = None,
    status: UserStatus = UserStatus.ACTIVE,
) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0].title(),
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
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "stone_shape")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/stone-shapes")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/admin/stone-shapes")
        assert resp.status_code == 403

    def test_office_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/stone-shapes")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/stone-shapes")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/stone-shapes")
        assert resp.status_code == 200


class TestList:
    def test_active_default(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(StoneShape(name="round", sort_order=0))
        db_session.add(
            StoneShape(name="oval", sort_order=1, archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/stone-shapes")
        assert resp.status_code == 200
        assert "round" in resp.text
        assert "oval" not in resp.text

    def test_archived_tab(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(StoneShape(name="round", sort_order=0))
        db_session.add(
            StoneShape(name="oval", sort_order=1, archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/stone-shapes?show=archived")
        assert resp.status_code == 200
        assert "oval" in resp.text
        assert "round" not in resp.text


class TestCreate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/stone-shapes",
            data={"name": "double_radiant", "sort_order": "5", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = db_session.execute(
            select(StoneShape).where(StoneShape.name == "double_radiant")
        ).scalar_one()
        assert row.sort_order == 5
        audit = _audit_rows(db_session, action="stone_shape.created")
        assert len(audit) == 1
        assert audit[0].after_json == {"name": "double_radiant", "sort_order": 5}

    def test_blank_name_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/stone-shapes",
            data={"name": "   ", "sort_order": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(StoneShape)).first() is None

    def test_duplicate_name_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(StoneShape(name="round", sort_order=0))
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/stone-shapes",
            data={"name": "round", "sort_order": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_duplicate_name_rejected_even_if_other_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            StoneShape(
                name="round", sort_order=0, archived_at=datetime(2026, 1, 1, tzinfo=UTC)
            )
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/stone-shapes",
            data={"name": "round", "sort_order": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        # Archiving doesn't free the name — same posture as suppliers/locations.
        assert resp.status_code == 400

    def test_bad_sort_order_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/stone-shapes",
            data={"name": "trillion", "sort_order": "not-a-number", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestEdit:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = StoneShape(name="round", sort_order=0)
        db_session.add(shape)
        db_session.commit()
        db_session.refresh(shape)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stone-shapes/{shape.id}",
            data={"name": "round_brilliant", "sort_order": "1", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(shape)
        assert shape.name == "round_brilliant"
        assert shape.sort_order == 1
        audit = _audit_rows(db_session, action="stone_shape.updated")
        assert len(audit) == 1
        # Diff records only changed fields.
        assert audit[0].before_json == {"name": "round", "sort_order": 0}
        assert audit[0].after_json == {"name": "round_brilliant", "sort_order": 1}

    def test_noop_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = StoneShape(name="round", sort_order=0)
        db_session.add(shape)
        db_session.commit()
        db_session.refresh(shape)
        _login_as(client, u)
        # Submit the same values — no diff, no audit row.
        resp = client.post(
            f"/admin/stone-shapes/{shape.id}",
            data={"name": "round", "sort_order": "0", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="stone_shape.updated") == []

    def test_404_on_unknown_id(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/stone-shapes/9999",
            data={"name": "round", "sort_order": "0", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


class TestArchive:
    def test_archive_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = StoneShape(name="round", sort_order=0)
        db_session.add(shape)
        db_session.commit()
        db_session.refresh(shape)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stone-shapes/{shape.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(shape)
        assert shape.archived_at is not None
        assert len(_audit_rows(db_session, action="stone_shape.archived")) == 1

    def test_archive_idempotent(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = StoneShape(
            name="round", sort_order=0, archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(shape)
        db_session.commit()
        db_session.refresh(shape)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stone-shapes/{shape.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Second archive is a no-op — no extra audit row.
        assert _audit_rows(db_session, action="stone_shape.archived") == []

    def test_unarchive_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = StoneShape(
            name="round", sort_order=0, archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(shape)
        db_session.commit()
        db_session.refresh(shape)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stone-shapes/{shape.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(shape)
        assert shape.archived_at is None
        assert len(_audit_rows(db_session, action="stone_shape.unarchived")) == 1
