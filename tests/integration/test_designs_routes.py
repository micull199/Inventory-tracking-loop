"""Integration tests for the Manager-owned ``/admin/designs`` CRUD routes.

S3 (modified) per ``docs/adr/003-designs-split-from-taxonomy.md``:
list / create / edit only (archive UX deferred). Role gates mirror
the other lookup admins: Manager + Admin only; Workshop / Office both
403; anon 401.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AlloyFamily,
    AuditLog,
    Design,
    Metal,
    MetalColour,
    Role,
    User,
    UserStatus,
)


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
        .where(AuditLog.entity_type == "design")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


def _make_metal(db: Session, code: str = "18KYG") -> Metal:
    metal = Metal(
        metal_code=code,
        name=f"Test {code}",
        alloy_family=AlloyFamily.GOLD,
        karat=18,
        purity_pct=Decimal("75.000"),
        colour=MetalColour.YELLOW,
    )
    db.add(metal)
    db.commit()
    db.refresh(metal)
    return metal


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/designs")
        assert resp.status_code == 401

    def test_workshop_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        assert client.get("/admin/designs").status_code == 403

    def test_office_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        assert client.get("/admin/designs").status_code == 403

    def test_manager_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        assert client.get("/admin/designs").status_code == 200

    def test_admin_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        assert client.get("/admin/designs").status_code == 200


class TestList:
    def test_active_only(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(Design(design_code="DSG-0001", name="Emma"))
        db_session.add(
            Design(
                design_code="DSG-0002",
                name="Old",
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/designs")
        assert resp.status_code == 200
        assert "Emma" in resp.text
        # Archived rows are hidden from the list (archive UX deferred per ADR-003).
        assert "Old" not in resp.text

    def test_empty_state(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/designs")
        assert resp.status_code == 200
        assert "No designs yet" in resp.text


class TestCreate:
    def test_minimal_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/designs",
            data={"name": "Emma", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = db_session.execute(
            select(Design).where(Design.name == "Emma")
        ).scalar_one()
        assert row.design_code == "DSG-0001"
        # Allocator persisted to the counter.
        from app.models import SequenceCounter

        counter = db_session.execute(
            select(SequenceCounter).where(SequenceCounter.name == "design_code")
        ).scalar_one()
        assert counter.next_value == 2
        # Audit row records the allocated code.
        audit = _audit_rows(db_session, action="design.created")
        assert len(audit) == 1
        assert audit[0].after_json is not None
        assert audit[0].after_json["design_code"] == "DSG-0001"

    def test_full_field_set_round_trip(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/designs",
            data={
                "name": "Emma",
                "collection": "Bridal 2026",
                "style_family": "solitaire",
                "designer": "Jane Smith",
                "cad_file_path": "/cad/emma.3dm",
                "cad_version": "v1.2",
                "cad_updated_at": "2026-05-15T12:00:00",
                "default_metal_id": str(metal.id),
                "intro_date": "2026-04-01",
                "standard_cost": "1250.00",
                "notes": "Best-selling solitaire.",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        row = db_session.execute(select(Design)).scalar_one()
        assert row.cad_version == "v1.2"
        assert row.cad_updated_at is not None  # SQLite drops tz; presence is enough
        assert row.default_metal_id == metal.id
        assert row.standard_cost == Decimal("1250.00")

    def test_blank_name_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/designs",
            data={"name": "   ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Design)).first() is None

    def test_unknown_style_family_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/designs",
            data={
                "name": "Emma",
                "style_family": "not-a-style",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_bad_decimal_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/designs",
            data={
                "name": "Emma",
                "standard_cost": "twelve-fifty",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_archived_metal_rejected_on_create(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        metal.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            "/admin/designs",
            data={
                "name": "Emma",
                "default_metal_id": str(metal.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestEdit:
    def test_happy_update(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        design = Design(design_code="DSG-0001", name="Emma")
        db_session.add(design)
        db_session.commit()
        db_session.refresh(design)
        _login_as(client, u)
        resp = client.post(
            f"/admin/designs/{design.id}",
            data={
                "name": "Emma Renamed",
                "cad_version": "v2.0",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(design)
        assert design.name == "Emma Renamed"
        assert design.cad_version == "v2.0"
        # design_code is immutable — server-allocated, no form field for it.
        assert design.design_code == "DSG-0001"
        audit = _audit_rows(db_session, action="design.updated")
        assert len(audit) == 1
        # Only changed fields appear in the diff.
        assert audit[0].after_json == {
            "name": "Emma Renamed",
            "cad_version": "v2.0",
        }

    def test_noop_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        design = Design(design_code="DSG-0001", name="Emma")
        db_session.add(design)
        db_session.commit()
        db_session.refresh(design)
        _login_as(client, u)
        resp = client.post(
            f"/admin/designs/{design.id}",
            data={"name": "Emma", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="design.updated") == []

    def test_404_unknown_id(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.post(
            "/admin/designs/9999",
            data={"name": "Emma", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archive_unarchive_cycle(
        self, client: TestClient, db_session: Session
    ) -> None:
        """ADR-003 follow-on: archive UX shipped after the items FK was
        deferred. The route flips ``archived_at`` and writes an audit row;
        the list-active tab hides archived rows; the archived tab shows
        them with the archived pill.
        """
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        design = Design(design_code="DSG-0001", name="Emma")
        db_session.add(design)
        db_session.commit()
        db_session.refresh(design)
        _login_as(client, u)

        # Archive.
        resp = client.post(
            f"/admin/designs/{design.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(design)
        assert design.archived_at is not None
        archived_audit = _audit_rows(db_session, action="design.archived")
        assert len(archived_audit) == 1

        # Active list hides archived rows. The flash banner echoes the
        # design name after a successful archive POST, so we assert on
        # the table row marker (data-design-id) rather than the text
        # itself — the flash will repeat the name regardless.
        active_list = client.get("/admin/designs")
        assert active_list.status_code == 200
        assert f'data-design-id="{design.id}"' not in active_list.text

        # Archived list shows them.
        archived_list = client.get("/admin/designs?show=archived")
        assert archived_list.status_code == 200
        assert f'data-design-id="{design.id}"' in archived_list.text

        # Unarchive flips back + writes its own audit row.
        resp = client.post(
            f"/admin/designs/{design.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(design)
        assert design.archived_at is None
        assert len(_audit_rows(db_session, action="design.unarchived")) == 1

    def test_archive_idempotent(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        design = Design(
            design_code="DSG-0001",
            name="Emma",
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(design)
        db_session.commit()
        db_session.refresh(design)
        _login_as(client, u)
        resp = client.post(
            f"/admin/designs/{design.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Second archive call is a no-op — no audit row.
        assert _audit_rows(db_session, action="design.archived") == []

    def test_edit_keeps_existing_archived_metal(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Editing a design whose metal is now archived doesn't drop the link.

        Mirrors the archived-FK-preservation posture from
        ``items._resolve_optional_supplier``: an existing archived
        reference survives the edit; a *new* archived pick is rejected.
        """
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        metal = _make_metal(db_session)
        design = Design(
            design_code="DSG-0001", name="Emma", default_metal_id=metal.id
        )
        db_session.add(design)
        db_session.commit()
        db_session.refresh(design)
        metal.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            f"/admin/designs/{design.id}",
            data={
                "name": "Emma v2",
                "default_metal_id": str(metal.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(design)
        assert design.default_metal_id == metal.id
