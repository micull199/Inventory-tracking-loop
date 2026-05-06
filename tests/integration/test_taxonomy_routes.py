"""Integration tests for the Manager-owned ``/admin/taxonomy`` CRUD routes.

S3 covers top-level categories only. The route never accepts ``parent_id`` from
form input, and never surfaces sub-categories in the list / edit / archive
flows. The schema accepts sub-categories (so S4 doesn't need a migration), but
they are tested at the unit level only — exercised through routes once S4
introduces them.

Mirrors ``test_suppliers_routes.py`` and ``test_locations_routes.py``: same
shape of tests, plus three S3-specific blocks (sort_order behaviour, parent_id
not accepted from the form, sub-category id 404s through the top-level routes).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Role, TaxonomyNode, User, UserStatus


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


def _audit_rows(
    db: Session, *, action: str | None = None
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "taxonomy_node")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 403

    def test_office_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Taxonomy is Manager-owned (MISSION §3) — Office cannot manage taxonomy."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 200

    def test_workshop_create_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            "/admin/taxonomy",
            data={"name": "Sneaky", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(TaxonomyNode)).first() is None

    def test_pending_user_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


class TestTaxonomyList:
    def test_list_shows_active_top_level_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                TaxonomyNode(name="Raw Materials", sort_order=10),
                TaxonomyNode(
                    name="Old",
                    sort_order=20,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 200
        assert "Raw Materials" in resp.text
        assert "Old" not in resp.text

    def test_list_show_archived_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                TaxonomyNode(name="Raw Materials"),
                TaxonomyNode(
                    name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/taxonomy?show=archived")
        assert resp.status_code == 200
        assert "Old" in resp.text
        assert "Raw Materials" not in resp.text

    def test_list_excludes_sub_categories(
        self, client: TestClient, db_session: Session
    ) -> None:
        """S3 list shows top-level only. Sub-cats inserted directly are hidden."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Raw Materials")
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        db_session.add(TaxonomyNode(name="Silver", parent_id=parent.id))
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/taxonomy")
        assert "Raw Materials" in resp.text
        assert "Silver" not in resp.text

    def test_list_orders_by_sort_order_then_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                TaxonomyNode(name="Zulu", sort_order=10),
                TaxonomyNode(name="Alpha", sort_order=20),
                TaxonomyNode(name="Bravo", sort_order=10),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/taxonomy")
        # sort_order=10: Bravo before Zulu (alpha within bucket); then Alpha (sort=20).
        body = resp.text
        idx_bravo = body.find("Bravo")
        idx_zulu = body.find("Zulu")
        idx_alpha = body.find("Alpha")
        assert 0 < idx_bravo < idx_zulu < idx_alpha


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestTaxonomyCreate:
    def test_get_new_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/new")
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert 'name="sort_order"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/taxonomy",
            data={
                "name": "Raw Materials",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/taxonomy"

        rows = list(db_session.execute(select(TaxonomyNode)).scalars().all())
        assert len(rows) == 1
        node = rows[0]
        assert node.name == "Raw Materials"
        assert node.parent_id is None
        assert node.sort_order == 5
        assert node.archived_at is None

    def test_create_strips_whitespace_on_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy",
            data={"name": "  Tools  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        node = db_session.execute(select(TaxonomyNode)).scalar_one()
        assert node.name == "Tools"

    def test_create_blank_sort_order_defaults_when_no_rows(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy",
            data={"name": "First", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        node = db_session.execute(select(TaxonomyNode)).scalar_one()
        assert node.sort_order == 0

    def test_create_blank_sort_order_steps_by_10(
        self, client: TestClient, db_session: Session
    ) -> None:
        """With existing top-level rows, an unspecified sort_order steps by 10."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add_all(
            [
                TaxonomyNode(name="A", sort_order=10),
                TaxonomyNode(name="B", sort_order=30),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            "/admin/taxonomy",
            data={"name": "C", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        c = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.name == "C")
        ).scalar_one()
        assert c.sort_order == 40

    def test_create_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(TaxonomyNode)).first() is None

    def test_create_rejects_whitespace_only_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy",
            data={"name": "   ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(TaxonomyNode)).first() is None

    def test_create_rejects_duplicate_top_level_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(TaxonomyNode(name="Raw Materials"))
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            "/admin/taxonomy",
            data={"name": "Raw Materials", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        rows = list(db_session.execute(select(TaxonomyNode)).scalars().all())
        assert len(rows) == 1

    def test_create_rejects_invalid_sort_order(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy",
            data={
                "name": "Tools",
                "sort_order": "first",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(TaxonomyNode)).first() is None

    def test_create_ignores_unknown_parent_id_form_field(
        self, client: TestClient, db_session: Session
    ) -> None:
        """S3's POST signature has no ``parent_id`` Form param.

        FastAPI silently ignores extra form fields, so a hostile client
        attaching a ``parent_id`` does NOT escalate to a sub-category create.
        Asserting this nails the contract for S4 — when sub-cats arrive, they
        must arrive on a different route, not by leaking parent_id into the
        top-level POST.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Pre-create a parent so the bogus form field could plausibly resolve.
        parent = TaxonomyNode(name="Raw Materials")
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/taxonomy",
            data={
                "name": "Silver",
                "parent_id": str(parent.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        silver = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.name == "Silver")
        ).scalar_one()
        assert silver.parent_id is None  # field ignored — top-level only.

    def test_create_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/taxonomy",
            data={
                "name": "Raw Materials",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="taxonomy_node.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.entity_type == "taxonomy_node"
        node = db_session.execute(select(TaxonomyNode)).scalar_one()
        assert row.entity_id == node.id
        assert row.before_json is None
        assert row.after_json == {
            "name": "Raw Materials",
            "sort_order": 5,
            "parent_id": None,
        }

    def test_create_validation_failure_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/taxonomy",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Edit / Update
# ---------------------------------------------------------------------------


class TestTaxonomyEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=20)
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{node.id}/edit")
        assert resp.status_code == 200
        assert "Raw Materials" in resp.text
        assert 'value="20"' in resp.text

    def test_get_edit_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/9999/edit")
        assert resp.status_code == 404

    def test_get_edit_sub_category_is_404_in_s3(
        self, client: TestClient, db_session: Session
    ) -> None:
        """S3's edit route is for top-level only. Sub-cats 404 here."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Raw Materials")
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{sub.id}/edit")
        assert resp.status_code == 404

    def test_post_update_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}",
            data={
                "name": "Raw Material",
                "sort_order": "15",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, node.id)
        assert refreshed is not None
        assert refreshed.name == "Raw Material"
        assert refreshed.sort_order == 15

    def test_post_update_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/9999",
            data={"name": "X", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_post_update_sub_category_via_top_route_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Raw Materials")
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{sub.id}",
            data={"name": "Renamed", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_post_update_can_keep_same_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}",
            data={
                "name": "Raw Materials",  # unchanged
                "sort_order": "25",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        assert db_session.get(TaxonomyNode, node.id).sort_order == 25  # type: ignore[union-attr]

    def test_post_update_rejects_name_clash_with_other(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        a = TaxonomyNode(name="Raw Materials")
        b = TaxonomyNode(name="Tools")
        db_session.add_all([a, b])
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{b.id}",
            data={"name": "Raw Materials", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(TaxonomyNode, b.id).name == "Tools"  # type: ignore[union-attr]

    def test_post_update_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials")
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}",
            data={"name": "  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        assert db_session.get(TaxonomyNode, node.id).name == "Raw Materials"  # type: ignore[union-attr]

    def test_post_update_blank_sort_order_keeps_existing(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A blank ``sort_order`` on edit means "leave alone" — no silent reset to 0."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=42)
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}",
            data={
                "name": "Raw Materials",
                "sort_order": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        assert db_session.get(TaxonomyNode, node.id).sort_order == 42  # type: ignore[union-attr]

    def test_update_writes_audit_row_with_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}",
            data={
                "name": "Raw Materials",  # unchanged
                "sort_order": "20",  # changed
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="taxonomy_node.updated")
        assert len(rows) == 1
        # Only changed fields appear in the diff.
        assert rows[0].before_json == {"sort_order": 10}
        assert rows[0].after_json == {"sort_order": 20}

    def test_update_no_op_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}",
            data={
                "name": "Raw Materials",
                "sort_order": "10",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="taxonomy_node.updated") == []


# ---------------------------------------------------------------------------
# Archive / Unarchive
# ---------------------------------------------------------------------------


class TestTaxonomyArchive:
    def test_archive_sets_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials")
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, node.id)
        assert refreshed is not None
        assert refreshed.archived_at is not None

    def test_archive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials")
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = _audit_rows(db_session, action="taxonomy_node.archived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == node.id

    def test_archive_already_archived_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Idempotent — second archive call writes no row but still 303s."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="taxonomy_node.archived") == []

    def test_unarchive_clears_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, node.id)
        assert refreshed is not None
        assert refreshed.archived_at is None

    def test_unarchive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="taxonomy_node.unarchived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == node.id

    def test_unarchive_already_active_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials")  # already active
        db_session.add(node)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="taxonomy_node.unarchived") == []

    def test_archive_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archive_sub_category_via_top_route_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Raw Materials")
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{sub.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404
