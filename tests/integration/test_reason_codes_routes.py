"""Integration tests for ``/admin/reason-codes`` CRUD.

Reason codes are type-scoped — the same ``code`` (e.g. ``damaged``)
can exist on multiple movement types. Tests cover the per-type
uniqueness invariant, the movement_type filter, and the archive
lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, ReasonCode, Role, User, UserStatus


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


def _audit(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "reason_code")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


class TestRoleEnforcement:
    def test_anonymous_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/reason-codes").status_code == 401

    def test_workshop_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        assert client.get("/admin/reason-codes").status_code == 403

    def test_manager_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        assert client.get("/admin/reason-codes").status_code == 200


class TestCreate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/reason-codes",
            data={
                "movement_type": "adjustment",
                "code": "FOUND",  # uppercase input — should be lowercased
                "label": "Found during stock-take",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = db_session.execute(select(ReasonCode)).scalar_one()
        assert row.code == "found"
        assert row.movement_type == "adjustment"
        assert len(_audit(db_session, action="reason_code.created")) == 1

    def test_same_code_different_type_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Same ``code`` on different movement types is fine — that's the
        whole point of type-scoping."""
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            ReasonCode(movement_type="out", code="damaged", label="Damaged (out)")
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/reason-codes",
            data={
                "movement_type": "adjustment",
                "code": "damaged",
                "label": "Damaged (adjustment)",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = list(db_session.execute(select(ReasonCode)).scalars().all())
        assert len(rows) == 2

    def test_same_code_same_type_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            ReasonCode(movement_type="out", code="damaged", label="Damaged")
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/reason-codes",
            data={
                "movement_type": "out",
                "code": "damaged",
                "label": "Damaged 2",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unknown_movement_type_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/reason-codes",
            data={
                "movement_type": "definitely-not-a-type",
                "code": "x",
                "label": "x",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestList:
    def test_filter_by_movement_type(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                ReasonCode(movement_type="out", code="sale", label="Sale"),
                ReasonCode(
                    movement_type="adjustment", code="found", label="Found"
                ),
            ]
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/reason-codes?movement_type=out")
        assert resp.status_code == 200
        assert "sale" in resp.text.lower()
        # The "found" code shouldn't appear in the filtered list.
        # (The select widget has "found" as an enum option *header*
        # nowhere — only as a table cell — so checking the table row
        # marker is safer than a raw substring.)
        assert 'data-testid="reason-code-code"><code>found' not in resp.text


class TestArchive:
    def test_archive_unarchive_cycle(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        rc = ReasonCode(movement_type="out", code="sale", label="Sale")
        db_session.add(rc)
        db_session.commit()
        db_session.refresh(rc)
        _login_as(client, u)
        resp = client.post(
            f"/admin/reason-codes/{rc.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(rc)
        assert rc.archived_at is not None
        resp = client.post(
            f"/admin/reason-codes/{rc.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(rc)
        assert rc.archived_at is None

    def test_archived_code_still_blocks_create(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            ReasonCode(
                movement_type="out",
                code="sale",
                label="Sale (old)",
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        _login_as(client, u)
        # Archive-doesn't-free-the-code convention: same (movement_type,
        # code) blocked even with the other row archived.
        resp = client.post(
            "/admin/reason-codes",
            data={
                "movement_type": "out",
                "code": "sale",
                "label": "Sale (new)",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
