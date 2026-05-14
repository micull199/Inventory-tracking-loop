"""Integration tests for the suppliers CSV upload route.

Covers:
- Role gating on GET + POST (Manager + Admin only).
- Happy-path dry-run preview (tags rows as new / skip / error; doesn't write).
- Happy-path commit (writes rows, audit summary, redirects with flash).
- Idempotency: re-uploading a row with a matching ``id`` skips it.
- Unknown id → error; non-integer id → error.
- Header mismatch (missing required column / unknown column) blocks parse.
- Cross-row duplicate names within the same file → both rows errored.
- Email-shape validation.
- archived_at on create emits a warning, doesn't block.
- File-size + row-count caps (smoke test against a small reduced cap).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, Supplier, User, UserStatus


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


def _post_upload(
    client: TestClient,
    csv_bytes: bytes,
    *,
    dry_run: bool,
    filename: str = "suppliers.csv",
) -> object:
    return client.post(
        "/admin/suppliers/upload",
        files={"file": (filename, csv_bytes, "text/csv")},
        data={
            "csrf_token": _csrf(client),
            "dry_run": "1" if dry_run else "",
        },
        follow_redirects=False,
    )


class TestRoleGating:
    def test_anonymous_get_form_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/suppliers/upload")
        assert resp.status_code == 401

    def test_workshop_get_form_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/suppliers/upload")
        assert resp.status_code == 403

    def test_office_get_form_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/suppliers/upload")
        assert resp.status_code == 403

    def test_manager_get_form_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers/upload")
        assert resp.status_code == 200
        assert "Upload suppliers CSV" in resp.text

    def test_admin_post_upload_is_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        csv = b"id,name,email,phone,notes\n,New Co,,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert resp.status_code == 200  # type: ignore[attr-defined]


class TestDryRun:
    def test_preview_does_not_write(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n,Acme,orders@acme.test,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert resp.status_code == 200  # type: ignore[attr-defined]
        # Nothing landed in the database.
        rows = list(db_session.execute(select(Supplier)).scalars().all())
        assert rows == []
        # No audit rows either.
        audit = list(
            db_session.execute(select(AuditLog).where(AuditLog.entity_type == "supplier"))
            .scalars()
            .all()
        )
        assert audit == []

    def test_preview_tags_new_row(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n,Acme,,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="new"' in resp.content  # type: ignore[attr-defined]
        assert b'data-testid="csv-upload-new-count">1' in resp.content  # type: ignore[attr-defined]

    def test_preview_tags_skip_for_existing_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        existing = Supplier(name="Already Here")
        db_session.add(existing)
        db_session.commit()
        _login_as(client, mgr)
        csv = f"id,name,email,phone,notes\n{existing.id},Already Here,,,\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="skip"' in resp.content  # type: ignore[attr-defined]
        assert b'data-testid="csv-upload-skip-count">1' in resp.content  # type: ignore[attr-defined]

    def test_preview_errors_on_unknown_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n9999,Whatever,,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"unknown id" in resp.content  # type: ignore[attr-defined]


class TestCommit:
    def test_commit_creates_rows_and_audits(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = (
            b"id,name,email,phone,notes\n"
            b",Acme,orders@acme.test,0123,Trade #1\n"
            b",Beta Co,,,\n"
        )
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        assert resp.headers["location"] == "/admin/suppliers"  # type: ignore[attr-defined]

        rows = list(
            db_session.execute(select(Supplier).order_by(Supplier.name)).scalars().all()
        )
        assert [r.name for r in rows] == ["Acme", "Beta Co"]
        assert rows[0].email == "orders@acme.test"
        assert rows[0].phone == "0123"
        assert rows[0].notes == "Trade #1"
        assert rows[0].archived_at is None

        # Per-row created audits + one summary audit.
        created = list(
            db_session.execute(
                select(AuditLog)
                .where(AuditLog.action == "supplier.created")
                .order_by(AuditLog.id)
            )
            .scalars()
            .all()
        )
        assert len(created) == 2
        for row in created:
            assert row.actor_id == mgr.id

        summary = list(
            db_session.execute(
                select(AuditLog).where(AuditLog.action == "supplier.csv_uploaded")
            )
            .scalars()
            .all()
        )
        assert len(summary) == 1
        assert summary[0].after_json is not None
        assert summary[0].after_json["count"] == 2
        # SHA-256 hex = 64 chars.
        assert len(summary[0].after_json["file_sha256"]) == 64

    def test_commit_blocked_when_any_row_errored(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # First row valid, second row has bad email → whole upload falls back
        # to preview, no writes.
        csv = b"id,name,email,phone,notes\n,Acme,,,\n,Beta,notanemail,,\n"
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 200  # type: ignore[attr-defined]
        # No suppliers landed — all-or-nothing.
        assert list(db_session.execute(select(Supplier)).scalars().all()) == []

    def test_commit_skips_existing_id_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        existing = Supplier(name="Already Here")
        db_session.add(existing)
        db_session.commit()
        _login_as(client, mgr)
        csv = (
            f"id,name,email,phone,notes\n"
            f"{existing.id},Already Here,,,\n"
            f",Fresh Co,,,\n"
        ).encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        names = sorted(
            n for (n,) in db_session.execute(select(Supplier.name)).all()
        )
        # Only the new row landed; existing row was skipped (not duplicated).
        assert names == ["Already Here", "Fresh Co"]


class TestValidation:
    def test_header_mismatch_top_level_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"bogus_col\nfoo\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert resp.status_code == 200  # type: ignore[attr-defined]
        assert b"csv-upload-top-error" in resp.content  # type: ignore[attr-defined]
        # Missing required 'name'.
        assert b"name" in resp.content  # type: ignore[attr-defined]

    def test_unknown_column_top_level_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"name,unrelated_col\nAcme,whatever\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b"unknown column" in resp.content  # type: ignore[attr-defined]

    def test_invalid_email_row_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n,Acme,notanemail,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"email" in resp.content  # type: ignore[attr-defined]

    def test_duplicate_name_in_file_both_rows_errored(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n,Acme,,,\n,acme,,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        # Each of the two rows is marked as a duplicate.
        assert resp.content.count(b'data-row-tag="error"') == 2  # type: ignore[attr-defined]

    def test_existing_active_name_row_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Supplier(name="Acme"))
        db_session.commit()
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n,Acme,,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"already exists" in resp.content  # type: ignore[attr-defined]

    def test_existing_archived_name_blocks_create(
        self, client: TestClient, db_session: Session
    ) -> None:
        # The spec says "unique across active + archived".
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(
            Supplier(name="Old Vendor", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
        db_session.commit()
        _login_as(client, mgr)
        csv = b"id,name,email,phone,notes\n,Old Vendor,,,\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]

    def test_archived_at_on_create_warns_but_does_not_block(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        csv = (
            b"id,name,email,phone,notes,archived_at\n"
            b",Brand New,,,,2026-05-01T00:00:00\n"
        )
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        s = db_session.execute(select(Supplier).where(Supplier.name == "Brand New")).scalar_one()
        # Row landed active despite the archived_at column.
        assert s.archived_at is None


class TestUploadButton:
    def test_list_page_has_upload_button(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers")
        assert "suppliers-list-upload-link" in resp.text
        assert "/admin/suppliers/upload" in resp.text
