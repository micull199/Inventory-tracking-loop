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

    def test_workshop_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 403

    def test_office_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        """Suppliers are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200

    def test_workshop_create_is_403(self, client: TestClient, db_session: Session) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            "/admin/suppliers",
            data={"name": "Sneaky", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(Supplier)).first() is None

    def test_pending_user_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
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
    def test_list_shows_active_by_default(self, client: TestClient, db_session: Session) -> None:
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

    def test_list_show_archived_filter(self, client: TestClient, db_session: Session) -> None:
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

    def test_list_renders_new_supplier_cta(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers")
        assert "/admin/suppliers/new" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestSupplierCreate:
    def test_get_new_form_renders(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers/new")
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_create_happy_path(self, client: TestClient, db_session: Session) -> None:
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

    def test_create_rejects_empty_name(self, client: TestClient, db_session: Session) -> None:
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

    def test_create_rejects_duplicate_name(self, client: TestClient, db_session: Session) -> None:
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

    def test_create_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
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
    def test_get_edit_form_renders(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Acme", email="o@a.test")
        db_session.add(s)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/suppliers/{s.id}/edit")
        assert resp.status_code == 200
        assert "Acme" in resp.text
        assert "o@a.test" in resp.text

    def test_get_edit_unknown_id_is_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers/9999/edit")
        assert resp.status_code == 404

    def test_post_update_happy_path(self, client: TestClient, db_session: Session) -> None:
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

    def test_post_update_unknown_id_is_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/suppliers/9999",
            data={"name": "X", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_post_update_can_keep_same_name(self, client: TestClient, db_session: Session) -> None:
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

    def test_post_update_rejects_empty_name(self, client: TestClient, db_session: Session) -> None:
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
    def test_archive_sets_archived_at(self, client: TestClient, db_session: Session) -> None:
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

    def test_archive_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
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

    def test_unarchive_clears_archived_at(self, client: TestClient, db_session: Session) -> None:
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

    def test_unarchive_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
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

    def test_archive_unknown_id_is_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/suppliers/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# R5d — CSV export on the suppliers list
# ---------------------------------------------------------------------------


class TestSuppliersListCsvRoleEnforcement:
    """``?format=csv`` inherits the same Manager-only gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 401

    def test_pending_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 403

    def test_office_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        """Suppliers are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 200

    def test_admin_csv_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 200


class TestSuppliersListCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="suppliers_active.csv"' in cd

    def test_content_disposition_archived_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv&show=archived")
        cd = resp.headers["content-disposition"]
        assert 'filename="suppliers_archived.csv"' in cd


class TestSuppliersListCsvBody:
    def test_empty_emits_only_header_row(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 200
        assert resp.text == "id,name,email,phone,notes\r\n"

    def test_one_supplier_one_data_row(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(
            name="Acme Wax Co",
            email="orders@acme.test",
            phone="0123 456789",
            notes="Trade #44",
        )
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        assert len(lines) == 3  # header + 1 data + trailing empty
        cells = lines[1].split(",")
        assert cells[0] == str(s.id)
        assert cells[1] == "Acme Wax Co"
        assert cells[2] == "orders@acme.test"
        assert cells[3] == "0123 456789"
        assert cells[4] == "Trade #44"

    def test_show_filter_applies_to_csv(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        active = Supplier(name="Acme Wax Co")
        archived = Supplier(
            name="Old Vendor",
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add_all([active, archived])
        db_session.commit()
        _login_as(client, mgr)

        # Default (active) → only the active row.
        resp = client.get("/admin/suppliers?format=csv")
        body = resp.text
        assert "Acme Wax Co" in body
        assert "Old Vendor" not in body

        # show=archived → only the archived row.
        resp = client.get("/admin/suppliers?format=csv&show=archived")
        body = resp.text
        assert "Old Vendor" in body
        assert "Acme Wax Co" not in body

    def test_blank_optional_fields_render_as_empty_cells(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        s = Supplier(name="Minimal")  # email/phone/notes default to None
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers?format=csv")
        body = resp.text
        # The data row should be: id,Minimal,,,\r\n  (three trailing empties).
        data_line = body.split("\r\n")[1]
        cells = data_line.split(",")
        assert cells[1] == "Minimal"
        assert cells[2] == ""
        assert cells[3] == ""
        assert cells[4] == ""

    def test_alphabetical_ordering_in_csv(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Insert deliberately out-of-order; the route orders by name within
        # the bucket.
        db_session.add_all([Supplier(name="Zebra Ltd"), Supplier(name="Acme Wax Co")])
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers?format=csv")
        body = resp.text
        acme_pos = body.index("Acme Wax Co")
        zebra_pos = body.index("Zebra Ltd")
        assert acme_pos < zebra_pos


class TestSuppliersListCsvHtmlBranch:
    def test_format_blank_renders_html(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="suppliers-tabs"' in resp.text

    def test_format_unknown_renders_html(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestSuppliersListCsvReadOnly:
    def test_csv_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Supplier(name="Acme"))
        db_session.commit()
        before = len(_audit_rows(db_session))
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers?format=csv")
        assert resp.status_code == 200
        after = len(_audit_rows(db_session))
        assert after == before


class TestSuppliersListCsvLink:
    def test_html_renders_csv_link_with_active_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="suppliers-list-csv-link"' in body
        assert "format=csv" in body
        assert "show=active" in body

    def test_html_renders_csv_link_with_archived_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/suppliers?show=archived")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="suppliers-list-csv-link"' in body
        assert "show=archived" in body
