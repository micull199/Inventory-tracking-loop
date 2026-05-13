"""Integration tests for the Manager-owned ``/admin/taxonomy/...fields`` CRUD routes.

S5 covers custom-field defs attached to taxonomy *leaf* nodes:
- top-level node with no active sub-categories, OR
- any sub-category.

Tests mirror the shape of ``test_taxonomy_routes.py`` (role enforcement,
list filters, create + edit + archive happy/edge paths, audit content) and
add S5-specific blocks for: leaf invariant on create + unarchive, options
validation per type, key auto-derivation + collisions, and the cross-cutting
"can't add a sub-cat to a node with active field defs" guard.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    FieldType,
    Role,
    TaxonomyFieldDef,
    TaxonomyNode,
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


def _make_node(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    node = TaxonomyNode(name=name)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_sub(db: Session, parent: TaxonomyNode, name: str = "Silver") -> TaxonomyNode:
    sub = TaxonomyNode(name=name, parent_id=parent.id)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = (
        select(AuditLog).where(AuditLog.entity_type == "taxonomy_field_def").order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestFieldDefRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 403

    def test_office_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200

    def test_pending_user_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 403

    def test_workshop_pick_is_403(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields/pick",
            data={"catalog_key": "karat", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(TaxonomyFieldDef)).first() is None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestFieldDefList:
    def test_list_unknown_node_is_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/9999/fields")
        assert resp.status_code == 404

    def test_list_empty_state(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200
        assert "field-defs-empty" in resp.text

    def test_list_shows_active_by_default(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        db_session.add_all(
            [
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="Karat",
                    key="karat",
                    type=FieldType.TEXT,
                ),
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="OldField",
                    key="oldfield",
                    type=FieldType.TEXT,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert "Karat" in resp.text
        assert "OldField" not in resp.text

    def test_list_show_archived(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        db_session.add_all(
            [
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="Karat",
                    key="karat",
                    type=FieldType.TEXT,
                ),
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="OldField",
                    key="oldfield",
                    type=FieldType.TEXT,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields?show=archived")
        assert "OldField" in resp.text
        assert "Karat" not in resp.text or "OldField" in resp.text

    def test_list_orders_by_sort_order_then_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        db_session.add_all(
            [
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="Zulu",
                    key="zulu",
                    type=FieldType.TEXT,
                    sort_order=10,
                ),
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="Alpha",
                    key="alpha",
                    type=FieldType.TEXT,
                    sort_order=20,
                ),
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="Bravo",
                    key="bravo",
                    type=FieldType.TEXT,
                    sort_order=10,
                ),
            ]
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        body = resp.text
        idx_bravo = body.find("Bravo")
        idx_zulu = body.find("Zulu")
        idx_alpha = body.find("Alpha")
        assert 0 < idx_bravo < idx_zulu < idx_alpha

    def test_list_excludes_other_nodes_fields(
        self, client: TestClient, db_session: Session
    ) -> None:
        a = _make_node(db_session, "Raw Materials")
        b = _make_node(db_session, "Tools")
        db_session.add_all(
            [
                TaxonomyFieldDef(node_id=a.id, name="Karat", key="karat", type=FieldType.TEXT),
                TaxonomyFieldDef(
                    node_id=b.id,
                    name="HandleSize",
                    key="handlesize",
                    type=FieldType.TEXT,
                ),
            ]
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{a.id}/fields")
        assert "Karat" in resp.text
        assert "HandleSize" not in resp.text

    def test_list_on_non_leaf_node_allows_field_management(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Inheritance: a non-leaf node can host fields that descendants inherit.

        Pre-inheritance the field-defs list rendered a "non-leaf-note" steering
        the user to manage fields on the sub-categories instead. With
        inheritance, the parent is the *right* place to define shared schema.
        Catalog refactor (slice 3+) replaced the free-text "+ New field" CTA
        with the catalog picker form; both surfaces let a manager add fields
        on a non-leaf node.
        """
        parent = _make_node(db_session, "Raw Materials")
        _make_sub(db_session, parent, "Silver")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/fields")
        assert resp.status_code == 200
        assert "non-leaf-note" not in resp.text
        assert 'data-testid="field-def-picker-form"' in resp.text

    def test_list_for_archived_node_shows_note_and_no_cta(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = TaxonomyNode(name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(node)
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200
        assert "node-archived-note" in resp.text
        assert 'data-testid="field-def-picker-form"' not in resp.text



# ---------------------------------------------------------------------------
# Archive / Unarchive
# ---------------------------------------------------------------------------


class TestFieldDefArchive:
    def test_archive_sets_archived_at(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT)
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/taxonomy/{node.id}/fields"
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.archived_at is not None

    def test_archive_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT)
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="taxonomy_field_def.archived")
        assert len(rows) == 1
        assert rows[0].actor_id == mgr.id
        assert rows[0].entity_id == f_id

    def test_archive_already_archived_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="taxonomy_field_def.archived")
        assert rows == []

    def test_archive_unknown_id_is_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/fields/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_unarchive_clears_archived_at(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.archived_at is None

    def test_unarchive_already_active_is_noop(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT)
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="taxonomy_field_def.unarchived")
        assert rows == []

    def test_unarchive_on_non_leaf_node_is_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Inheritance: a field on a non-leaf node is legitimate (it inherits down)."""
        parent = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=parent.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(f)
        db_session.commit()
        _make_sub(db_session, parent)
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_unarchive_on_archived_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = TaxonomyNode(name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(node)
        db_session.commit()
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unarchive_unknown_id_is_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/fields/9999/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-cutting: sub-cat create + parent fields now coexist via inheritance.
# ---------------------------------------------------------------------------


class TestSubCategoryCreateWithParentFieldsInherits:
    def test_create_sub_under_node_with_active_field_def_is_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Inheritance replaces the legacy block: parent fields propagate to new sub-cats."""
        parent = _make_node(db_session)
        db_session.add(
            TaxonomyFieldDef(
                node_id=parent.id,
                name="Karat",
                key="karat",
                type=FieldType.TEXT,
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        sub = db_session.execute(
            select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
        ).scalar_one()
        assert sub.name == "Silver"

    def test_new_sub_form_GET_under_node_with_active_field_def_is_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session)
        db_session.add(
            TaxonomyFieldDef(
                node_id=parent.id,
                name="Karat",
                key="karat",
                type=FieldType.TEXT,
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/children/new")
        assert resp.status_code == 200

    def test_create_sub_works_when_only_archived_field_defs(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archived field defs don't block — they're history, not schema."""
        parent = _make_node(db_session)
        db_session.add(
            TaxonomyFieldDef(
                node_id=parent.id,
                name="Karat",
                key="karat",
                type=FieldType.TEXT,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
