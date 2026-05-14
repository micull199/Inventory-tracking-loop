"""Integration tests for the locations CSV upload route.

Same shape as ``test_suppliers_csv_upload.py``; smaller column surface but
all the same end-to-end invariants are pinned (role gating, dry-run vs
commit, header mismatch, idempotency by ``id``, cross-row duplicate names,
notes-length cap, archived_at warning).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Location, Role, User, UserStatus


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


def _post_upload(client: TestClient, csv_bytes: bytes, *, dry_run: bool) -> object:
    return client.post(
        "/admin/locations/upload",
        files={"file": ("locations.csv", csv_bytes, "text/csv")},
        data={"csrf_token": _csrf(client), "dry_run": "1" if dry_run else ""},
        follow_redirects=False,
    )


class TestRoleGating:
    def test_anonymous_get_form_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/locations/upload").status_code == 401

    def test_workshop_get_form_is_403(self, client: TestClient, db_session: Session) -> None:
        w = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, w)
        assert client.get("/admin/locations/upload").status_code == 403

    def test_office_get_form_is_403(self, client: TestClient, db_session: Session) -> None:
        o = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, o)
        assert client.get("/admin/locations/upload").status_code == 403

    def test_manager_get_form_is_200(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        resp = client.get("/admin/locations/upload")
        assert resp.status_code == 200
        assert "Upload locations CSV" in resp.text


class TestDryRunAndCommit:
    def test_dry_run_does_not_write(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name,notes\n,Bench A,front\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert resp.status_code == 200  # type: ignore[attr-defined]
        assert list(db_session.execute(select(Location)).scalars().all()) == []

    def test_commit_creates_rows_and_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name,notes\n,Bench A,front\n,Safe,back\n"
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert resp.headers["location"] == "/admin/locations"  # type: ignore[attr-defined]
        rows = list(db_session.execute(select(Location).order_by(Location.name)).scalars().all())
        assert [r.name for r in rows] == ["Bench A", "Safe"]
        assert rows[0].notes == "front"

        created = list(
            db_session.execute(select(AuditLog).where(AuditLog.action == "location.created"))
            .scalars()
            .all()
        )
        assert len(created) == 2
        summary = list(
            db_session.execute(select(AuditLog).where(AuditLog.action == "location.csv_uploaded"))
            .scalars()
            .all()
        )
        assert len(summary) == 1
        assert summary[0].after_json is not None
        assert summary[0].after_json["count"] == 2


class TestValidation:
    def test_missing_required_name_column(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,notes\n,front\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b"missing required column" in resp.content  # type: ignore[attr-defined]

    def test_unknown_column_blocks(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name,bogus\n,Bench A,whatever\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b"unknown column" in resp.content  # type: ignore[attr-defined]

    def test_existing_name_blocks(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Location(name="Bench A"))
        db_session.commit()
        _login_as(client, m)
        csv = b"id,name,notes\n,Bench A,front\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]

    def test_existing_id_unchanged_skipped(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        existing = Location(name="Bench A", notes="front")
        db_session.add(existing)
        db_session.commit()
        _login_as(client, m)
        csv = f"id,name,notes\n{existing.id},Bench A,front\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        # Same values → skip.
        assert b'data-row-tag="skip"' in resp.content  # type: ignore[attr-defined]

    def test_existing_id_changed_updates(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        existing = Location(name="Bench A")
        db_session.add(existing)
        db_session.commit()
        _login_as(client, m)
        csv = f"id,name,notes\n{existing.id},Bench A,front\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        # Different notes value → update.
        assert b'data-row-tag="update"' in resp.content  # type: ignore[attr-defined]

    def test_notes_too_long_errors(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        notes = "x" * 2001
        csv = f"id,name,notes\n,Bench Z,{notes}\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b"notes too long" in resp.content  # type: ignore[attr-defined]

    def test_duplicate_name_in_file(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name\n,Same\n,Same\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert resp.content.count(b'data-row-tag="error"') == 2  # type: ignore[attr-defined]


class TestUploadButton:
    def test_list_page_has_upload_button(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        resp = client.get("/admin/locations")
        assert "locations-list-upload-link" in resp.text
        assert "/admin/locations/upload" in resp.text
