"""Integration tests for field-def inheritance (user-requested scope addition).

Field defs now inherit downward through the taxonomy: a field defined on a
top-level node is automatically visible on the item form for every leaf below
it. Sub-categories can still add their own additive fields on top.

Coverage:
- ``_get_active_field_defs`` collects own + ancestor fields, ordered root → leaf.
- New items on a child leaf carry the inherited field's value persisted as a
  normal ``item_field_values`` row.
- Item form GET on a leaf shows both inherited + own fields.
- Tree-wide key uniqueness: same key on ancestor + descendant is rejected.
- Tree-wide key uniqueness: same key on siblings is allowed (independent scope).
- Field-defs list view renders an "Inherited from" section for ancestor fields.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.items import _get_active_field_defs
from app.models import (
    Archetype,
    FieldType,
    Item,
    ItemFieldValue,
    Role,
    TaxonomyFieldDef,
    TaxonomyNode,
    User,
    UserStatus,
)

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


def _make_user(
    db: Session, *, email: str, role: Role | None = None
) -> User:
    user = User(
        google_sub=f"sub-{email}",
        email=email,
        name=email.split("@")[0].title(),
        role=role,
        status=UserStatus.ACTIVE,
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


def _make_top(db: Session, name: str = "Rings", sku_prefix: str = "RNG", archetype: Archetype = Archetype.BULK) -> TaxonomyNode:
    n = TaxonomyNode(name=name, sku_prefix=sku_prefix, archetype=archetype)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_sub(db: Session, parent: TaxonomyNode, name: str, sku_prefix: str) -> TaxonomyNode:
    n = TaxonomyNode(name=name, parent_id=parent.id, sku_prefix=sku_prefix)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _add_field(
    db: Session,
    *,
    node: TaxonomyNode,
    name: str,
    key: str | None = None,
    field_type: FieldType = FieldType.TEXT,
    sort_order: int = 0,
    required: bool = False,
) -> TaxonomyFieldDef:
    f = TaxonomyFieldDef(
        node_id=node.id,
        name=name,
        key=key or name.lower().replace(" ", "_"),
        type=field_type,
        sort_order=sort_order,
        required=required,
    )
    db.add(f)
    db.commit()
    db.refresh(f)
    return f


# ---------------------------------------------------------------------------
# _get_active_field_defs walks the parent chain
# ---------------------------------------------------------------------------


class TestEffectiveFieldDefsWalk:
    def test_leaf_inherits_from_top_level(self, db_session: Session) -> None:
        top = _make_top(db_session, "Rings")
        sub = _make_sub(db_session, top, "Silver", "SIL")

        top_field = _add_field(db_session, node=top, name="Karat", sort_order=0)
        sub_field = _add_field(db_session, node=sub, name="Polish", sort_order=0)

        effective = _get_active_field_defs(db_session, sub.id)
        # Root-first ordering: top-level fields appear before sub fields.
        assert [f.id for f in effective] == [top_field.id, sub_field.id]

    def test_top_level_sees_only_own_fields(self, db_session: Session) -> None:
        top = _make_top(db_session, "Rings")
        sub = _make_sub(db_session, top, "Silver", "SIL")
        own = _add_field(db_session, node=top, name="Karat")
        _add_field(db_session, node=sub, name="Polish")

        effective = _get_active_field_defs(db_session, top.id)
        assert [f.id for f in effective] == [own.id]

    def test_archived_ancestor_field_is_excluded(self, db_session: Session) -> None:
        top = _make_top(db_session)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        archived = _add_field(db_session, node=top, name="OldKarat")
        archived.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        active_own = _add_field(db_session, node=sub, name="Polish")
        db_session.commit()

        effective = _get_active_field_defs(db_session, sub.id)
        assert [f.id for f in effective] == [active_own.id]

    def test_three_level_chain_collects_in_order(self, db_session: Session) -> None:
        top = _make_top(db_session, "Rings")
        sub = _make_sub(db_session, top, "Silver", "SIL")
        leaf = _make_sub(db_session, sub, "925", "925")  # depth-2

        a = _add_field(db_session, node=top, name="Karat", sort_order=0)
        b = _add_field(db_session, node=sub, name="Polish", sort_order=0)
        c = _add_field(db_session, node=leaf, name="HallmarkBatch", sort_order=0)

        effective = _get_active_field_defs(db_session, leaf.id)
        assert [f.id for f in effective] == [a.id, b.id, c.id]


# ---------------------------------------------------------------------------
# Tree-wide key uniqueness
# ---------------------------------------------------------------------------


class TestTreeWideKeyUniqueness:
    def test_same_key_on_ancestor_rejects_descendant_create(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        _add_field(db_session, node=top, name="Karat", key="karat")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{sub.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_same_key_on_descendant_rejects_ancestor_create(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        _add_field(db_session, node=sub, name="Karat", key="karat")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{top.id}/fields",
            data={
                "name": "Karat",
                "type": "text",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_same_key_on_siblings_is_allowed(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Siblings under the same parent each scope their keys independently."""
        top = _make_top(db_session)
        sub_a = _make_sub(db_session, top, "Silver", "SIL")
        sub_b = _make_sub(db_session, top, "Gold", "GLD")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)

        resp_a = client.post(
            f"/admin/taxonomy/{sub_a.id}/fields",
            data={"name": "Karat", "type": "text", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp_a.status_code == 303

        resp_b = client.post(
            f"/admin/taxonomy/{sub_b.id}/fields",
            data={"name": "Karat", "type": "text", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp_b.status_code == 303

    def test_archived_ancestor_field_does_not_block(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        archived = _add_field(db_session, node=top, name="Karat", key="karat")
        archived.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{sub.id}/fields",
            data={"name": "Karat", "type": "text", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303


# ---------------------------------------------------------------------------
# Item create on a child leaf persists inherited field values
# ---------------------------------------------------------------------------


class TestItemCreateConsumesInheritedFields:
    def test_inherited_field_persists_on_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session, "Rings", "RNG", archetype=Archetype.BULK)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        # Field defined on the *parent*; sub-cat inherits it.
        parent_field = _add_field(db_session, node=top, name="Karat")
        # Sub-cat also has its own.
        own_field = _add_field(db_session, node=sub, name="Polish")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data={
                "name": "Plain band",
                "taxonomy_node_id": str(sub.id),
                "unit": "ea",
                "tracking_mode": "qty",
                "reorder_threshold": "0",
                "reorder_qty": "0",
                f"cf_{parent_field.key}": "18K",
                f"cf_{own_field.key}": "mirror",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        item = db_session.execute(select(Item)).scalar_one()
        values = {
            v.field_def_id: v.value_text
            for v in db_session.execute(
                select(ItemFieldValue).where(ItemFieldValue.item_id == item.id)
            )
            .scalars()
            .all()
        }
        assert values == {parent_field.id: "18K", own_field.id: "mirror"}

    def test_form_get_renders_inherited_field(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session, "Rings", "RNG", archetype=Archetype.BULK)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        _add_field(db_session, node=top, name="ParentField")
        _add_field(db_session, node=sub, name="SubField")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/_custom-fields?taxonomy_node_id={sub.id}")
        assert resp.status_code == 200
        # Both labels appear; order: parent first per root→leaf walk.
        body = resp.text
        assert body.find("ParentField") < body.find("SubField")


# ---------------------------------------------------------------------------
# Field-defs list view shows inherited block
# ---------------------------------------------------------------------------


class TestFieldDefsListShowsInherited:
    def test_inherited_section_rendered_on_child(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session)
        sub = _make_sub(db_session, top, "Silver", "SIL")
        _add_field(db_session, node=top, name="Karat")
        _add_field(db_session, node=sub, name="Polish")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{sub.id}/fields")
        assert resp.status_code == 200
        assert "inherited-fields-heading" in resp.text
        assert "inherited-fields-group" in resp.text
        # The "Karat" row appears under the inherited table.
        assert "Karat" in resp.text
        # And the sub's own "Polish" still appears under the own-fields table.
        assert "Polish" in resp.text

    def test_no_inherited_section_when_root(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top(db_session)
        _add_field(db_session, node=top, name="Karat")

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{top.id}/fields")
        assert resp.status_code == 200
        assert "inherited-fields-heading" not in resp.text
