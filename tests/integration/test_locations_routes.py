"""Integration tests for the Manager-owned ``/admin/locations`` CRUD routes.

Mirrors ``test_suppliers_routes.py`` since the route shape is intentionally
identical. Covers:
- Role enforcement: anon=401, workshop=403, office=403, manager=200, admin=200,
  pending-manager=403.
- List filters (active default; ``?show=archived``).
- Create: happy path; whitespace strip; blank-string-as-null; reject empty,
  whitespace-only, duplicate; audit row content; no audit row on validation
  failure.
- Edit: GET form; 404 on unknown id; happy update; same-name allowed;
  cross-row name clash rejected; reject empty name; audit diff records only
  changed fields; no-op writes no audit row.
- Archive / unarchive: idempotent (no audit row on second call); audit on
  change; 404 on unknown id.
"""

from __future__ import annotations

from datetime import UTC, datetime

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


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.entity_type == "location").order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/locations")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/locations")
        assert resp.status_code == 403

    def test_office_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Locations are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/locations")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/locations")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/locations")
        assert resp.status_code == 200

    def test_workshop_create_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            "/admin/locations",
            data={"name": "Sneaky", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(Location)).first() is None

    def test_pending_user_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        pending = _make_user(
            db_session, email="p@x.test", role=Role.MANAGER, status=UserStatus.PENDING
        )
        _login_as(client, pending)
        resp = client.get("/admin/locations")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


class TestLocationsList:
    def test_list_shows_active_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                Location(name="Workshop Bench"),
                Location(name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/locations")
        assert resp.status_code == 200
        assert "Workshop Bench" in resp.text
        assert "Old Bench" not in resp.text

    def test_list_show_archived_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                Location(name="Workshop Bench"),
                Location(name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC)),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/locations?show=archived")
        assert resp.status_code == 200
        assert "Old Bench" in resp.text
        assert "Workshop Bench" not in resp.text

    def test_list_renders_new_location_cta(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/locations")
        assert "/admin/locations/new" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestLocationCreate:
    def test_get_new_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/locations/new")
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={
                "name": "Workshop Bench",
                "notes": "Main filing bench",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/locations"

        rows = list(db_session.execute(select(Location)).scalars().all())
        assert len(rows) == 1
        loc = rows[0]
        assert loc.name == "Workshop Bench"
        assert loc.notes == "Main filing bench"
        assert loc.archived_at is None

    def test_create_strips_whitespace_on_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={"name": "  Workshop  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = db_session.execute(select(Location)).scalar_one()
        assert loc.name == "Workshop"

    def test_create_treats_blank_notes_as_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={"name": "Workshop", "notes": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        loc = db_session.execute(select(Location)).scalar_one()
        assert loc.notes is None

    def test_create_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Location)).first() is None

    def test_create_rejects_whitespace_only_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={"name": "   ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Location)).first() is None

    def test_create_rejects_duplicate_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Location(name="Workshop"))
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={"name": "Workshop", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        rows = list(db_session.execute(select(Location)).scalars().all())
        assert len(rows) == 1

    def test_create_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={
                "name": "Workshop",
                "notes": "main bench",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="location.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.entity_type == "location"
        loc = db_session.execute(select(Location)).scalar_one()
        assert row.entity_id == loc.id
        assert row.before_json is None
        assert row.after_json == {"name": "Workshop", "notes": "main bench"}

    def test_create_validation_failure_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/locations",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Edit / Update
# ---------------------------------------------------------------------------


class TestLocationEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", notes="bench")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/locations/{loc.id}/edit")
        assert resp.status_code == 200
        assert "Workshop" in resp.text
        assert "bench" in resp.text

    def test_get_edit_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/locations/9999/edit")
        assert resp.status_code == 404

    def test_post_update_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", notes="old")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}",
            data={
                "name": "Workshop A",
                "notes": "new notes",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(Location, loc.id)
        assert refreshed is not None
        assert refreshed.name == "Workshop A"
        assert refreshed.notes == "new notes"

    def test_post_update_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/locations/9999",
            data={"name": "X", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_post_update_can_keep_same_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Updating without renaming must not trip the unique constraint."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", notes="old")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}",
            data={
                "name": "Workshop",  # unchanged
                "notes": "new",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        assert db_session.get(Location, loc.id).notes == "new"  # type: ignore[union-attr]

    def test_post_update_rejects_name_clash_with_other(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        a = Location(name="Workshop")
        b = Location(name="Vault")
        db_session.add_all([a, b])
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{b.id}",
            data={"name": "Workshop", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(Location, b.id).name == "Vault"  # type: ignore[union-attr]

    def test_post_update_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}",
            data={"name": "  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(Location, loc.id).name == "Workshop"  # type: ignore[union-attr]

    def test_update_writes_audit_row_with_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", notes="old")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}",
            data={
                "name": "Workshop",  # unchanged
                "notes": "renamed",  # changed
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="location.updated")
        assert len(rows) == 1
        # Only changed fields appear in the diff.
        assert rows[0].before_json == {"notes": "old"}
        assert rows[0].after_json == {"notes": "renamed"}

    def test_update_no_op_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", notes="n")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}",
            data={
                "name": "Workshop",
                "notes": "n",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="location.updated") == []


# ---------------------------------------------------------------------------
# Archive / Unarchive
# ---------------------------------------------------------------------------


class TestLocationArchive:
    def test_archive_sets_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(Location, loc.id)
        assert refreshed is not None
        assert refreshed.archived_at is not None

    def test_archive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop")
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="location.archived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == loc.id

    def test_archive_already_archived_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Idempotent — second archive call writes no row but still 303s."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="location.archived") == []

    def test_unarchive_clears_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(Location, loc.id)
        assert refreshed is not None
        assert refreshed.archived_at is None

    def test_unarchive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="location.unarchived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == loc.id

    def test_unarchive_already_active_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop")  # already active
        db_session.add(loc)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/locations/{loc.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="location.unarchived") == []

    def test_archive_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/locations/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# R5f — CSV export on the locations list
# ---------------------------------------------------------------------------


class TestLocationsListCsvRoleEnforcement:
    """``?format=csv`` inherits the same Manager-only gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 401

    def test_pending_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 403

    def test_office_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Locations are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 200

    def test_admin_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 200


class TestLocationsListCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="locations_active.csv"' in cd

    def test_content_disposition_archived_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv&show=archived")
        cd = resp.headers["content-disposition"]
        assert 'filename="locations_archived.csv"' in cd


class TestLocationsListCsvBody:
    def test_empty_emits_only_header_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 200
        assert resp.text == "id,name,notes\r\n"

    def test_one_location_one_data_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop Bench", notes="Filer's bay")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        _login_as(client, mgr)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        assert len(lines) == 3  # header + 1 data + trailing empty
        cells = lines[1].split(",")
        assert cells[0] == str(loc.id)
        assert cells[1] == "Workshop Bench"
        assert cells[2] == "Filer's bay"

    def test_show_filter_applies_to_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        active = Location(name="Workshop Bench")
        archived = Location(
            name="Old Bench",
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add_all([active, archived])
        db_session.commit()
        _login_as(client, mgr)

        # Default (active) → only the active row.
        resp = client.get("/admin/locations?format=csv")
        body = resp.text
        assert "Workshop Bench" in body
        assert "Old Bench" not in body

        # show=archived → only the archived row.
        resp = client.get("/admin/locations?format=csv&show=archived")
        body = resp.text
        assert "Old Bench" in body
        assert "Workshop Bench" not in body

    def test_blank_notes_renders_as_empty_cell(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Minimal")  # notes defaults to None
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        _login_as(client, mgr)
        resp = client.get("/admin/locations?format=csv")
        body = resp.text
        # The data row should be: id,Minimal,\r\n  (one trailing empty).
        data_line = body.split("\r\n")[1]
        cells = data_line.split(",")
        assert cells[1] == "Minimal"
        assert cells[2] == ""

    def test_alphabetical_ordering_in_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Insert deliberately out-of-order; the route orders by name within
        # the bucket.
        db_session.add_all(
            [Location(name="Zebra Cabinet"), Location(name="Acme Bench")]
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/locations?format=csv")
        body = resp.text
        acme_pos = body.index("Acme Bench")
        zebra_pos = body.index("Zebra Cabinet")
        assert acme_pos < zebra_pos


class TestLocationsListCsvHtmlBranch:
    def test_format_blank_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="locations-tabs"' in resp.text

    def test_format_unknown_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestLocationsListCsvReadOnly:
    def test_csv_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Location(name="Workshop"))
        db_session.commit()
        before = len(_audit_rows(db_session))
        _login_as(client, mgr)
        resp = client.get("/admin/locations?format=csv")
        assert resp.status_code == 200
        after = len(_audit_rows(db_session))
        assert after == before


class TestLocationsListCsvLink:
    def test_html_renders_csv_link_with_active_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="locations-list-csv-link"' in body
        assert "format=csv" in body
        assert "show=active" in body

    def test_html_renders_csv_link_with_archived_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/locations?show=archived")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="locations-list-csv-link"' in body
        assert "show=archived" in body
