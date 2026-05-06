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


# ===========================================================================
# Sub-category routes (S4)
# ===========================================================================


def _make_parent(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    parent = TaxonomyNode(name=name)
    db.add(parent)
    db.commit()
    db.refresh(parent)
    return parent


class TestSubCategoryRoleEnforcement:
    def test_anonymous_get_children_list_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_parent(db_session)
        resp = client.get(f"/admin/taxonomy/{parent.id}/children")
        assert resp.status_code == 401

    def test_workshop_get_children_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_parent(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get(f"/admin/taxonomy/{parent.id}/children")
        assert resp.status_code == 403

    def test_office_get_children_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_parent(db_session)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/taxonomy/{parent.id}/children")
        assert resp.status_code == 403

    def test_manager_get_children_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_parent(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/children")
        assert resp.status_code == 200

    def test_workshop_create_sub_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_parent(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Sneaky", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        # No row was created.
        assert (
            db_session.execute(
                select(TaxonomyNode).where(TaxonomyNode.parent_id.is_not(None))
            ).first()
            is None
        )


class TestSubCategoryList:
    def test_list_unknown_parent_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/9999/children")
        assert resp.status_code == 404

    def test_list_when_parent_is_actually_a_sub_category_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Listing children of a sub-cat would imply a third level. 400."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{sub.id}/children")
        assert resp.status_code == 400

    def test_list_shows_active_children_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        db_session.add_all(
            [
                TaxonomyNode(name="Silver", parent_id=parent.id, sort_order=10),
                TaxonomyNode(
                    name="Old Silver",
                    parent_id=parent.id,
                    sort_order=20,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{parent.id}/children")
        assert resp.status_code == 200
        assert "Silver" in resp.text
        assert "Old Silver" not in resp.text

    def test_list_show_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        db_session.add_all(
            [
                TaxonomyNode(name="Silver", parent_id=parent.id),
                TaxonomyNode(
                    name="Old Silver",
                    parent_id=parent.id,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{parent.id}/children?show=archived")
        assert resp.status_code == 200
        assert "Old Silver" in resp.text
        assert "Silver" not in resp.text or "Old Silver" in resp.text  # active hidden

    def test_list_excludes_other_parents_children(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Sub-cats under a different parent must not leak into the list."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent_a = _make_parent(db_session, "Raw Materials")
        parent_b = _make_parent(db_session, "Tools")
        db_session.add_all(
            [
                TaxonomyNode(name="Silver", parent_id=parent_a.id),
                TaxonomyNode(name="Hammer", parent_id=parent_b.id),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{parent_a.id}/children")
        assert "Silver" in resp.text
        assert "Hammer" not in resp.text

    def test_list_orders_by_sort_order_then_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        db_session.add_all(
            [
                TaxonomyNode(name="Zulu", parent_id=parent.id, sort_order=10),
                TaxonomyNode(name="Alpha", parent_id=parent.id, sort_order=20),
                TaxonomyNode(name="Bravo", parent_id=parent.id, sort_order=10),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{parent.id}/children")
        body = resp.text
        idx_bravo = body.find("Bravo")
        idx_zulu = body.find("Zulu")
        idx_alpha = body.find("Alpha")
        assert 0 < idx_bravo < idx_zulu < idx_alpha


class TestSubCategoryCreate:
    def test_get_new_form_renders_with_parent_context(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/{parent.id}/children/new")
        assert resp.status_code == 200
        assert "Raw Materials" in resp.text
        assert 'name="name"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_get_new_form_unknown_parent_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/9999/children/new")
        assert resp.status_code == 404

    def test_get_new_form_under_archived_parent_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(parent)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/children/new")
        assert resp.status_code == 400

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={
                "name": "Silver",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/taxonomy/{parent.id}/children"

        sub = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
        ).scalar_one()
        assert sub.name == "Silver"
        assert sub.sort_order == 5
        assert sub.archived_at is None

    def test_create_strips_whitespace(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "  Silver  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        sub = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
        ).scalar_one()
        assert sub.name == "Silver"

    def test_create_blank_sort_order_steps_by_10(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        db_session.add_all(
            [
                TaxonomyNode(name="A", parent_id=parent.id, sort_order=10),
                TaxonomyNode(name="B", parent_id=parent.id, sort_order=30),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "C", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        c = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.name == "C")
        ).scalar_one()
        assert c.sort_order == 40

    def test_create_blank_sort_order_zero_when_no_siblings(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "First", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        sub = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
        ).scalar_one()
        assert sub.sort_order == 0

    def test_create_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_rejects_duplicate_sibling_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        db_session.add(TaxonomyNode(name="Silver", parent_id=parent.id))
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        rows = list(
            db_session.execute(
                select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1

    def test_create_rejects_duplicate_archived_sibling_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archiving a sub-cat does not free its name within the same parent."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        db_session.add(
            TaxonomyNode(
                name="Silver",
                parent_id=parent.id,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_allows_same_name_under_different_parent(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        a = _make_parent(db_session, "Raw Materials")
        b = _make_parent(db_session, "Tools")
        db_session.add(TaxonomyNode(name="Silver", parent_id=a.id))
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{b.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_allows_same_name_as_top_level_category(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A top-level "Silver" and a sub-cat "Silver" don't share a namespace."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(TaxonomyNode(name="Silver"))  # top-level
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_unknown_parent_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/9999/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_create_under_sub_category_parent_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Depth-limit guard: parent must itself be top-level."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        db_session.refresh(sub)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{sub.id}/children",
            data={"name": "Hallmark", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_under_archived_parent_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(parent)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_writes_audit_row_with_parent_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={
                "name": "Silver",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        sub = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
        ).scalar_one()
        rows = _audit_rows(db_session, action="taxonomy_node.created")
        # parent + sub-cat audit rows; pick the one targeting the sub-cat.
        sub_row = next(r for r in rows if r.entity_id == sub.id)
        assert sub_row.actor_id == mgr.id
        assert sub_row.before_json is None
        assert sub_row.after_json == {
            "name": "Silver",
            "sort_order": 5,
            "parent_id": parent.id,
        }


class TestSubCategoryEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id, sort_order=20)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/taxonomy/sub/{sub.id}/edit")
        assert resp.status_code == 200
        assert "Silver" in resp.text
        assert 'value="20"' in resp.text
        assert "Raw Materials" in resp.text  # parent context

    def test_get_edit_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/sub/9999/edit")
        assert resp.status_code == 404

    def test_get_edit_top_level_via_sub_route_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The sub-cat edit URL only matches sub-cats. Top-level ids 404 here."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/sub/{parent.id}/edit")
        assert resp.status_code == 404

    def test_post_update_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id, sort_order=10)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub.id}",
            data={
                "name": "Silver Sheet",
                "sort_order": "15",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/taxonomy/{parent.id}/children"

        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, sub.id)
        assert refreshed is not None
        assert refreshed.name == "Silver Sheet"
        assert refreshed.sort_order == 15
        assert refreshed.parent_id == parent.id

    def test_post_update_top_level_via_sub_route_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/sub/{parent.id}",
            data={"name": "Renamed", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_post_update_rejects_sibling_name_clash(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        a = TaxonomyNode(name="Silver", parent_id=parent.id)
        b = TaxonomyNode(name="Gold", parent_id=parent.id)
        db_session.add_all([a, b])
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{b.id}",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.expire_all()
        b_after = db_session.get(TaxonomyNode, b.id)
        assert b_after is not None
        assert b_after.name == "Gold"

    def test_post_update_allows_same_name_in_different_parent(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Renaming a sub-cat to a name that exists under a different parent is fine."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent_a = _make_parent(db_session, "Raw Materials")
        parent_b = _make_parent(db_session, "Tools")
        db_session.add_all(
            [
                TaxonomyNode(name="Silver", parent_id=parent_a.id),
                TaxonomyNode(name="Hammer", parent_id=parent_b.id),
            ]
        )
        db_session.commit()
        hammer = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.name == "Hammer")
        ).scalar_one()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{hammer.id}",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        hammer_after = db_session.get(TaxonomyNode, hammer.id)
        assert hammer_after is not None
        assert hammer_after.name == "Silver"
        assert hammer_after.parent_id == parent_b.id

    def test_post_update_blank_sort_order_keeps_existing(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id, sort_order=42)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub.id}",
            data={
                "name": "Silver",
                "sort_order": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        sub_after = db_session.get(TaxonomyNode, sub.id)
        assert sub_after is not None
        assert sub_after.sort_order == 42

    def test_post_update_writes_audit_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id, sort_order=10)
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}",
            data={
                "name": "Silver",
                "sort_order": "20",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_node.updated")
            if r.entity_id == sub_id
        ]
        assert len(rows) == 1
        assert rows[0].before_json == {"sort_order": 10}
        assert rows[0].after_json == {"sort_order": 20}

    def test_post_update_no_op_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id, sort_order=10)
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}",
            data={
                "name": "Silver",
                "sort_order": "10",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_node.updated")
            if r.entity_id == sub_id
        ]
        assert rows == []


class TestSubCategoryArchive:
    def test_archive_sets_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/taxonomy/{parent.id}/children"

        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, sub_id)
        assert refreshed is not None
        assert refreshed.archived_at is not None

    def test_archive_already_archived_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(
            name="Silver",
            parent_id=parent.id,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_node.archived")
            if r.entity_id == sub_id
        ]
        assert rows == []

    def test_archive_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/sub/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archive_top_level_via_sub_route_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/sub/{parent.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_node.archived")
            if r.entity_id == sub_id
        ]
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id

    def test_unarchive_clears_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(
            name="Silver",
            parent_id=parent.id,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, sub_id)
        assert refreshed is not None
        assert refreshed.archived_at is None

    def test_unarchive_already_active_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = _make_parent(db_session)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_node.unarchived")
            if r.entity_id == sub_id
        ]
        assert rows == []

    def test_unarchive_top_level_via_sub_route_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(parent)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/sub/{parent.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archive_remains_allowed_under_archived_parent(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archiving an existing sub-cat is allowed even when the parent is archived.

        The "no new structure under archived" rule blocks creates, not cleanups.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Raw Materials", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        sub = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        sub_id = sub.id
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/sub/{sub_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyNode, sub_id)
        assert refreshed is not None
        assert refreshed.archived_at is not None


# ---------------------------------------------------------------------------
# CSV export — R5g
# ---------------------------------------------------------------------------
#
# Mirrors the locations / suppliers CSV blocks (R5d / R5f). The route inherits
# the existing Manager-only dependency for both branches; only the response
# shape changes.


class TestTaxonomyListCsvRoleEnforcement:
    """``?format=csv`` inherits the same Manager-only gate as the HTML branch."""

    def test_anonymous_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/taxonomy?format=csv")
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
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 403

    def test_office_csv_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Taxonomy is Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        off = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, off)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 200

    def test_admin_csv_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 200


class TestTaxonomyListCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="taxonomy_active.csv"' in cd

    def test_content_disposition_archived_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=csv&show=archived")
        cd = resp.headers["content-disposition"]
        assert 'filename="taxonomy_archived.csv"' in cd


class TestTaxonomyListCsvBody:
    def test_empty_emits_only_header_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 200
        assert resp.text == "id,sort_order,name\r\n"

    def test_one_node_one_data_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        node = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(node)
        db_session.commit()
        db_session.refresh(node)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        assert len(lines) == 3  # header + 1 data + trailing empty
        cells = lines[1].split(",")
        assert cells[0] == str(node.id)
        assert cells[1] == "10"
        assert cells[2] == "Raw Materials"

    def test_show_filter_applies_to_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        active = TaxonomyNode(name="Raw Materials", sort_order=10)
        archived = TaxonomyNode(
            name="Old Category",
            sort_order=20,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add_all([active, archived])
        db_session.commit()
        _login_as(client, mgr)

        # Default (active) → only the active row.
        resp = client.get("/admin/taxonomy?format=csv")
        body = resp.text
        assert "Raw Materials" in body
        assert "Old Category" not in body

        # show=archived → only the archived row.
        resp = client.get("/admin/taxonomy?format=csv&show=archived")
        body = resp.text
        assert "Old Category" in body
        assert "Raw Materials" not in body

    def test_sort_order_ordering_in_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Insert in non-ascending sort_order; the route orders by sort_order
        # ascending within the bucket, then name.
        db_session.add_all(
            [
                TaxonomyNode(name="Tools", sort_order=30),
                TaxonomyNode(name="Raw Materials", sort_order=10),
                TaxonomyNode(name="Consumables", sort_order=20),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy?format=csv")
        body = resp.text
        raw_pos = body.index("Raw Materials")
        cons_pos = body.index("Consumables")
        tools_pos = body.index("Tools")
        assert raw_pos < cons_pos < tools_pos

    def test_sub_categories_not_in_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Only top-level nodes (``parent_id IS NULL``) are exported.

        Sub-categories live under their own per-parent list view and are out
        of scope for this CSV surface.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(parent)
        db_session.commit()
        db_session.refresh(parent)
        sub = TaxonomyNode(name="Silver", sort_order=10, parent_id=parent.id)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy?format=csv")
        body = resp.text
        assert "Raw Materials" in body
        assert "Silver" not in body


class TestTaxonomyListCsvHtmlBranch:
    def test_format_blank_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="taxonomy-tabs"' in resp.text

    def test_format_unknown_renders_html(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestTaxonomyListCsvReadOnly:
    def test_csv_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        db_session.add(TaxonomyNode(name="Raw Materials", sort_order=10))
        db_session.commit()
        before = len(_audit_rows(db_session))
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy?format=csv")
        assert resp.status_code == 200
        after = len(_audit_rows(db_session))
        assert after == before


class TestTaxonomyListCsvLink:
    def test_html_renders_csv_link_with_active_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="taxonomy-list-csv-link"' in body
        assert "format=csv" in body
        assert "show=active" in body

    def test_html_renders_csv_link_with_archived_show(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy?show=archived")
        assert resp.status_code == 200
        body = resp.text
        assert 'data-testid="taxonomy-list-csv-link"' in body
        assert "show=archived" in body
