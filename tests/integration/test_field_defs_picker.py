"""Integration tests for the catalog-driven ``POST /admin/taxonomy/{node_id}/fields/pick`` route.

Slice 3 of the catalog refactor introduces the "pick from catalog" flow.
These tests cover:
- Happy path: picking a catalog entry materialises a ``TaxonomyFieldDef``
  with ``catalog_key`` populated and the name/type/options from the catalog.
- Audit-log row written with ``action="taxonomy_field_def.picked_from_catalog"``.
- Picking an unknown catalog key returns 400 and inserts nothing.
- Picking the same entry twice on the same node returns 400.
- Picking an entry already picked on an ancestor returns 400.
- Picking an entry already picked on a descendant returns 400.
- Sibling collisions are allowed (two sub-cats under the same parent both
  picking the same entry).
- Picker page lists only entries not yet picked anywhere in the tree.
- Role enforcement: workshop / office / pending all 403.

Free-text ``POST /fields`` remains for backwards-compat in slice 3 and gets
its own coverage in ``test_field_defs_routes.py``; this file only exercises
the new pick flow.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.field_catalog import CATALOG_BY_KEY
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


def _make_node(db: Session, *, name: str = "Raw Materials") -> TaxonomyNode:
    node = TaxonomyNode(name=name)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_sub(db: Session, parent: TaxonomyNode, *, name: str) -> TaxonomyNode:
    sub = TaxonomyNode(name=name, parent_id=parent.id)
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def _manager(db: Session, *, email: str = "mgr@uc.test") -> User:
    return _make_user(db, email=email, role=Role.MANAGER)


def _pick(client: TestClient, node_id: int, catalog_key: str) -> object:
    return client.post(
        f"/admin/taxonomy/{node_id}/fields/pick",
        data={"catalog_key": catalog_key, "csrf_token": _csrf(client)},
        follow_redirects=False,
    )


class TestPickHappyPath:
    def test_picking_karat_creates_a_row_with_catalog_metadata(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        mgr = _manager(db_session)
        _login_as(client, mgr)

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/taxonomy/{node.id}/fields"

        rows = list(db_session.execute(select(TaxonomyFieldDef)).scalars().all())
        assert len(rows) == 1
        fd = rows[0]
        entry = CATALOG_BY_KEY["karat"]
        assert fd.catalog_key == "karat"
        assert fd.key == "karat"
        assert fd.name == entry.label
        assert fd.type == FieldType.SELECT
        assert fd.options_json == list(entry.options)
        assert fd.required is False

    def test_picking_a_field_value_entry_persists_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        _login_as(client, _manager(db_session))

        resp = _pick(client, node.id, "material")
        assert resp.status_code == 303

        fd = db_session.execute(select(TaxonomyFieldDef)).scalars().one()
        assert fd.catalog_key == "material"
        assert fd.options_json is not None
        assert "Silver" in fd.options_json

    def test_picking_a_text_entry_leaves_options_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        _login_as(client, _manager(db_session))

        resp = _pick(client, node.id, "hallmark")
        assert resp.status_code == 303

        fd = db_session.execute(select(TaxonomyFieldDef)).scalars().one()
        assert fd.catalog_key == "hallmark"
        assert fd.options_json is None

    def test_pick_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _manager(db_session))

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 303

        rows = list(
            db_session.execute(
                select(AuditLog)
                .where(AuditLog.entity_type == "taxonomy_field_def")
                .where(AuditLog.action == "taxonomy_field_def.picked_from_catalog")
            ).scalars().all()
        )
        assert len(rows) == 1
        audit = rows[0]
        assert audit.before_json is None
        after = audit.after_json
        assert after is not None
        assert after["catalog_key"] == "karat"
        assert after["node_id"] == node.id
        assert after["storage"] == "field_value"


class TestPickValidationFailures:
    def test_unknown_catalog_key_is_400(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _manager(db_session))

        resp = _pick(client, node.id, "not-a-real-key")
        assert resp.status_code == 400
        assert db_session.execute(select(TaxonomyFieldDef)).first() is None

    def test_empty_catalog_key_is_400(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        _login_as(client, _manager(db_session))

        resp = _pick(client, node.id, "")
        assert resp.status_code == 400

    def test_picking_same_key_on_same_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        _login_as(client, _manager(db_session))

        first = _pick(client, node.id, "karat")
        assert first.status_code == 303
        second = _pick(client, node.id, "karat")
        assert second.status_code == 400

        rows = list(db_session.execute(select(TaxonomyFieldDef)).scalars().all())
        assert len(rows) == 1

    def test_pick_on_archived_node_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        from datetime import UTC, datetime

        node.archived_at = datetime.now(UTC)
        db_session.commit()
        _login_as(client, _manager(db_session))

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 400

    def test_unknown_node_is_404(self, client: TestClient, db_session: Session) -> None:
        _login_as(client, _manager(db_session))
        resp = _pick(client, 99999, "karat")
        assert resp.status_code == 404


class TestPickTreeUniqueness:
    def test_ancestor_already_picked_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Rings")
        child = _make_sub(db_session, parent, name="Silver")
        _login_as(client, _manager(db_session))

        first = _pick(client, parent.id, "karat")
        assert first.status_code == 303

        second = _pick(client, child.id, "karat")
        assert second.status_code == 400

        rows = list(db_session.execute(select(TaxonomyFieldDef)).scalars().all())
        assert len(rows) == 1
        assert rows[0].node_id == parent.id

    def test_descendant_already_picked_is_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Rings")
        child = _make_sub(db_session, parent, name="Silver")
        _login_as(client, _manager(db_session))

        first = _pick(client, child.id, "karat")
        assert first.status_code == 303

        second = _pick(client, parent.id, "karat")
        assert second.status_code == 400

        rows = list(db_session.execute(select(TaxonomyFieldDef)).scalars().all())
        assert len(rows) == 1
        assert rows[0].node_id == child.id

    def test_sibling_can_pick_same_entry(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Rings")
        silver = _make_sub(db_session, parent, name="Silver")
        gold = _make_sub(db_session, parent, name="Gold")
        _login_as(client, _manager(db_session))

        first = _pick(client, silver.id, "karat")
        assert first.status_code == 303
        second = _pick(client, gold.id, "karat")
        assert second.status_code == 303

        rows = list(db_session.execute(select(TaxonomyFieldDef)).scalars().all())
        assert len(rows) == 2
        assert {r.node_id for r in rows} == {silver.id, gold.id}


class TestPickerListing:
    def test_list_view_filters_out_picked_entries_within_tree(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Rings")
        child = _make_sub(db_session, parent, name="Silver")
        _login_as(client, _manager(db_session))

        _pick(client, parent.id, "karat")

        resp = client.get(f"/admin/taxonomy/{child.id}/fields")
        assert resp.status_code == 200
        body = resp.text
        # Karat is picked on the ancestor — must not appear in the
        # descendant's picker dropdown.
        assert 'value="karat"' not in body
        # An unrelated entry still appears.
        assert 'value="weight_grams"' in body


class TestPickRoleEnforcement:
    def test_workshop_pick_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 403
        assert db_session.execute(select(TaxonomyFieldDef)).first() is None

    def test_office_pick_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 403

    def test_pending_pick_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        pending = _make_user(
            db_session, email="p@x.test", role=Role.MANAGER, status=UserStatus.PENDING
        )
        _login_as(client, pending)

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 403

    def test_anonymous_pick_is_401(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 401

    def test_admin_can_pick(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)

        resp = _pick(client, node.id, "karat")
        assert resp.status_code == 303
