"""Integration tests for the Manager+Admin audit log read view (slice A1).

The view at ``GET /admin/audit`` is the only read surface for the
``audit_log`` table. Writers (``app.audit.record_audit``) and the DB-level
immutability triggers are tested elsewhere; this file focuses on the role
gate, render shape, sort order, and pagination behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.audit_routes import PAGE_SIZE
from app.models import AuditLog, Role, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None,
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
        data={"email": user.email, "sub": user.google_sub, "name": user.name},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def _seed_row(
    db: Session,
    *,
    actor: User | None,
    action: str = "user.role_assigned",
    entity_type: str = "user",
    entity_id: int | None = 1,
    before: dict | None = None,
    after: dict | None = None,
) -> AuditLog:
    row = record_audit(
        db,
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before=before or {"role": None},
        after=after or {"role": "manager"},
    )
    db.commit()
    return row


class TestAuditViewRoleEnforcement:
    def test_anon_returns_401(self, client: TestClient) -> None:
        resp = client.get("/admin/audit", follow_redirects=False)
        assert resp.status_code == 401

    def test_pending_returns_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(
            db_session,
            email="pending@x.test",
            role=None,
            status=UserStatus.PENDING,
        )
        _login_as(client, user)
        resp = client.get("/admin/audit", follow_redirects=False)
        assert resp.status_code == 403

    def test_workshop_returns_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, user)
        resp = client.get("/admin/audit", follow_redirects=False)
        assert resp.status_code == 403

    def test_office_returns_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, user)
        resp = client.get("/admin/audit", follow_redirects=False)
        assert resp.status_code == 403

    def test_manager_returns_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, user)
        resp = client.get("/admin/audit", follow_redirects=False)
        assert resp.status_code == 200

    def test_admin_returns_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, user)
        resp = client.get("/admin/audit", follow_redirects=False)
        assert resp.status_code == 200


class TestAuditViewRender:
    def test_renders_audit_table(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        assert 'data-testid="admin-audit-table"' in resp.text

    def test_renders_seeded_row_action_and_actor(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="boss@x.test", role=Role.ADMIN)
        _seed_row(db_session, actor=admin, action="supplier.created")
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        assert "supplier.created" in resp.text
        assert "boss@x.test" in resp.text

    def test_entity_column_renders_type_and_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _seed_row(
            db_session,
            actor=admin,
            entity_type="supplier",
            entity_id=42,
        )
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        assert "supplier:42" in resp.text

    def test_system_action_renders_actor_as_system(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        # System action — no actor.
        _seed_row(
            db_session,
            actor=None,
            action="user.bootstrap_admin_granted",
            entity_type="user",
            entity_id=admin.id,
        )
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        # Find the row, then assert "(system)" appears within it.
        row_idx = resp.text.find("user.bootstrap_admin_granted")
        assert row_idx > 0
        row_start = resp.text.rfind("<tr", 0, row_idx)
        row_end = resp.text.find("</tr>", row_idx)
        row_html = resp.text[row_start:row_end]
        assert "(system)" in row_html

    def test_before_after_render_compact_json(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _seed_row(
            db_session,
            actor=admin,
            before={"role": "office"},
            after={"role": "manager"},
        )
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        # Jinja's tojson filter emits compact JSON without extraneous spaces.
        assert '{"role": "office"}' in resp.text or '{"role":"office"}' in resp.text
        assert '{"role": "manager"}' in resp.text or '{"role":"manager"}' in resp.text

    def test_null_before_renders_dash(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        # Override _seed_row to omit before_json entirely.
        record_audit(
            db_session,
            actor=admin,
            action="thing.created",
            entity_type="thing",
            entity_id=1,
            before=None,
            after={"x": 1},
        )
        db_session.commit()
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        # The em-dash placeholder should appear in the before column for the row.
        row_idx = resp.text.find("thing.created")
        assert row_idx > 0
        row_start = resp.text.rfind("<tr", 0, row_idx)
        row_end = resp.text.find("</tr>", row_idx)
        row_html = resp.text[row_start:row_end]
        assert 'data-testid="audit-before"' in row_html
        # The before cell renders the dash; the after cell renders JSON.
        before_cell_idx = row_html.find('data-testid="audit-before"')
        before_cell_close = row_html.find("</td>", before_cell_idx)
        before_cell = row_html[before_cell_idx:before_cell_close]
        assert "—" in before_cell

    def test_empty_summary_when_no_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        assert "No audit entries yet." in resp.text


class TestAuditViewSort:
    def test_newest_row_appears_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        # Seed two rows with explicit created_at timestamps.
        older = AuditLog(
            actor_id=admin.id,
            action="thing.older",
            entity_type="thing",
            entity_id=1,
            before_json=None,
            after_json={"x": 1},
            created_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        )
        newer = AuditLog(
            actor_id=admin.id,
            action="thing.newer",
            entity_type="thing",
            entity_id=2,
            before_json=None,
            after_json={"x": 2},
            created_at=datetime(2025, 1, 2, 12, 0, 0, tzinfo=UTC),
        )
        db_session.add_all([older, newer])
        db_session.commit()

        _login_as(client, admin)
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        newer_idx = resp.text.find("thing.newer")
        older_idx = resp.text.find("thing.older")
        assert newer_idx > 0
        assert older_idx > 0
        assert newer_idx < older_idx, "newest row must appear first"


class TestAuditViewPagination:
    @pytest.fixture
    def admin_with_many_rows(
        self, client: TestClient, db_session: Session
    ) -> User:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        # Seed 60 rows so we have one full page + 10 spillover. Stagger
        # created_at so the order is deterministic.
        base = datetime(2025, 1, 1, 0, 0, 0, tzinfo=UTC)
        for i in range(60):
            db_session.add(
                AuditLog(
                    actor_id=admin.id,
                    action=f"thing.event_{i:02d}",
                    entity_type="thing",
                    entity_id=i,
                    before_json=None,
                    after_json={"i": i},
                    created_at=base + timedelta(seconds=i),
                )
            )
        db_session.commit()
        _login_as(client, admin)
        return admin

    def test_default_page_shows_first_page_size(
        self,
        client: TestClient,
        db_session: Session,
        admin_with_many_rows: User,
    ) -> None:
        resp = client.get("/admin/audit")
        assert resp.status_code == 200
        # 50 rows on the page (PAGE_SIZE = 50). The 10 oldest events 00-09 are NOT
        # on page 1 (newer-first ordering puts them on page 2).
        assert "thing.event_59" in resp.text  # newest -> on page 1
        assert "thing.event_10" in resp.text  # 50th newest -> on page 1
        assert "thing.event_09" not in resp.text  # 51st newest -> on page 2
        # Summary text mentions the range.
        assert f"Showing 1-{PAGE_SIZE} of 60" in resp.text
        # Next link present (more pages); prev link absent (page=1).
        assert 'data-testid="audit-next"' in resp.text
        assert 'data-testid="audit-prev"' not in resp.text

    def test_page_two_shows_remaining_rows(
        self,
        client: TestClient,
        db_session: Session,
        admin_with_many_rows: User,
    ) -> None:
        resp = client.get("/admin/audit?page=2")
        assert resp.status_code == 200
        # Page 2 has the 10 oldest rows (event_00..event_09).
        assert "thing.event_09" in resp.text
        assert "thing.event_00" in resp.text
        # Newest 50 are NOT on page 2.
        assert "thing.event_59" not in resp.text
        assert "thing.event_10" not in resp.text
        # Summary mentions 51-60.
        assert "Showing 51-60 of 60" in resp.text
        # Prev link present; next absent.
        assert 'data-testid="audit-prev"' in resp.text
        assert 'data-testid="audit-next"' not in resp.text

    def test_page_too_high_renders_empty_placeholder(
        self,
        client: TestClient,
        db_session: Session,
        admin_with_many_rows: User,
    ) -> None:
        resp = client.get("/admin/audit?page=99")
        assert resp.status_code == 200
        assert 'data-testid="audit-empty"' in resp.text
        # Prev link exists (we can page back); next absent.
        assert 'data-testid="audit-prev"' in resp.text
        assert 'data-testid="audit-next"' not in resp.text

    def test_page_zero_clamps_to_one(
        self,
        client: TestClient,
        db_session: Session,
        admin_with_many_rows: User,
    ) -> None:
        resp = client.get("/admin/audit?page=0")
        assert resp.status_code == 200
        # Behaves like page=1: latest event present, no prev link.
        assert "thing.event_59" in resp.text
        assert 'data-testid="audit-prev"' not in resp.text

    def test_page_negative_clamps_to_one(
        self,
        client: TestClient,
        db_session: Session,
        admin_with_many_rows: User,
    ) -> None:
        resp = client.get("/admin/audit?page=-3")
        assert resp.status_code == 200
        assert "thing.event_59" in resp.text
        assert 'data-testid="audit-prev"' not in resp.text
