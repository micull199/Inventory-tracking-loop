"""Integration tests for the Manager-owned ``/admin/suppliers`` CRUD routes.

Covers:
- List view: filters by ``?show=active|archived``; New CTA visible.
- Create: happy path; name validation (empty, whitespace, duplicate); audit row.
- Edit: happy path; uniqueness across other rows; no-op writes no audit row.
- Archive / unarchive: idempotent (no audit row on second call); audit on change.
- Role enforcement: Workshop = 403, Office = 403, anonymous = 401, Manager = 200, Admin = 200.
- 404 on unknown id.
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


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.entity_type == "supplier").order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        # Anon needs a CSRF cookie to even POST, but GET is exempt — should 401.
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 403

    def test_office_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Suppliers are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200

    def test_workshop_create_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            "/admin/suppliers",
            data={"name": "Sneaky", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(Supplier)).first() is None

    def test_pending_user_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        pending = _make_user(
            db_session, email="p@x.test", role=Role.MANAGER, status=UserStatus.PENDING
        )
        _login_as(client, pending)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


class TestSuppliersList:
    def test_list_shows_active_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                Supplier(name="Acme Wax Co"),
                Supplier(name="Old Vendor", archived_at=datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200
        assert "Acme Wax Co" in resp.text
        assert "Old Vendor" not in resp.text

    def test_list_show_archived_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                Supplier(name="Acme Wax Co"),
                Supplier(name="Old Vendor", archived_at=datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/suppliers?show=archived")
        assert resp.status_code == 200
        assert "Old Vendor" in resp.text
        assert "Acme Wax Co" not in resp.text

    def test_list_renders_new_supplier_cta(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers")
        assert "/admin/suppliers/new" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestSupplierCreate:
    def test_get_new_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers/new")
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={
                "name": "Acme Wax Co",
                "email": "orders@acme.test",
                "phone": "0123 456789",
                "notes": "Trade #44",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/suppliers"

        rows = list(db_session.execute(select(Supplier)).scalars().all())
        assert len(rows) == 1
        s = rows[0]
        assert s.name == "Acme Wax Co"
        assert s.email == "orders@acme.test"
        assert s.phone == "0123 456789"
        assert s.notes == "Trade #44"
        assert s.archived_at is None

    def test_create_strips_whitespace_on_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={"name": "  Acme  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        s = db_session.execute(select(Supplier)).scalar_one()
        assert s.name == "Acme"

    def test_create_treats_blank_strings_as_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={
                "name": "Acme",
                "email": "",
                "phone": "",
                "notes": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        s = db_session.execute(select(Supplier)).scalar_one()
        assert s.email is None
        assert s.phone is None
        assert s.notes is None

    def test_create_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Supplier)).first() is None

    def test_create_rejects_whitespace_only_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={"name": "   ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Supplier)).first() is None

    def test_create_rejects_duplicate_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Supplier(name="Acme"))
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={"name": "Acme", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        rows = list(db_session.execute(select(Supplier)).scalars().all())
        assert len(rows) == 1  # original survived

    def test_create_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={
                "name": "Acme",
                "email": "x@y.test",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="supplier.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.entity_type == "supplier"
        s = db_session.execute(select(Supplier)).scalar_one()
        assert row.entity_id == s.id
        assert row.before_json is None
        assert row.after_json == {
            "name": "Acme",
            "email": "x@y.test",
            "phone": None,
            "notes": None,
        }

    def test_create_validation_failure_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/suppliers",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Edit / Update
# ---------------------------------------------------------------------------


class TestSupplierEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", email="o@a.test")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/suppliers/{s.id}/edit")
        assert resp.status_code == 200
        assert "Acme" in resp.text
        assert "o@a.test" in resp.text

    def test_get_edit_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers/9999/edit")
        assert resp.status_code == 404

    def test_post_update_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", email="o@a.test")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}",
            data={
                "name": "Acme Inc",
                "email": "new@a.test",
                "phone": "0123",
                "notes": "renamed",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(Supplier, s.id)
        assert refreshed is not None
        assert refreshed.name == "Acme Inc"
        assert refreshed.email == "new@a.test"
        assert refreshed.phone == "0123"
        assert refreshed.notes == "renamed"

    def test_post_update_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/suppliers/9999",
            data={"name": "X", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_post_update_can_keep_same_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Updating without renaming must not trip the unique constraint."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", email="o@a.test")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}",
            data={
                "name": "Acme",  # unchanged
                "email": "different@a.test",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        assert db_session.get(Supplier, s.id).email == "different@a.test"  # type: ignore[union-attr]

    def test_post_update_rejects_name_clash_with_other(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        a = Supplier(name="Acme")
        b = Supplier(name="Brindleys")
        db_session.add_all([a, b])
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{b.id}",
            data={"name": "Acme", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(Supplier, b.id).name == "Brindleys"  # type: ignore[union-attr]

    def test_post_update_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}",
            data={"name": "  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(Supplier, s.id).name == "Acme"  # type: ignore[union-attr]

    def test_update_writes_audit_row_with_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", email="old@a.test", phone="111")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}",
            data={
                "name": "Acme",  # unchanged
                "email": "new@a.test",  # changed
                "phone": "111",  # unchanged
                "notes": "added",  # was None → set
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="supplier.updated")
        assert len(rows) == 1
        # Only changed fields appear in the diff.
        assert rows[0].before_json == {"email": "old@a.test", "notes": None}
        assert rows[0].after_json == {"email": "new@a.test", "notes": "added"}

    def test_update_no_op_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", email="o@a.test", phone="111", notes="n")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}",
            data={
                "name": "Acme",
                "email": "o@a.test",
                "phone": "111",
                "notes": "n",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="supplier.updated") == []


# ---------------------------------------------------------------------------
# Archive / Unarchive
# ---------------------------------------------------------------------------


class TestSupplierArchive:
    def test_archive_sets_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(Supplier, s.id)
        assert refreshed is not None
        assert refreshed.archived_at is not None

    def test_archive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="supplier.archived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == s.id

    def test_archive_already_archived_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Idempotent — second archive call writes no row but still 303s."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="supplier.archived") == []

    def test_unarchive_clears_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(Supplier, s.id)
        assert refreshed is not None
        assert refreshed.archived_at is None

    def test_unarchive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="supplier.unarchived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == s.id

    def test_unarchive_already_active_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme")  # already active
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/suppliers/{s.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="supplier.unarchived") == []

    def test_archive_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/suppliers/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404
