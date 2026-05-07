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


def _make_sub(
    db: Session, parent: TaxonomyNode, name: str = "Silver"
) -> TaxonomyNode:
    sub = TaxonomyNode(name=name, parent_id=parent.id)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _audit_rows(
    db: Session, *, action: str | None = None
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "taxonomy_field_def")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestFieldDefRoleEnforcement:
    def test_anonymous_get_list_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 403

    def test_office_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200

    def test_pending_user_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_workshop_create_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={"name": "Sneaky", "type": "text", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(TaxonomyFieldDef)).first() is None


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestFieldDefList:
    def test_list_unknown_node_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/9999/fields")
        assert resp.status_code == 404

    def test_list_empty_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200
        assert "field-defs-empty" in resp.text

    def test_list_shows_active_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_list_show_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
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
                TaxonomyFieldDef(
                    node_id=a.id, name="Karat", key="karat", type=FieldType.TEXT
                ),
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

    def test_list_renders_non_leaf_note_when_node_has_active_children(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, "Raw Materials")
        _make_sub(db_session, parent, "Silver")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/fields")
        assert resp.status_code == 200
        assert "non-leaf-note" in resp.text
        assert "new-field-def" not in resp.text  # CTA hidden

    def test_list_for_archived_node_shows_note_and_no_cta(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = TaxonomyNode(
            name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(node)
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields")
        assert resp.status_code == 200
        assert "node-archived-note" in resp.text
        assert "new-field-def" not in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestFieldDefCreate:
    def test_get_new_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields/new")
        assert resp.status_code == 200
        assert 'name="name"' in resp.text
        assert 'name="type"' in resp.text
        # options_text is HTMX-swapped in only when type is select/multiselect.
        # The default type on a new form is text, so the textarea is absent.
        # The container is always present so the HTMX target exists.
        assert 'id="fd-options-container"' in resp.text
        assert 'name="options_text"' not in resp.text
        assert 'name="required"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_get_new_form_unknown_node_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/9999/fields/new")
        assert resp.status_code == 404

    def test_get_new_form_under_archived_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = TaxonomyNode(
            name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(node)
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields/new")
        assert resp.status_code == 400

    def test_get_new_form_under_non_leaf_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session)
        _make_sub(db_session, parent)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/fields/new")
        assert resp.status_code == 400

    def test_get_new_form_under_top_level_with_only_archived_children_is_ok(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archived children don't count: the node is still a leaf."""
        parent = _make_node(db_session)
        db_session.add(
            TaxonomyNode(
                name="OldSub",
                parent_id=parent.id,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{parent.id}/fields/new")
        assert resp.status_code == 200

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "options_text": "",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert (
            resp.headers["location"] == f"/admin/taxonomy/{node.id}/fields"
        )

        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert f.node_id == node.id
        assert f.name == "Karat"
        assert f.key == "karat"
        assert f.type == FieldType.TEXT
        assert f.options_json is None
        assert f.required is False
        assert f.sort_order == 5

    def test_create_strips_whitespace(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "   Karat  ",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert f.name == "Karat"
        assert f.key == "karat"

    def test_create_derives_key_from_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Density (g/cm³)",
                "type": "decimal",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        # Non-alphanumeric runs collapse to single underscores; trailing
        # underscores are stripped.
        assert f.key == "density_g_cm"

    def test_create_rejects_name_with_no_alphanumeric(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "!!!",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(TaxonomyFieldDef)).first() is None

    def test_create_rejects_empty_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_rejects_unknown_type(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "rocketlauncher",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_select_requires_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "select",
                "options_text": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_multiselect_requires_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Finish",
                "type": "multiselect",
                "options_text": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_select_with_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "select",
                "options_text": "9\n14\n  18  \n",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert f.options_json == ["9", "14", "18"]

    def test_create_select_rejects_duplicate_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "select",
                "options_text": "9\n14\n14",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_options_rejected_for_non_select_type(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "options_text": "9\n14",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_required_checkbox_stored(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "required": "true",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert f.required is True

    def test_create_required_unchecked_stores_false(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                # No "required" key — checkbox unchecked.
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert f.required is False

    def test_create_blank_sort_order_steps_by_10(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        db_session.add_all(
            [
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="A",
                    key="a",
                    type=FieldType.TEXT,
                    sort_order=10,
                ),
                TaxonomyFieldDef(
                    node_id=node.id,
                    name="B",
                    key="b",
                    type=FieldType.TEXT,
                    sort_order=30,
                ),
            ]
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={"name": "C", "type": "text", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        c = db_session.execute(
            select(TaxonomyFieldDef).where(TaxonomyFieldDef.name == "C")
        ).scalar_one()
        assert c.sort_order == 40

    def test_create_blank_sort_order_zero_when_first(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "First",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert f.sort_order == 0

    def test_create_rejects_invalid_sort_order(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "sort_order": "first",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_rejects_duplicate_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        db_session.add(
            TaxonomyFieldDef(
                node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_rejects_duplicate_archived_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        db_session.add(
            TaxonomyFieldDef(
                node_id=node.id,
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
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_rejects_key_collision_via_different_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Two names that derive to the same key must collide on the key index."""
        node = _make_node(db_session)
        db_session.add(
            TaxonomyFieldDef(
                node_id=node.id,
                name="Karat",
                key="karat",
                type=FieldType.TEXT,
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # Different name, but lower-case slug also "karat".
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "KARAT",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_allows_same_name_under_different_node(
        self, client: TestClient, db_session: Session
    ) -> None:
        a = _make_node(db_session, "Raw Materials")
        b = _make_node(db_session, "Tools")
        db_session.add(
            TaxonomyFieldDef(
                node_id=a.id, name="Karat", key="karat", type=FieldType.TEXT
            )
        )
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{b.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_under_non_leaf_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session)
        _make_sub(db_session, parent)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_under_archived_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = TaxonomyNode(
            name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(node)
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/fields",
            data={
                "name": "Karat",
                "type": "select",
                "options_text": "9\n14\n18",
                "required": "true",
                "sort_order": "5",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = _audit_rows(db_session, action="taxonomy_field_def.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        f = db_session.execute(select(TaxonomyFieldDef)).scalar_one()
        assert row.entity_id == f.id
        assert row.before_json is None
        assert row.after_json == {
            "node_id": node.id,
            "name": "Karat",
            "key": "karat",
            "type": "select",
            "options_json": ["9", "14", "18"],
            "required": True,
            "sort_order": 5,
        }


# ---------------------------------------------------------------------------
# Edit / Update
# ---------------------------------------------------------------------------


class TestFieldDefEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.SELECT,
            options_json=["9", "14"],
            sort_order=20,
        )
        db_session.add(f)
        db_session.commit()
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/fields/{f.id}/edit")
        assert resp.status_code == 200
        assert "Karat" in resp.text
        assert "9\n14" in resp.text  # textarea pre-filled

    def test_get_edit_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy/fields/9999/edit")
        assert resp.status_code == 404

    def test_post_update_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            sort_order=10,
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat 18",
                "type": "text",
                "sort_order": "20",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/taxonomy/{node.id}/fields"
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.name == "Karat 18"
        assert refreshed.key == "karat_18"  # rename re-derives slug
        assert refreshed.sort_order == 20

    def test_post_update_rename_to_same_slug_keeps_key(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Casing-only rename (Karat → karat) keeps the key (still "karat")."""
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.name == "karat"
        assert refreshed.key == "karat"

    def test_post_update_blank_sort_order_keeps_existing(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            sort_order=42,
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat",
                "type": "text",
                "sort_order": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.sort_order == 42

    def test_post_update_changes_type_and_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat",
                "type": "select",
                "options_text": "9\n14",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.type == FieldType.SELECT
        assert refreshed.options_json == ["9", "14"]

    def test_post_update_required_toggle(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            required=False,
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat",
                "type": "text",
                "required": "true",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.required is True

    def test_post_update_rejects_sibling_name_clash(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        a = TaxonomyFieldDef(
            node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
        )
        b = TaxonomyFieldDef(
            node_id=node.id, name="Other", key="other", type=FieldType.TEXT
        )
        db_session.add_all([a, b])
        db_session.commit()
        b_id = b.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{b_id}",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_post_update_writes_audit_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            sort_order=10,
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat",
                "type": "text",
                "sort_order": "20",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_field_def.updated")
            if r.entity_id == f_id
        ]
        assert len(rows) == 1
        assert rows[0].before_json == {"sort_order": 10}
        assert rows[0].after_json == {"sort_order": 20}

    def test_post_update_records_key_change_in_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat 18",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_field_def.updated")
            if r.entity_id == f_id
        ]
        assert len(rows) == 1
        assert rows[0].before_json == {"name": "Karat", "key": "karat"}
        assert rows[0].after_json == {"name": "Karat 18", "key": "karat_18"}

    def test_post_update_no_op_writes_no_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.TEXT,
            sort_order=10,
        )
        db_session.add(f)
        db_session.commit()
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}",
            data={
                "name": "Karat",
                "type": "text",
                "sort_order": "10",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = [
            r
            for r in _audit_rows(db_session, action="taxonomy_field_def.updated")
            if r.entity_id == f_id
        ]
        assert rows == []


# ---------------------------------------------------------------------------
# Archive / Unarchive
# ---------------------------------------------------------------------------


class TestFieldDefArchive:
    def test_archive_sets_archived_at(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
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
        assert resp.headers["location"] == f"/admin/taxonomy/{node.id}/fields"
        db_session.expire_all()
        refreshed = db_session.get(TaxonomyFieldDef, f_id)
        assert refreshed is not None
        assert refreshed.archived_at is not None

    def test_archive_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        f = TaxonomyFieldDef(
            node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
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

    def test_archive_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/fields/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_unarchive_clears_archived_at(
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
        f = TaxonomyFieldDef(
            node_id=node.id, name="Karat", key="karat", type=FieldType.TEXT
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
        rows = _audit_rows(db_session, action="taxonomy_field_def.unarchived")
        assert rows == []

    def test_unarchive_on_non_leaf_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Symmetric with the create-time leaf check.

        Sequence: create field on a leaf (top-level with no children), archive
        it, then add a sub-cat (which un-leafs the parent). Unarchiving the
        archived field def should now be rejected — schema doesn't apply.
        """
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
        # Add a sub-cat that un-leafs the parent (allowed because the only
        # field def is archived).
        _make_sub(db_session, parent)
        f_id = f.id
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/fields/{f_id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unarchive_on_archived_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = TaxonomyNode(
            name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
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

    def test_unarchive_unknown_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/taxonomy/fields/9999/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cross-cutting: sub-cat create blocked when parent has active field defs.
# ---------------------------------------------------------------------------


class TestSubCategoryCreateGuardedByActiveFieldDefs:
    def test_create_sub_under_node_with_active_field_def_is_400(
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
        resp = client.post(
            f"/admin/taxonomy/{parent.id}/children",
            data={"name": "Silver", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert (
            db_session.execute(
                select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
            ).first()
            is None
        )

    def test_new_sub_form_GET_under_node_with_active_field_def_is_400(
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
        assert resp.status_code == 400

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


class TestFieldDefOptionsFragmentRoute:
    """``GET /admin/taxonomy/fields/_options-partial`` — HTMX fragment for the
    field-def form's type ``<select>``.

    When the user picks ``select`` or ``multiselect`` the options textarea
    appears; when the user picks any other type the textarea disappears.
    Without this swap the textarea was always rendered and the server 400'd
    on submit when the user typed options under a non-select type.
    Manager-only — same gate as the field-def form itself.
    """

    def test_anon_blocked(self, client: TestClient) -> None:
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=select"
        )
        assert resp.status_code == 401

    def test_office_blocked(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=select"
        )
        assert resp.status_code == 403

    def test_workshop_blocked(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=select"
        )
        assert resp.status_code == 403

    def test_select_renders_options_textarea(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=select"
        )
        assert resp.status_code == 200
        assert 'name="options_text"' in resp.text
        assert 'data-testid="field-def-options-input"' in resp.text

    def test_multiselect_renders_options_textarea(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=multiselect"
        )
        assert resp.status_code == 200
        assert 'name="options_text"' in resp.text

    def test_text_hides_options_textarea(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=text"
        )
        assert resp.status_code == 200
        assert 'name="options_text"' not in resp.text

    def test_number_hides_options_textarea(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=number"
        )
        assert resp.status_code == 200
        assert 'name="options_text"' not in resp.text

    def test_boolean_hides_options_textarea(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=boolean"
        )
        assert resp.status_code == 200
        assert 'name="options_text"' not in resp.text

    def test_blank_type_hides_options_textarea(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/taxonomy/fields/_options-partial")
        assert resp.status_code == 200
        assert 'name="options_text"' not in resp.text

    def test_options_text_round_trips(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(
            "/admin/taxonomy/fields/_options-partial?type=select&options_text=9%0A14%0A18"
        )
        assert resp.status_code == 200
        # Newlines round-trip via the textarea body so a user mid-edit
        # doesn't lose typed options when flipping select <-> multiselect.
        assert "9" in resp.text
        assert "14" in resp.text
        assert "18" in resp.text


class TestFieldDefFormHtmxWiring:
    """The field-def form's type ``<select>`` carries the HTMX attributes
    that drive the options-partial swap."""

    def test_new_form_type_select_has_htmx_get(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/admin/taxonomy/{node.id}/fields/new")
        assert resp.status_code == 200
        assert 'hx-get="/admin/taxonomy/fields/_options-partial"' in resp.text
        assert 'hx-target="#fd-options-container"' in resp.text
        assert 'id="fd-options-container"' in resp.text

    def test_edit_form_with_select_field_pre_renders_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        fd = TaxonomyFieldDef(
            node_id=node.id,
            name="Karat",
            key="karat",
            type=FieldType.SELECT,
            options_json=["9", "14", "18"],
        )
        db_session.add(fd)
        db_session.commit()
        db_session.refresh(fd)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/admin/taxonomy/fields/{fd.id}/edit")
        assert resp.status_code == 200
        # Server-side render: select-typed field already has options visible
        # without HTMX needing to fire.
        assert 'name="options_text"' in resp.text
        assert "9" in resp.text
        assert "18" in resp.text

    def test_edit_form_with_text_field_does_not_render_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        fd = TaxonomyFieldDef(
            node_id=node.id,
            name="Alloy",
            key="alloy",
            type=FieldType.TEXT,
        )
        db_session.add(fd)
        db_session.commit()
        db_session.refresh(fd)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/admin/taxonomy/fields/{fd.id}/edit")
        assert resp.status_code == 200
        assert 'name="options_text"' not in resp.text
        # Container still present so HTMX can swap when type is changed.
        assert 'id="fd-options-container"' in resp.text
