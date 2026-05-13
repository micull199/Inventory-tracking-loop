"""Integration tests for slice 5: category-required items list + catalog columns.

Covers:

- Pick-a-category empty state when no ``node_id`` is provided and at least
  one taxonomy node has active field defs (the graceful-degradation
  threshold).
- Items table renders when ``node_id`` is set, including a column header per
  effective catalog field on the leaf (own + inherited).
- Per-item cells reflect the catalog values (column-backed or field-value-
  backed).
- CSV export gains one extra column per catalog field, keyed ``cf_<key>``.
- Category dropdown only includes nodes whose own or any ancestor has
  active field defs.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    FieldType,
    Item,
    ItemFieldValue,
    Role,
    TaxonomyFieldDef,
    TaxonomyNode,
    TrackingMode,
    User,
    UserStatus,
)


def _make_user(
    db: Session,
    *,
    email: str = "m@x.test",
    role: Role | None = Role.MANAGER,
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


def _make_node(db: Session, *, name: str = "Rings") -> TaxonomyNode:
    node = TaxonomyNode(name=name)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _add_field_def(
    db: Session,
    node: TaxonomyNode,
    *,
    catalog_key: str,
    key: str,
    name: str,
    field_type: FieldType = FieldType.SELECT,
    options: list[str] | None = None,
    sort_order: int = 10,
) -> TaxonomyFieldDef:
    fd = TaxonomyFieldDef(
        node_id=node.id,
        name=name,
        key=key,
        catalog_key=catalog_key,
        type=field_type,
        options_json=options,
        required=False,
        sort_order=sort_order,
    )
    db.add(fd)
    db.commit()
    db.refresh(fd)
    return fd


def _add_item(
    db: Session,
    node: TaxonomyNode,
    *,
    sku: str,
    name: str = "Item",
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=TrackingMode.QTY,
        reorder_threshold=Decimal("0"),
        reorder_qty=Decimal("0"),
        current_qty=Decimal("0"),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class TestPickCategoryEmptyState:
    def test_empty_state_when_no_node_id_and_a_category_has_fields(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        _add_field_def(
            db_session,
            node,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        _add_item(db_session, node, sku="RIN-0001")
        _login_as(client, _make_user(db_session))

        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert 'data-testid="items-pick-category"' in resp.text
        assert "RIN-0001" not in resp.text

    def test_items_table_renders_when_node_id_given(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        _add_field_def(
            db_session,
            node,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        _add_item(db_session, node, sku="RIN-0001")
        _login_as(client, _make_user(db_session))

        resp = client.get(f"/admin/items?node_id={node.id}")
        assert resp.status_code == 200
        assert "RIN-0001" in resp.text
        assert 'data-testid="items-pick-category"' not in resp.text

    def test_no_field_defs_falls_back_to_legacy_list(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Until any category has a picked field def, the empty state is not
        forced — the page falls back to the legacy 'all items' view so the
        UX isn't broken on day one."""

        node = _make_node(db_session)
        _add_item(db_session, node, sku="RIN-0001")
        _login_as(client, _make_user(db_session))

        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert 'data-testid="items-pick-category"' not in resp.text
        assert "RIN-0001" in resp.text


class TestCatalogColumnsHtml:
    def test_catalog_column_header_appears(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        _add_field_def(
            db_session,
            node,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        _add_item(db_session, node, sku="RIN-0001")
        _login_as(client, _make_user(db_session))

        resp = client.get(f"/admin/items?node_id={node.id}")
        assert resp.status_code == 200
        assert 'data-testid="catalog-col-karat"' in resp.text
        assert "Karat" in resp.text

    def test_field_value_cell_renders(self, client: TestClient, db_session: Session) -> None:
        node = _make_node(db_session)
        fd = _add_field_def(
            db_session,
            node,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        item = _add_item(db_session, node, sku="RIN-0001")
        ifv = ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_text="18ct")
        db_session.add(ifv)
        db_session.commit()
        _login_as(client, _make_user(db_session))

        resp = client.get(f"/admin/items?node_id={node.id}")
        assert resp.status_code == 200
        assert "18ct" in resp.text

    def test_inherited_field_def_surfaces_on_descendant(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Rings")
        child = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(child)
        db_session.commit()
        db_session.refresh(child)
        _add_field_def(
            db_session,
            parent,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        _add_item(db_session, child, sku="SIL-0001")
        _login_as(client, _make_user(db_session))

        resp = client.get(f"/admin/items?node_id={child.id}")
        assert resp.status_code == 200
        # Inherited from parent — appears as a column on the child's list.
        assert 'data-testid="catalog-col-karat"' in resp.text


class TestCatalogColumnsCsv:
    def test_csv_export_includes_catalog_column(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_node(db_session)
        fd = _add_field_def(
            db_session,
            node,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        item = _add_item(db_session, node, sku="RIN-0001")
        ifv = ItemFieldValue(item_id=item.id, field_def_id=fd.id, value_text="18ct")
        db_session.add(ifv)
        db_session.commit()
        _login_as(client, _make_user(db_session))

        resp = client.get(f"/admin/items?node_id={node.id}&format=csv")
        assert resp.status_code == 200
        body = resp.text
        # Header includes the cf_<key> column.
        first_line = body.splitlines()[0]
        assert "cf_karat" in first_line
        # The value appears in the body.
        assert "18ct" in body

    def test_csv_export_omits_catalog_columns_when_no_node_id(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Without a node_id the CSV is the legacy fixed-column export.

        Note that the HTML path *would* render the empty state in this
        setup; the CSV path remains a snapshot of whatever items match,
        which preserves the existing 'all items' download for users who
        rely on it before adopting the per-category flow.
        """

        node = _make_node(db_session)
        _add_field_def(
            db_session,
            node,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct", "18ct"],
        )
        _add_item(db_session, node, sku="RIN-0001")
        _login_as(client, _make_user(db_session))

        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        first_line = resp.text.splitlines()[0]
        assert "cf_karat" not in first_line


class TestCategoryDropdownFilter:
    def test_only_nodes_with_effective_fields_appear(
        self, client: TestClient, db_session: Session
    ) -> None:
        rings = _make_node(db_session, name="Rings")
        consumables = _make_node(db_session, name="Consumables")
        # Only Rings has a field def.
        _add_field_def(
            db_session,
            rings,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct"],
        )
        _login_as(client, _make_user(db_session))

        resp = client.get("/admin/items")
        body = resp.text
        # Rings appears in the category dropdown; Consumables does not.
        assert 'value="' + str(rings.id) + '"' in body
        assert 'value="' + str(consumables.id) + '"' not in body

    def test_descendant_of_field_owner_appears(
        self, client: TestClient, db_session: Session
    ) -> None:
        parent = _make_node(db_session, name="Rings")
        child = TaxonomyNode(name="Silver", parent_id=parent.id)
        db_session.add(child)
        db_session.commit()
        db_session.refresh(child)
        _add_field_def(
            db_session,
            parent,
            catalog_key="karat",
            key="karat",
            name="Karat",
            options=["9ct"],
        )
        _login_as(client, _make_user(db_session))

        resp = client.get("/admin/items")
        body = resp.text
        # Both parent and child appear — the child inherits the parent's field.
        assert 'value="' + str(parent.id) + '"' in body
        assert 'value="' + str(child.id) + '"' in body
