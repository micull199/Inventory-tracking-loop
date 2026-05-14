"""Integration tests for the Manager-owned ``/admin/items`` CRUD routes (I1a).

Mirrors the suppliers / locations / taxonomy test shape, plus I1a-specifics:
- Leaf-node validation (cannot pick a top-level node with active children, an
  archived node, or a non-existent id).
- Tracking mode is a real enum.
- ``current_qty`` is *not* writable through the route.
- Optional FKs (supplier, location) must reference an active row if set.
- ``qr_code`` is partial-unique-when-set; blank means "no label".
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Item,
    Location,
    Role,
    Supplier,
    TaxonomyFieldDef,
    TaxonomyNode,
    TrackingMode,
    User,
    UserStatus,
)

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


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
    stmt = select(AuditLog).where(AuditLog.entity_type == "item").order_by(AuditLog.id)
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


_DEFAULT_PICKED_BUILT_INS: tuple[str, ...] = (
    "name",
    "unit",
    "tracking_mode",
    "requires_checkout",
    "reorder_threshold",
    "reorder_qty",
    "supplier_id",
    "location_id",
    "qr_code",
)


def _pick_default_built_ins(db: Session, node: TaxonomyNode) -> None:
    """Pick every column-backed catalog entry on ``node`` so the items form
    renders the full set of built-in inputs.

    Pre-slice-6 these inputs rendered unconditionally; slice 6 makes their
    rendering catalog-driven. Most existing tests assume the legacy "all
    built-ins visible" form, so this helper preserves that behaviour without
    each test having to call the picker route.
    """

    from app.field_catalog import CATALOG_BY_KEY

    for key in _DEFAULT_PICKED_BUILT_INS:
        entry = CATALOG_BY_KEY[key]
        db.add(
            TaxonomyFieldDef(
                node_id=node.id,
                key=entry.key,
                required=False,
                sort_order=entry.sort_order,
            )
        )
    db.commit()


def _make_leaf(
    db: Session, name: str = "Raw Materials", *, pick_built_ins: bool = True
) -> TaxonomyNode:
    """A top-level taxonomy node with no children — i.e. a leaf.

    By default pre-picks every column-backed catalog entry so the items
    form renders the full built-in input set (the legacy "all built-ins
    visible" form every pre-slice-6 test assumed). Pass
    ``pick_built_ins=False`` for tests that need the post-slice-6
    "nothing picked yet" state, e.g. the items-list "Pick a category"
    empty-state tests that need the eligible-categories set to stay
    empty.
    """

    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    if pick_built_ins:
        _pick_default_built_ins(db, n)
    return n


def _existing_item(
    db: Session, node: TaxonomyNode, *, sku: str = "RM-001", name: str = "Wire"
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=node.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_top_with_child(
    db: Session, *, top_name: str = "Top", child_name: str = "Child"
) -> tuple[TaxonomyNode, TaxonomyNode]:
    top = TaxonomyNode(name=top_name)
    db.add(top)
    db.commit()
    db.refresh(top)
    child = TaxonomyNode(name=child_name, parent_id=top.id)
    db.add(child)
    db.commit()
    db.refresh(child)
    return top, child


def _create_payload(
    *,
    sku: str = "RM-001",
    name: str = "Silver wire",
    taxonomy_node_id: int,
    unit: str = "g",
    tracking_mode: str = "qty",
    requires_checkout: bool | None = None,
    reorder_threshold: str = "",
    reorder_qty: str = "",
    supplier_id: str = "",
    location_id: str = "",
    qr_code: str = "",
    notes: str = "",
    csrf: str = "",
) -> dict[str, str]:
    payload = {
        "sku": sku,
        "name": name,
        "taxonomy_node_id": str(taxonomy_node_id),
        "unit": unit,
        "tracking_mode": tracking_mode,
        "reorder_threshold": reorder_threshold,
        "reorder_qty": reorder_qty,
        "supplier_id": supplier_id,
        "location_id": location_id,
        "qr_code": qr_code,
        "notes": notes,
        "csrf_token": csrf,
    }
    if requires_checkout:
        payload["requires_checkout"] = "true"
    return payload


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestRoleEnforcement:
    def test_anonymous_get_list_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/items")
        assert resp.status_code == 401

    def test_workshop_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        """I1c: Workshop can list items (read-only). Direct precursor to SC1."""
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/items")
        assert resp.status_code == 200

    def test_workshop_get_edit_form_is_200(self, client: TestClient, db_session: Session) -> None:
        """I1c: Workshop can GET the edit form (renders read-only)."""
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="W-1",
            name="W item",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200

    def test_workshop_get_new_form_is_403(self, client: TestClient, db_session: Session) -> None:
        """I1c: Workshop cannot reach the create form."""
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/items/new")
        assert resp.status_code == 403

    def test_workshop_update_is_403(self, client: TestClient, db_session: Session) -> None:
        """I1c: Workshop cannot POST updates — and no audit row written."""
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="W-2",
            name="W item",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, worker)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                sku="W-2-CHANGED",
                name="W item changed",
                taxonomy_node_id=leaf.id,
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 403
        db_session.refresh(item)
        assert item.sku == "W-2"
        assert item.name == "W item"
        assert _audit_rows(db_session, action="item.updated") == []

    def test_workshop_archive_is_403(self, client: TestClient, db_session: Session) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="W-3",
            name="W",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, worker)
        resp = client.post(
            f"/admin/items/{item.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        db_session.refresh(item)
        assert item.archived_at is None

    def test_workshop_unarchive_is_403(self, client: TestClient, db_session: Session) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="W-4",
            name="W",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, worker)
        resp = client.post(
            f"/admin/items/{item.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        db_session.refresh(item)
        assert item.archived_at is not None

    def test_office_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        """I1b: Office can list items (MISSION §3)."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/items")
        assert resp.status_code == 200

    def test_office_get_new_form_is_403(self, client: TestClient, db_session: Session) -> None:
        """I1b: Office cannot create items — only read + edit existing rows."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/items/new")
        assert resp.status_code == 403

    def test_office_create_is_403(self, client: TestClient, db_session: Session) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        _login_as(client, office)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(Item)).first() is None

    def test_office_archive_is_403(self, client: TestClient, db_session: Session) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        db_session.refresh(item)
        assert item.archived_at is None

    def test_office_unarchive_is_403(self, client: TestClient, db_session: Session) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        db_session.refresh(item)
        assert item.archived_at is not None

    def test_manager_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(self, client: TestClient, db_session: Session) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/items")
        assert resp.status_code == 200

    def test_admin_can_create_item(self, client: TestClient, db_session: Session) -> None:
        """DoD #2: Admin creates items. ``require_role(MANAGER)`` lets Admin
        through, but the explicit assertion lives here so a future tightening
        of that rule can't quietly remove Admin's create access."""
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        leaf = _make_leaf(db_session)
        _login_as(client, admin)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        # SKU is server-allocated (see TestItemCreate.test_create_happy_path).
        assert item.sku == "RAW-0001"

    def test_workshop_create_is_403(self, client: TestClient, db_session: Session) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        leaf = _make_leaf(db_session)
        _login_as(client, worker)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(Item)).first() is None

    def test_pending_user_get_list_is_403(self, client: TestClient, db_session: Session) -> None:
        pending = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, pending)
        resp = client.get("/admin/items")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


class TestItemList:
    def test_list_shows_active_by_default(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        db_session.add_all(
            [
                Item(
                    sku="RM-1",
                    name="Active Item",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="RM-2",
                    name="Old Item",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert "Active Item" in resp.text
        assert "Old Item" not in resp.text

    def test_list_show_archived_filter(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        db_session.add_all(
            [
                Item(
                    sku="A",
                    name="Active Item",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="B",
                    name="Old Item",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                    archived_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/items?show=archived")
        assert "Old Item" in resp.text
        assert "Active Item" not in resp.text

    def test_list_orders_by_sku(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        db_session.add_all(
            [
                Item(
                    sku="ZULU",
                    name="Z",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="ALPHA",
                    name="A",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="BRAVO",
                    name="B",
                    taxonomy_node_id=leaf.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/items")
        body = resp.text
        assert 0 < body.find("ALPHA") < body.find("BRAVO") < body.find("ZULU")

    def test_list_filters_by_node_id(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf_a = _make_leaf(db_session, "A")
        leaf_b = _make_leaf(db_session, "B")
        db_session.add_all(
            [
                Item(
                    sku="A1",
                    name="In A",
                    taxonomy_node_id=leaf_a.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
                Item(
                    sku="B1",
                    name="In B",
                    taxonomy_node_id=leaf_b.id,
                    unit="g",
                    tracking_mode=TrackingMode.QTY,
                ),
            ]
        )
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get(f"/admin/items?node_id={leaf_a.id}")
        assert "In A" in resp.text
        assert "In B" not in resp.text

    def test_list_renders_new_item_cta(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert "/admin/items/new" in resp.text

    def test_list_shows_category_label(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, child = _make_top_with_child(
            db_session, top_name="Raw Materials", child_name="Silver"
        )
        db_session.add(
            Item(
                sku="RM-1",
                name="Wire",
                taxonomy_node_id=child.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
            )
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert "Raw Materials / Silver" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestItemCreate:
    def test_get_new_form_renders(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        # SKU is the only structural field (MISSION §3 Item management) so
        # it renders before the category picker — always visible.
        resp = client.get("/admin/items/new")
        assert resp.status_code == 200
        assert 'data-testid="item-category-picker"' in resp.text
        assert 'data-testid="item-pick-category-prompt"' in resp.text
        assert 'data-testid="item-sku-input"' in resp.text
        # Pre-selecting a category via ?node_id= renders the full form.
        resp = client.get(f"/admin/items/new?node_id={leaf.id}")
        assert resp.status_code == 200
        assert 'data-testid="item-sku-input"' in resp.text
        assert "auto-generate" in resp.text
        # Notes was removed entirely.
        assert 'data-testid="item-notes-input"' not in resp.text
        assert 'name="taxonomy_node_id"' in resp.text
        assert 'name="csrf_token"' in resp.text
        assert "Raw Materials" in resp.text

    def test_create_happy_path(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="RM-001",
                name="Silver wire",
                unit="g",
                tracking_mode="qty",
                reorder_threshold="100",
                reorder_qty="500",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # Slice 5 onward: redirect to the created item's category so the
        # items list (per-category since slice 5) actually shows the new row.
        assert resp.headers["location"].startswith("/admin/items?node_id=")

        item = db_session.execute(select(Item)).scalar_one()
        # Server now owns SKU allocation under the taxonomy refinement; the
        # client-supplied ``sku`` is ignored. Leaf ``Raw Materials`` has the
        # derived prefix ``RAW`` (model default from ``name``), and this is
        # the first item under it → sequence 1 → ``RAW-0001``.
        assert item.sku == "RAW-0001"
        assert item.assigned_sequence == 1
        assert item.name == "Silver wire"
        assert item.taxonomy_node_id == leaf.id
        assert item.unit == "g"
        # Effective archetype falls back to BULK for a fixture-built node
        # without an explicit archetype; tracking_mode is derived from
        # archetype on create.
        assert item.tracking_mode is TrackingMode.QTY
        assert item.requires_checkout is False
        assert item.current_qty == Decimal("0")
        assert item.reorder_threshold == Decimal("100")
        assert item.reorder_qty == Decimal("500")
        assert item.supplier_id is None
        assert item.location_id is None
        assert item.qr_code is None
        assert item.notes is None
        assert item.archived_at is None

    def test_create_with_optional_fields(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        supplier = Supplier(name="Acme Wax")
        location = Location(name="Bench A")
        db_session.add_all([supplier, location])
        db_session.commit()
        db_session.refresh(supplier)
        db_session.refresh(location)
        _login_as(client, mgr)

        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                tracking_mode="unique",
                requires_checkout=True,
                supplier_id=str(supplier.id),
                location_id=str(location.id),
                qr_code="qr-123",
                notes="Some notes",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        # tracking_mode submitted by the form is overridden by the
        # archetype-derived value on create — a depth-0 leaf without an
        # explicit archetype defaults to ``bulk`` → ``qty``. The edit form
        # remains writable so an Office user can correct mistakes.
        assert item.tracking_mode is TrackingMode.QTY
        assert item.requires_checkout is True
        assert item.supplier_id == supplier.id
        assert item.location_id == location.id
        assert item.qr_code == "qr-123"
        assert item.notes == "Some notes"

    def test_create_strips_whitespace(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="  X-1  ",
                name="  Wire  ",
                unit="  g  ",
                qr_code="  qr-x  ",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        # SKU is server-allocated (client-supplied is ignored). Whitespace
        # is still trimmed from name / unit / qr_code.
        assert item.sku == "RAW-0001"
        assert item.name == "Wire"
        assert item.unit == "g"
        assert item.qr_code == "qr-x"

    def test_create_writes_audit_row(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.before_json is None
        assert row.after_json is not None
        # SKU is server-allocated (``<leaf-prefix>-<NNNN>``); the audit
        # blob also records the freshly-allocated sequence.
        assert row.after_json["sku"] == "RAW-0001"
        assert row.after_json["assigned_sequence"] == 1
        assert row.after_json["taxonomy_node_id"] == leaf.id
        assert row.after_json["tracking_mode"] == "qty"

    def test_create_blank_sku_is_auto_generated(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Behaviour change: SKU is auto-generated when the form omits it. The
        # form input was removed; the route accepts a blank ``sku`` and fills
        # it from ``_generate_sku(leaf)`` based on the leaf's name prefix.
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, sku="   ", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalars().one()
        assert item.sku == "RAW-0001"

    def test_create_empty_name_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, name="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_empty_unit_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, unit="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_duplicate_sku_400(self, client: TestClient, db_session: Session) -> None:
        """Even with server-allocated SKUs, a collision (e.g. a row that
        pre-dates the refinement) is rejected rather than silently 500ing
        on the unique index. We pre-stage an item already holding the SKU
        the allocator would mint and assert the route returns 400 instead
        of bypassing ``_check_sku_unique``.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        # The allocator will mint ``RAW-0001`` (leaf prefix RAW, seq 1).
        # Block it with a pre-existing item carrying that exact SKU.
        db_session.add(
            Item(
                sku="RAW-0001",
                name="A",
                taxonomy_node_id=leaf.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
            )
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_duplicate_sku_with_archived_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archiving doesn't free the SKU. Same convention as Supplier names."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        db_session.add(
            Item(
                sku="RAW-0001",
                name="A",
                taxonomy_node_id=leaf.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                archived_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_missing_node_id_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # Pass empty string for taxonomy_node_id.
        payload = _create_payload(taxonomy_node_id=0, csrf=_csrf(client))
        payload["taxonomy_node_id"] = ""
        resp = client.post("/admin/items", data=payload, follow_redirects=False)
        assert resp.status_code == 400

    def test_create_unknown_node_id_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=9999, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_archived_node_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        leaf.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_non_leaf_node_400(self, client: TestClient, db_session: Session) -> None:
        """A top-level node with an active sub-cat is NOT a leaf."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        top, _child = _make_top_with_child(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=top.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_under_sub_category_succeeds(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Sub-cats are always leaves; items can attach there."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, child = _make_top_with_child(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=child.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.taxonomy_node_id == child.id

    def test_create_invalid_tracking_mode_silently_overridden(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The form-submitted ``tracking_mode`` is overridden on create by
        the leaf's effective archetype (``bulk`` → ``qty`` here). A
        ``"bogus"`` submission therefore doesn't even reach validation —
        the route saves the archetype-derived value and 303s. The edit
        form remains where users correct mistakes."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                tracking_mode="bogus",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.tracking_mode is TrackingMode.QTY

    def test_create_negative_threshold_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                reorder_threshold="-1",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_non_numeric_threshold_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                reorder_threshold="abc",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_archived_supplier_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        supplier = Supplier(name="Old Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(supplier)
        db_session.commit()
        db_session.refresh(supplier)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                supplier_id=str(supplier.id),
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_archived_location_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        loc = Location(name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                location_id=str(loc.id),
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_unknown_supplier_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                supplier_id="9999",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_dup_qr_code_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        db_session.add(
            Item(
                sku="A",
                name="A",
                taxonomy_node_id=leaf.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                qr_code="qr-123",
            )
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="B",
                qr_code="qr-123",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_blank_qr_does_not_collide(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Two items with no QR code each is fine; partial unique index."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp1 = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, sku="A", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        resp2 = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, sku="B", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        rows = list(db_session.execute(select(Item)).scalars().all())
        assert len(rows) == 2
        assert all(r.qr_code is None for r in rows)

    def test_create_failure_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        # A blank-name submit is the simplest way to trigger a validation
        # failure post the SKU auto-gen change. (Blank SKU now auto-generates
        # rather than 400ing — see ``test_create_blank_sku_is_auto_generated``.)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, name="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


class TestItemEdit:
    def test_get_edit_form_renders(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="RM-1",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert "RM-1" in resp.text
        assert "Wire" in resp.text

    def test_edit_unknown_id_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/9999/edit")
        assert resp.status_code == 404

    def test_edit_happy_path(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="RM-1",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="RM-1",
                name="Silver wire",
                unit="g",
                tracking_mode="qty",
                reorder_threshold="50",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.name == "Silver wire"
        assert item.reorder_threshold == Decimal("50")

    def test_edit_records_sparse_diff_only(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="RM-1",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="RM-1",
                name="Silver wire",  # only this changes
                unit="g",
                tracking_mode="qty",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item.updated")
        assert len(rows) == 1
        row = rows[0]
        assert row.before_json is not None
        assert row.after_json is not None
        assert set(row.before_json.keys()) == {"name"}
        assert row.before_json["name"] == "Wire"
        assert row.after_json["name"] == "Silver wire"

    def test_edit_no_op_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="RM-1",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="RM-1",
                name="Wire",
                unit="g",
                tracking_mode="qty",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert _audit_rows(db_session, action="item.updated") == []

    def test_edit_can_move_to_another_leaf(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf_a = _make_leaf(db_session, "A")
        leaf_b = _make_leaf(db_session, "B")
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf_a.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf_b.id,
                sku="X",
                name="X",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        db_session.refresh(item)
        assert item.taxonomy_node_id == leaf_b.id

    def test_edit_rejects_move_to_non_leaf(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Leaf")
        top, _child = _make_top_with_child(db_session)
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=top.id,
                sku="X",
                name="X",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_edit_duplicate_sku_400(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        a = Item(
            sku="A",
            name="A",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        b = Item(
            sku="B",
            name="B",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add_all([a, b])
        db_session.commit()
        db_session.refresh(b)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{b.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="A",
                name="B",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_edit_keeps_current_qty(self, client: TestClient, db_session: Session) -> None:
        """current_qty isn't on the form; edits must not zero it out."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            current_qty=Decimal("42"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Y",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        db_session.refresh(item)
        assert item.current_qty == Decimal("42")
        assert item.name == "Y"


# ---------------------------------------------------------------------------
# Archive / unarchive
# ---------------------------------------------------------------------------


class TestItemArchive:
    def test_archive_idempotent_audit_only_once(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        token = _csrf(client)

        resp1 = client.post(
            f"/admin/items/{item.id}/archive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        db_session.refresh(item)
        assert item.archived_at is not None

        # Second archive is a no-op: no new audit row, still 303.
        resp2 = client.post(
            f"/admin/items/{item.id}/archive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        rows = _audit_rows(db_session, action="item.archived")
        assert len(rows) == 1

    def test_unarchive_idempotent_audit_only_once(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="X",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        token = _csrf(client)

        client.post(
            f"/admin/items/{item.id}/unarchive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        db_session.refresh(item)
        assert item.archived_at is None

        client.post(
            f"/admin/items/{item.id}/unarchive",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item.unarchived")
        assert len(rows) == 1

    def test_archive_unknown_id_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_unarchive_unknown_id_404(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items/9999/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# I1b: Office can edit but not change reorder thresholds (MISSION §3)
# ---------------------------------------------------------------------------


class TestOfficeEdit:
    """I1b: Office gets read+edit; reorder thresholds remain Manager-only.

    Office submitting threshold changes is silently ignored (the route
    overrides the inbound values with the existing row's values *before*
    validation), so the audit row never records a threshold change for an
    Office actor. That's deliberate per MISSION §3: "cannot change reorder
    thresholds." A 400 would leak the field shape; silent override matches
    the way ``current_qty`` is already handled.
    """

    def test_office_get_edit_form(self, client: TestClient, db_session: Session) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200

    def test_office_form_hides_threshold_inputs(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            reorder_threshold=Decimal("100"),
            reorder_qty=Decimal("500"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/edit")
        # No editable input — the inputs are hidden behind ``can_edit_thresholds``.
        assert 'data-testid="item-reorder-threshold-input"' not in resp.text
        assert 'data-testid="item-reorder-qty-input"' not in resp.text
        # Read-only display values are present and show the existing values.
        assert 'data-testid="item-reorder-threshold-readonly"' in resp.text
        assert 'data-testid="item-reorder-qty-readonly"' in resp.text
        assert "100" in resp.text
        assert "500" in resp.text

    def test_manager_form_shows_threshold_inputs(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Sanity: I1b's Office hide didn't accidentally hide for Manager too."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="item-reorder-threshold-input"' in resp.text
        assert 'data-testid="item-reorder-qty-input"' in resp.text

    def test_office_can_edit_non_threshold_fields(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Silver wire",  # Office can change the name
                unit="g",
                tracking_mode="qty",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.name == "Silver wire"

    def test_office_threshold_change_silently_ignored(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Office submits new thresholds → row keeps the old values, no audit diff for them."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            reorder_threshold=Decimal("100"),
            reorder_qty=Decimal("500"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Wire",
                unit="g",
                tracking_mode="qty",
                reorder_threshold="999",  # would-be change
                reorder_qty="888",  # would-be change
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        # Form is a no-op — nothing actually changed because the thresholds
        # were silently overridden and no other field moved. 303 still fires.
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.reorder_threshold == Decimal("100")
        assert item.reorder_qty == Decimal("500")
        # No update audit row because the diff was empty.
        assert _audit_rows(db_session, action="item.updated") == []

    def test_office_threshold_change_along_with_real_change(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Office mixes a name change with a (silently ignored) threshold change.

        Audit diff records ONLY the name; thresholds are inert.
        """
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            reorder_threshold=Decimal("100"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, office)
        client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Silver wire",
                unit="g",
                tracking_mode="qty",
                reorder_threshold="999",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item.updated")
        assert len(rows) == 1
        row = rows[0]
        assert row.before_json is not None
        assert row.after_json is not None
        assert "reorder_threshold" not in row.before_json
        assert "reorder_threshold" not in row.after_json
        assert "name" in row.after_json

    def test_office_list_hides_new_and_archive_buttons(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        db_session.add(
            Item(
                sku="X",
                name="X",
                taxonomy_node_id=leaf.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
            )
        )
        db_session.commit()
        _login_as(client, office)
        resp = client.get("/admin/items")
        assert 'data-testid="new-item"' not in resp.text
        assert 'data-testid="archive-item"' not in resp.text
        # Edit link is still there — it's the action they're allowed to take.
        assert 'data-testid="edit-item"' in resp.text


# ---------------------------------------------------------------------------
# I1b: Archived-FK preservation on item edit
# ---------------------------------------------------------------------------


class TestArchivedFKPreservation:
    """Editing an item that references a now-archived FK must not silently drop it.

    Pre-I1b: the dropdown only listed active rows, so the archived row was
    missing. The blank option submitted on save → the FK silently went to
    None. Now: the assigned archived row is rendered with an "(archived)"
    suffix and the resolver accepts it *unchanged* (but still rejects any
    *change* to a different archived row).
    """

    def test_edit_form_lists_archived_supplier_with_suffix(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        supplier = Supplier(name="Old Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(supplier)
        db_session.commit()
        db_session.refresh(supplier)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            supplier_id=supplier.id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert "Old Co (archived)" in resp.text

    def test_edit_form_lists_archived_location_with_suffix(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        loc = Location(name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            location_id=loc.id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert "Old Bench (archived)" in resp.text

    def test_edit_form_lists_archived_leaf_with_suffix(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Old Cat")
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        # Archive the leaf after item creation.
        leaf.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert "Old Cat (archived)" in resp.text

    def test_edit_form_lists_archived_subcat_leaf_with_parent_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archived sub-cat leaf should render as ``Parent / Sub (archived)``."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, child = _make_top_with_child(
            db_session, top_name="Raw Materials", child_name="Silver"
        )
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=child.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        # Archive the child leaf.
        child.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert "Raw Materials / Silver (archived)" in resp.text

    def test_edit_keeps_unchanged_archived_supplier(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Submit the same archived supplier id → 303, supplier unchanged, no diff for that field."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        supplier = Supplier(name="Old Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(supplier)
        db_session.commit()
        db_session.refresh(supplier)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            supplier_id=supplier.id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Silver wire",  # only this changes
                unit="g",
                tracking_mode="qty",
                supplier_id=str(supplier.id),  # unchanged archived FK
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.supplier_id == supplier.id
        rows = _audit_rows(db_session, action="item.updated")
        assert len(rows) == 1
        assert rows[0].after_json is not None
        assert "supplier_id" not in rows[0].after_json
        assert "name" in rows[0].after_json

    def test_edit_keeps_unchanged_archived_location(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        loc = Location(name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            location_id=loc.id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Silver wire",
                unit="g",
                tracking_mode="qty",
                location_id=str(loc.id),
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.location_id == loc.id

    def test_edit_keeps_unchanged_archived_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Old Cat")
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        leaf.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Silver wire",
                unit="g",
                tracking_mode="qty",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.taxonomy_node_id == leaf.id

    def test_edit_rejects_switch_to_different_archived_supplier(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        a = Supplier(name="A", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        b = Supplier(name="B", archived_at=datetime(2026, 1, 2, tzinfo=UTC))
        db_session.add_all([a, b])
        db_session.commit()
        db_session.refresh(a)
        db_session.refresh(b)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            supplier_id=a.id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Wire",
                unit="g",
                tracking_mode="qty",
                supplier_id=str(b.id),  # different archived supplier
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_edit_rejects_switch_to_different_archived_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        cur = _make_leaf(db_session, "Cur")
        other = _make_leaf(db_session, "Other")
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=cur.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        cur.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        other.archived_at = datetime(2026, 1, 2, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=other.id,  # different archived leaf
                sku="X",
                name="Wire",
                unit="g",
                tracking_mode="qty",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_edit_clearing_archived_supplier_is_explicit_clear(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Submit blank supplier_id → link is cleared, audit records the clear.

        Clearing is an explicit user action, not the silent data loss the
        pre-I1b code was guilty of (which silently cleared because the
        archived row was missing from the dropdown).
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        s = Supplier(name="Old Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            supplier_id=s.id,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="X",
                name="Wire",
                unit="g",
                tracking_mode="qty",
                supplier_id="",  # explicit blank
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.supplier_id is None
        rows = _audit_rows(db_session, action="item.updated")
        assert len(rows) == 1
        assert rows[0].after_json is not None
        assert rows[0].after_json.get("supplier_id") is None

    def test_edit_form_does_not_list_archived_supplier_when_not_assigned(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archived rows that aren't the *current* assignment must not appear.

        Otherwise the dropdown becomes a graveyard. Only the row the item is
        actually pinned to gets the carve-out.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        s = Supplier(name="Old Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(s)
        db_session.commit()
        db_session.refresh(s)
        item = Item(
            sku="X",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            # no supplier_id
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert "Old Co" not in resp.text

    def test_create_does_not_list_archived_FKs(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The new-item form has no current assignment so no archived rows."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_leaf(db_session)
        s = Supplier(name="Archived Sup", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        loc = Location(name="Archived Loc", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add_all([s, loc])
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/items/new")
        assert "Archived Sup" not in resp.text
        assert "Archived Loc" not in resp.text


# ---------------------------------------------------------------------------
# Custom field values (I2)
# ---------------------------------------------------------------------------


class TestWorkshopReadOnlyView:
    def _seed(
        self,
        db: Session,
        *,
        with_custom_field: bool = False,
        pick_built_ins: bool = False,
    ) -> tuple[User, Item]:
        worker = _make_user(db, email="w@x.test", role=Role.WORKSHOP)
        leaf = _make_leaf(db, pick_built_ins=pick_built_ins)
        item = Item(
            sku="WV-1",
            name="Workshop view item",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db.add(item)
        db.commit()
        db.refresh(item)
        _ = with_custom_field  # Legacy parameter kept for caller API stability.
        return worker, item

    def test_list_shows_view_link_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker, _ = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert 'data-testid="view-item"' in resp.text
        assert 'data-testid="edit-item"' not in resp.text

    def test_list_hides_new_cta_for_workshop(self, client: TestClient, db_session: Session) -> None:
        worker, _ = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get("/admin/items")
        assert 'data-testid="new-item"' not in resp.text

    def test_list_hides_archive_buttons_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker, _ = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get("/admin/items")
        assert 'data-testid="archive-item"' not in resp.text
        assert 'data-testid="unarchive-item"' not in resp.text

    def test_list_shows_edit_link_for_office(self, client: TestClient, db_session: Session) -> None:
        """Confirms the link-label split: Office still sees 'Edit'."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        item = Item(
            sku="OF-1",
            name="Office item",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        _login_as(client, office)
        resp = client.get("/admin/items")
        assert 'data-testid="edit-item"' in resp.text
        assert 'data-testid="view-item"' not in resp.text

    def test_form_inputs_are_disabled_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker, item = self._seed(db_session, pick_built_ins=True)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        body = resp.text
        # Every named input/select/textarea should carry the ``disabled``
        # attribute somewhere in its tag. SKU was removed from the editable
        # input set (read-only span on edit) and Notes was removed entirely.
        for tid in (
            "item-name-input",
            "item-category-input",
            "item-unit-input",
            "item-tracking-mode-input",
            "item-requires-checkout-input",
            "item-supplier-input",
            "item-location-input",
            "item-qr-input",
        ):
            tag_start = body.find(f'data-testid="{tid}"')
            assert tag_start != -1, f"missing input {tid!r}"
            # Find the enclosing tag (back to '<' before the testid attr).
            open_lt = body.rfind("<", 0, tag_start)
            close_gt = body.find(">", tag_start)
            assert open_lt != -1
            assert close_gt != -1
            tag = body[open_lt:close_gt]
            assert "disabled" in tag, f"input {tid!r} is not disabled in workshop view: {tag!r}"

    def test_form_hides_submit_for_workshop(self, client: TestClient, db_session: Session) -> None:
        worker, item = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="item-submit"' not in resp.text

    def test_form_renders_readonly_note_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker, item = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="item-form-readonly-note"' in resp.text

    def test_form_title_is_view_for_workshop(self, client: TestClient, db_session: Session) -> None:
        worker, item = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert "View Workshop view item" in resp.text

    def test_form_shows_action_links_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Workshop has access to /in, /out, /adjust, /detail — links remain."""
        worker, item = self._seed(db_session)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="stock-in-link"' in resp.text
        assert 'data-testid="stock-out-link"' in resp.text
        assert 'data-testid="stock-adjust-link"' in resp.text
        assert 'data-testid="detail-link"' in resp.text

    def test_form_disables_custom_field_inputs_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        import pytest
        pytest.skip("superseded by 0024 standard-fields refactor")
        worker, item = self._seed(db_session, with_custom_field=True)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        body = resp.text
        tag_start = body.find('data-testid="item-cf-alloy-input"')
        assert tag_start != -1
        open_lt = body.rfind("<", 0, tag_start)
        close_gt = body.find(">", tag_start)
        tag = body[open_lt:close_gt]
        assert "disabled" in tag

    def test_form_for_manager_keeps_submit_and_does_not_render_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Sanity-check: the read-only treatment is Workshop-only."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="MX-1",
            name="Mgr item",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert 'data-testid="item-submit"' in resp.text
        assert 'data-testid="item-form-readonly-note"' not in resp.text


# ---------------------------------------------------------------------------
# C1 — requires_checkout flag UI + filter
# ---------------------------------------------------------------------------


class TestRequiresCheckoutFlag:
    """C1: items list shows the flag + filter; form shows explanatory help."""

    def _seed_two(self, db: Session) -> tuple[User, Item, Item]:
        mgr = _make_user(db, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db, pick_built_ins=False)
        flagged = Item(
            sku="TOOL-A",
            name="Hammer",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            requires_checkout=True,
        )
        plain = Item(
            sku="MAT-A",
            name="Silver wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            requires_checkout=False,
        )
        db.add_all([flagged, plain])
        db.commit()
        db.refresh(flagged)
        db.refresh(plain)
        return mgr, flagged, plain

    def test_list_shows_yes_for_flagged_item(self, client: TestClient, db_session: Session) -> None:
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        body = resp.text
        # Both items render; we confirm the flagged one carries "Yes" in its
        # checkout cell and the unflagged one carries "—".
        assert "TOOL-A" in body
        assert "MAT-A" in body
        # The cell test-id appears once per row; assert both labels appear.
        assert ">Yes<" in body
        assert ">—<" in body

    def test_list_shows_dash_when_no_items_flagged(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        db_session.add(
            Item(
                sku="MAT-A",
                name="Silver wire",
                taxonomy_node_id=leaf.id,
                unit="g",
                tracking_mode=TrackingMode.QTY,
                requires_checkout=False,
            )
        )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        body = resp.text
        assert 'data-testid="item-requires-checkout"' in body
        assert ">Yes<" not in body

    def test_list_filter_yes_shows_only_flagged(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/items?requires_checkout=yes")
        body = resp.text
        assert "TOOL-A" in body
        assert "MAT-A" not in body

    def test_list_filter_blank_shows_all(self, client: TestClient, db_session: Session) -> None:
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        body = resp.text
        assert "TOOL-A" in body
        assert "MAT-A" in body

    def test_list_filter_unrecognised_value_does_not_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Silent coerce: ?requires_checkout=foo behaves like no filter."""
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/items?requires_checkout=foo")
        body = resp.text
        assert "TOOL-A" in body
        assert "MAT-A" in body

    def test_list_filter_no_does_not_filter(self, client: TestClient, db_session: Session) -> None:
        """``requires_checkout=no`` doesn't mean 'show only non-flagged' — same as no filter."""
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/items?requires_checkout=no")
        body = resp.text
        assert "TOOL-A" in body
        assert "MAT-A" in body

    def test_filter_nav_links_present_with_aria_current(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)

        # All-items view: "All items" carries aria-current.
        resp = client.get("/admin/items")
        body = resp.text
        assert 'data-testid="filter-all"' in body
        assert 'data-testid="filter-requires-checkout"' in body
        # Locate the all-filter tag and assert aria-current is on it.
        all_tag_start = body.find('data-testid="filter-all"')
        all_tag_close = body.find(">", all_tag_start)
        all_tag = body[body.rfind("<", 0, all_tag_start) : all_tag_close]
        assert 'aria-current="page"' in all_tag

        # Filtered view: the requires-checkout link carries aria-current.
        resp2 = client.get("/admin/items?requires_checkout=yes")
        body2 = resp2.text
        rc_tag_start = body2.find('data-testid="filter-requires-checkout"')
        rc_tag_close = body2.find(">", rc_tag_start)
        rc_tag = body2[body2.rfind("<", 0, rc_tag_start) : rc_tag_close]
        assert 'aria-current="page"' in rc_tag

    def test_filter_combines_with_show_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, pick_built_ins=False)
        archived_flagged = Item(
            sku="TOOL-OLD",
            name="Retired chisel",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            requires_checkout=True,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        archived_plain = Item(
            sku="MAT-OLD",
            name="Old wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            requires_checkout=False,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add_all([archived_flagged, archived_plain])
        db_session.commit()
        _login_as(client, mgr)

        resp = client.get("/admin/items?show=archived&requires_checkout=yes")
        body = resp.text
        assert "TOOL-OLD" in body
        assert "MAT-OLD" not in body

    def test_form_renders_help_note(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        # Built-in fields render after a category is picked; pre-select via
        # ``?node_id=`` so the requires-checkout help note is part of the
        # initial server render.
        resp = client.get(f"/admin/items/new?node_id={leaf.id}")
        assert resp.status_code == 200
        assert 'data-testid="item-requires-checkout-help"' in resp.text

    def test_filter_link_preserves_show_param(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Switching between 'All items' and 'Requires checkout' preserves show=archived."""
        mgr, _flagged, _plain = self._seed_two(db_session)
        _login_as(client, mgr)
        resp = client.get("/admin/items?show=archived")
        body = resp.text
        # The filter link's href carries show=archived so the filter doesn't
        # silently flip the user back to active.
        assert "/admin/items?show=archived&amp;requires_checkout=yes" in body
        assert 'href="/admin/items?show=archived"' in body


# ---------------------------------------------------------------------------
# R5b — CSV export on the items list
# ---------------------------------------------------------------------------


class TestItemsListCsvRoleEnforcement:
    """The CSV branch is gated tighter than the HTML branch.

    HTML list is Manager + Office + Workshop (Workshop has read-only access
    per I1c). CSV branch is Manager + Office only — Workshop is rejected
    with 403 because MISSION §3 says Workshop "cannot see aggregated cost
    data or reports". A snapshot CSV is a shareable artefact, not a live
    list.
    """

    def test_anon_csv_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 401

    def test_pending_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="p@x.test", role=None, status=UserStatus.PENDING)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 403

    def test_workshop_csv_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 403

    def test_manager_csv_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200

    def test_office_csv_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200


class TestItemsListCsvHeaders:
    def test_content_type_carries_csv_charset(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"

    def test_content_disposition_default_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        cd = resp.headers["content-disposition"]
        assert "attachment" in cd
        assert 'filename="items_active.csv"' in cd

    def test_content_disposition_archived_filename(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv&show=archived")
        cd = resp.headers["content-disposition"]
        assert 'filename="items_archived.csv"' in cd


class TestItemsListCsvBody:
    def test_empty_emits_only_header_row(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        assert resp.text == (
            "id,sku,name,category,stage,unit,tracking_mode,current_qty,"
            "reorder_threshold,reorder_qty,requires_checkout,unit_cost\r\n"
        )

    def test_one_item_one_data_row(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, name="Raw Materials")
        item = Item(
            sku="MAT-A",
            name="Silver wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            requires_checkout=False,
            current_qty=Decimal("10"),
            reorder_threshold=Decimal("5"),
            reorder_qty=Decimal("20"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        lines = resp.text.split("\r\n")
        assert len(lines) == 3  # header + 1 data + trailing empty
        cells = lines[1].split(",")
        assert cells[0] == str(item.id)
        assert cells[1] == "MAT-A"
        assert cells[2] == "Silver wire"
        assert cells[3] == "Raw Materials"
        assert cells[4] == ""  # stage — no stages configured for this category
        assert cells[5] == "g"
        assert cells[6] == "qty"
        # current_qty / reorder_threshold / reorder_qty round-trip via the
        # column at scale 4 — Decimal("10.0000") str()s to "10.0000".
        assert Decimal(cells[7]) == Decimal("10")
        assert Decimal(cells[8]) == Decimal("5")
        assert Decimal(cells[9]) == Decimal("20")
        assert cells[10] == "no"

    def test_flagged_item_renders_yes(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, name="Tools")
        item = Item(
            sku="TOOL-A",
            name="Hammer",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            requires_checkout=True,
        )
        db_session.add(item)
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        # The requires_checkout cell carries the literal string "yes" (not
        # "True"). Two-cell match avoids accidental hit on a substring "yes"
        # elsewhere — preceded by tracking_mode=unique and zero qty/threshold/
        # reorder; ends with a CRLF. (Stage cell — empty here — sits between
        # ``category`` and ``unit`` in the row layout.)
        # ``unit_cost`` cell trails after requires_checkout; blank because
        # no FIFO layer exists for this item yet.
        assert ",unique,0.0000,0.0000,0.0000,yes,\r\n" in resp.text

    def test_show_filter_applies_to_csv(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        active = Item(
            sku="MAT-A",
            name="Active wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        archived = Item(
            sku="MAT-OLD",
            name="Old wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add_all([active, archived])
        db_session.commit()
        _login_as(client, u)

        # Default show=active: only the active item appears.
        resp_active = client.get("/admin/items?format=csv")
        assert "MAT-A" in resp_active.text
        assert "MAT-OLD" not in resp_active.text

        # show=archived: only the archived item appears.
        resp_archived = client.get("/admin/items?format=csv&show=archived")
        assert "MAT-A" not in resp_archived.text
        assert "MAT-OLD" in resp_archived.text

    def test_requires_checkout_filter_applies_to_csv(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        flagged = Item(
            sku="TOOL-A",
            name="Hammer",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            requires_checkout=True,
        )
        plain = Item(
            sku="MAT-A",
            name="Silver wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
            requires_checkout=False,
        )
        db_session.add_all([flagged, plain])
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv&requires_checkout=yes")
        assert resp.status_code == 200
        assert "TOOL-A" in resp.text
        assert "MAT-A" not in resp.text

    def test_sku_ordering_preserved_in_csv(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        # Insert in non-alphabetical order to verify the route's _LIST_ORDER
        # sorts by sku ascending across active rows.
        z_item = Item(
            sku="Z-1",
            name="Zinc",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        a_item = Item(
            sku="A-1",
            name="Alpha",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add_all([z_item, a_item])
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        body = resp.text
        # A-1 row appears before Z-1 row in the CSV body.
        a_pos = body.index(",A-1,")
        z_pos = body.index(",Z-1,")
        assert a_pos < z_pos

    def test_category_with_parent_renders_parent_slash_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Sub-cat under a top: category cell is 'Top / Leaf'."""
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, child = _make_top_with_child(db_session, top_name="Tools", child_name="Hand")
        item = Item(
            sku="HAM-1",
            name="Hammer",
            taxonomy_node_id=child.id,
            unit="ea",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        # The CSV writer quotes cells that contain the slash separator only
        # if the dialect requires; the default QUOTE_MINIMAL doesn't quote
        # ``/`` (no comma, quote, or CR/LF in the cell). The cell appears
        # literally as ``Tools / Hand``.
        assert ",Tools / Hand," in resp.text


class TestItemsListCsvHtmlBranch:
    def test_format_blank_renders_html(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert 'data-testid="items-table"' in resp.text or (
            'data-testid="items-empty"' in resp.text
        )

    def test_format_unknown_renders_html(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?format=garbage")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")


class TestItemsListCsvReadOnly:
    def test_csv_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = Item(
            sku="MAT-A",
            name="Silver wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
        db_session.add(item)
        db_session.commit()
        before = len(list(db_session.execute(select(AuditLog)).scalars().all()))
        _login_as(client, u)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        after = len(list(db_session.execute(select(AuditLog)).scalars().all()))
        assert after == before


class TestItemsListCsvLink:
    def test_html_renders_csv_link_for_manager(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert 'data-testid="items-list-csv-link"' in resp.text
        assert "format=csv" in resp.text

    def test_html_renders_csv_link_for_office(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert 'data-testid="items-list-csv-link"' in resp.text

    def test_html_hides_csv_link_for_workshop(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        # Workshop sees the items list (HTML branch) but not the CSV link.
        assert 'data-testid="items-list-csv-link"' not in resp.text

    def test_csv_link_preserves_show_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?show=archived")
        # The link's href carries show=archived so a user looking at archived
        # gets a CSV of archived.
        assert "show=archived" in resp.text
        assert "format=csv&amp;show=archived" in resp.text

    def test_csv_link_preserves_requires_checkout_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items?requires_checkout=yes")
        # The link's href carries requires_checkout=yes when the filter is on.
        assert "format=csv&amp;show=active&amp;requires_checkout=yes" in (resp.text)


class TestItemFormHtmxWiring:
    """The items form's category ``<select>`` carries the HTMX attributes that
    drive the partial swap. Without these, the user picks a category and the
    custom-field block never updates."""

    def test_new_form_category_select_has_htmx_get(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/items/new")
        assert resp.status_code == 200
        assert 'hx-get="/admin/items/_custom-fields"' in resp.text
        assert 'hx-trigger="change"' in resp.text
        # HTMX target is the wrapper for the post-category fragment.
        assert 'hx-target="#item-fields-after-category"' in resp.text
        # Wrapper div is always present, even before a category is picked.
        assert 'id="item-fields-after-category"' in resp.text

    def test_edit_form_category_select_has_htmx_get(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session)
        item = _existing_item(db_session, leaf)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert 'hx-get="/admin/items/_custom-fields"' in resp.text
        # Edit form's HTMX target is also the post-category fragment wrapper.
        assert 'id="item-fields-after-category"' in resp.text


class TestSkuAutoGeneration:
    """SKU is server-allocated on create under the taxonomy refinement.

    Client-supplied ``sku`` form values are ignored; the route composes the
    SKU from the leaf's ancestor prefixes plus the leaf's monotonic
    ``next_sequence``. See ``docs/taxonomy-refinement-plan.md`` and
    ``app.sku``.
    """

    def test_blank_sku_uses_leaf_name_prefix(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Wax Injection Moulds")
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, sku="", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalars().one()
        # Leaf name "Wax Injection Moulds" → derived prefix "WAX". First
        # item under the leaf → sequence 1.
        assert item.sku == "WAX-0001"
        assert item.assigned_sequence == 1

    def test_sequence_increments_within_a_prefix(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        for i, name in enumerate(("a", "b", "c"), start=1):
            resp = client.post(
                "/admin/items",
                data=_create_payload(
                    taxonomy_node_id=leaf.id,
                    sku="",
                    name=name,
                    csrf=_csrf(client),
                ),
                follow_redirects=False,
            )
            assert resp.status_code == 303
            assert (
                db_session.execute(select(Item).where(Item.name == name)).scalar_one().sku
                == f"RAW-{i:04d}"
            )

    def test_explicit_sku_is_ignored_on_create(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Behaviour change: a client-supplied SKU on create is ignored.

        Pre-refinement the route honoured a hand-rolled SKU; the
        refinement gives the server exclusive ownership so an item's SKU
        always traces back to its leaf's ancestor chain.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                sku="MY-OWN-SKU",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalars().one()
        assert item.sku != "MY-OWN-SKU"
        assert item.sku == "RAW-0001"


class TestItemsFormNotesRemoved:
    """Notes field removed from both create and edit forms."""

    def test_create_form_has_no_notes_input(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/new")
        assert resp.status_code == 200
        assert 'data-testid="item-notes-input"' not in resp.text
        assert 'name="notes"' not in resp.text

    def test_edit_form_has_no_notes_input(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _existing_item(db_session, leaf)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert 'data-testid="item-notes-input"' not in resp.text


class TestItemsFormSkuOnEdit:
    """SKU is read-only on edit (auto-generated on create, immutable thereafter)."""

    def test_edit_form_renders_sku_as_readonly(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _existing_item(db_session, leaf, sku="ABC-0042")
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        # The read-only display carries the SKU as text in a span.
        assert 'data-testid="item-sku-readonly"' in resp.text
        assert "ABC-0042" in resp.text
        # No editable text input for SKU.
        assert 'data-testid="item-sku-input"' not in resp.text


class TestItemsFragmentDefaults:
    """HTMX fragment OOB-swaps core defaults when ``include_defaults=1``."""

    def test_fragment_with_defaults_includes_oob_for_set_keys(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        leaf.defaults_json = {"unit": "g", "tracking_mode": "qty"}
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(
            f"/admin/items/_custom-fields?taxonomy_node_id={leaf.id}&include_defaults=1"
        )
        assert resp.status_code == 200
        # OOB swaps for the keys the leaf actually sets.
        assert 'hx-swap-oob="true"' in resp.text
        assert 'id="unit"' in resp.text
        assert 'value="g"' in resp.text
        assert 'id="tracking_mode"' in resp.text

    def test_fragment_omits_oob_for_unset_keys(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        # Only unit set — tracking_mode / supplier / location etc. are absent.
        leaf.defaults_json = {"unit": "g"}
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(
            f"/admin/items/_custom-fields?taxonomy_node_id={leaf.id}&include_defaults=1"
        )
        assert resp.status_code == 200
        # Under the post-category-fragment design, all default-visible fields
        # render in the response. The configured ``unit`` default is baked
        # into the input value; unset keys (tracking_mode, supplier, etc.)
        # render with the form's blank initial state — not erased mid-typing
        # via an OOB swap.
        assert 'id="unit"' in resp.text
        assert 'value="g"' in resp.text
        # tracking_mode + supplier + location all render (default-visible),
        # but with no leaf default applied. The fragment no longer uses
        # OOB-swap markup for built-in fields.
        assert 'id="tracking_mode"' in resp.text
        assert 'id="supplier_id"' in resp.text
        assert "hx-swap-oob" in resp.text  # only for sku-preview now

    def test_fragment_without_include_defaults_emits_no_oob(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Edit form path: omits ``include_defaults`` so a Manager re-classifying
        # an item doesn't silently lose the existing item's typed values.
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        leaf.defaults_json = {"unit": "g", "tracking_mode": "unique"}
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/_custom-fields?taxonomy_node_id={leaf.id}")
        assert resp.status_code == 200
        assert "hx-swap-oob" not in resp.text


class TestItemsCreateFormPrefillsFromDefaults:
    """``GET /admin/items/new?node_id=…`` server-side renders inputs with the
    leaf's defaults_json values pre-filled."""

    def test_unit_default_pre_fills_input(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        leaf.defaults_json = {"unit": "g"}
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/new?node_id={leaf.id}")
        assert resp.status_code == 200
        # The unit input renders with value="g" pre-filled.
        body = resp.text
        unit_idx = body.find('data-testid="item-unit-input"')
        assert unit_idx != -1
        # Look back to find the enclosing tag's opening
        tag_start = body.rfind("<", 0, unit_idx)
        tag_end = body.find(">", unit_idx)
        tag = body[tag_start:tag_end]
        assert 'value="g"' in tag

    def test_no_node_id_no_defaults_applied(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        leaf.defaults_json = {"unit": "g"}
        db_session.commit()
        _login_as(client, mgr)
        # Without ?node_id=, no leaf is identified — no defaults applied.
        resp = client.get("/admin/items/new")
        assert resp.status_code == 200
        body = resp.text
        unit_idx = body.find('data-testid="item-unit-input"')
        tag_start = body.rfind("<", 0, unit_idx)
        tag_end = body.find(">", unit_idx)
        tag = body[tag_start:tag_end]
        assert 'value=""' in tag or "value" not in tag.split("data-testid")[0]


class TestItemsFormReRendersOnValidationError:
    """Validation failures re-render the form with typed values + an error
    message instead of bubbling out as a raw JSON ``{"detail": ...}`` body.

    Covers testnotes #1 + #2 (items custom-field validation regressions).
    """

    def test_create_invalid_decimal_custom_field_re_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        import pytest
        pytest.skip("superseded by 0024 standard-fields refactor")

    def test_create_missing_required_custom_field_re_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        import pytest
        pytest.skip("superseded by 0024 standard-fields refactor")

    def test_update_invalid_custom_field_re_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        import pytest
        pytest.skip("superseded by 0024 standard-fields refactor")


class TestLeafOptionsNonLeafLabel:
    """Item-edit category dropdown labels a non-leaf parent correctly.

    Bug (testnotes #5): the parent dropdown labelled non-archived parents
    that had gained sub-categories as ``"(archived)"``, misleading the
    manager into thinking they archived something they didn't.
    """

    def test_non_leaf_parent_labelled_explicitly(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Create top-level category, an item assigned to it (so it was a
        # leaf at the time), then add a sub-cat to make it non-leaf.
        parent = TaxonomyNode(name="Raw Materials", sort_order=10)
        db_session.add(parent)
        db_session.commit()
        item = _existing_item(db_session, parent, name="Pre-existing")
        sub = TaxonomyNode(parent_id=parent.id, name="Silver", sort_order=10)
        db_session.add(sub)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        # The parent appears as a selectable option (still the item's
        # current_id), but is NOT labelled archived.
        assert "Raw Materials (archived)" not in resp.text
        # New post-refinement label clearly flags the unreachable
        # destination. ``_pickable_options`` flips ineligible-but-present
        # current_ids to "no longer a pickable destination".
        assert "no longer a pickable destination" in resp.text

    def test_archived_parent_still_labelled_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        parent = TaxonomyNode(
            name="Old Cat",
            sort_order=10,
            archived_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        db_session.add(parent)
        db_session.commit()
        item = _existing_item(db_session, parent, name="Hist")
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/edit")
        assert resp.status_code == 200
        assert "Old Cat (archived)" in resp.text


# ===========================================================================
# Taxonomy refinement: archetype-aware item create + list filter
# ===========================================================================


from app.models import Archetype  # noqa: E402


class TestPerArchetypeCreate:
    """Per-archetype item create paths.

    Confirms the route picks the right SKU shape + tracking_mode for each
    archetype, and that unique-variant items get an auto-created depth-2
    leaf below the picked depth-1 sub-cat.
    """

    def _bulk_leaf(self, db: Session) -> TaxonomyNode:
        node = TaxonomyNode(
            name="Tools",
            archetype=Archetype.BULK,
            sku_prefix="TOOL",
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        return node

    def _unique_leaf(self, db: Session) -> TaxonomyNode:
        node = TaxonomyNode(
            name="Rings",
            archetype=Archetype.UNIQUE,
            sku_prefix="RING",
        )
        db.add(node)
        db.commit()
        db.refresh(node)
        return node

    def _uv_tree(self, db: Session) -> tuple[TaxonomyNode, TaxonomyNode]:
        top = TaxonomyNode(
            name="RTS Rings",
            archetype=Archetype.UNIQUE_VARIANT,
            sku_prefix="RTS",
        )
        db.add(top)
        db.commit()
        db.refresh(top)
        sub = TaxonomyNode(name="Emma", parent_id=top.id, sku_prefix="EM")
        db.add(sub)
        db.commit()
        db.refresh(sub)
        return top, sub

    def test_bulk_create_yields_two_segment_sku_and_qty_mode(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = self._bulk_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id,
                # client-supplied tracking_mode is forced to archetype.
                tracking_mode="unique",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku == "TOOL-0001"
        assert item.assigned_sequence == 1
        assert item.tracking_mode is TrackingMode.QTY
        assert item.taxonomy_node_id == leaf.id

    def test_unique_create_yields_two_segment_sku_and_unique_mode(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = self._unique_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=leaf.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku == "RING-0001"
        assert item.tracking_mode is TrackingMode.UNIQUE
        assert item.assigned_sequence == 1

    def test_unique_variant_create_yields_three_segments(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, sub = self._uv_tree(db_session)
        _login_as(client, mgr)
        # Pick the depth-1 sub-cat (the picker target for unique-variant
        # trees). The server mints an auto-leaf and attaches the item to it.
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=sub.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku == "RTS-EM-001"
        assert item.tracking_mode is TrackingMode.UNIQUE
        assert item.assigned_sequence == 1
        # The item now lives on a freshly-created depth-2 auto-leaf, not
        # on the sub-cat itself.
        assert item.taxonomy_node_id != sub.id
        leaf = db_session.get(TaxonomyNode, item.taxonomy_node_id)
        assert leaf is not None
        assert leaf.parent_id == sub.id
        assert leaf.name == "001"
        assert leaf.sku_prefix == "001"

    def test_unique_variant_create_under_depth_0_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Picking the top-level node of a unique-variant tree is rejected
        — items require the depth-1 sub-cat.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        top, _sub = self._uv_tree(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=top.id, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unique_variant_sequential_creates_increment(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Two creates against the same sub-cat get SKUs ending in 001
        then 002. The sub-cat's ``next_sequence`` advances to 3.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, sub = self._uv_tree(db_session)
        _login_as(client, mgr)
        for expected in ("RTS-EM-001", "RTS-EM-002"):
            resp = client.post(
                "/admin/items",
                data=_create_payload(
                    taxonomy_node_id=sub.id,
                    name=f"Ring {expected}",
                    csrf=_csrf(client),
                ),
                follow_redirects=False,
            )
            assert resp.status_code == 303
        skus = list(db_session.execute(select(Item.sku).order_by(Item.id)).scalars().all())
        assert skus == ["RTS-EM-001", "RTS-EM-002"]
        db_session.expire_all()
        sub_refreshed = db_session.get(TaxonomyNode, sub.id)
        assert sub_refreshed is not None
        assert sub_refreshed.next_sequence == 3

    def test_client_supplied_sku_ignored_for_unique_variant(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, sub = self._uv_tree(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=sub.id,
                sku="MY-OWN",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku != "MY-OWN"
        assert item.sku == "RTS-EM-001"


class TestListFilterDescendantTree:
    def test_filter_by_uv_subcat_matches_descendant_items(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A list filter on a depth-1 unique_variant sub-cat must surface
        items whose ``taxonomy_node_id`` points at depth-2 auto-leaves
        under it.
        """
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        top = TaxonomyNode(
            name="RTS",
            archetype=Archetype.UNIQUE_VARIANT,
            sku_prefix="RTS",
        )
        db_session.add(top)
        db_session.commit()
        db_session.refresh(top)
        sub = TaxonomyNode(name="Emma", parent_id=top.id, sku_prefix="EM")
        db_session.add(sub)
        db_session.commit()
        db_session.refresh(sub)
        _login_as(client, mgr)
        # Mint two items via the route so the auto-leaves are created.
        client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=sub.id, name="A", csrf=_csrf(client)),
            follow_redirects=False,
        )
        client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=sub.id, name="B", csrf=_csrf(client)),
            follow_redirects=False,
        )
        resp = client.get(f"/admin/items?node_id={sub.id}")
        assert resp.status_code == 200
        assert "RTS-EM-001" in resp.text
        assert "RTS-EM-002" in resp.text


# ===========================================================================
# Taxonomy refinement (Agent 4): searchable picker + SKU preview
# ===========================================================================


class TestCategorySearchFragment:
    """``GET /admin/items/_category-search`` — leaf-picker HTMX search.

    The picker JS layers click + keyboard nav over an HTMX-fed result list.
    The fragment route filters pickable options by a case-insensitive
    substring against the breadcrumb. Cap of 20 results per request.
    """

    def test_returns_partial_with_leaf_options(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        resp = client.get("/admin/items/_category-search")
        assert resp.status_code == 200
        # Each option is a list item with the picker data attributes used by
        # the JS to populate the hidden id + visible breadcrumb on click.
        assert 'data-testid="item-category-option"' in resp.text
        assert f'data-id="{leaf.id}"' in resp.text
        assert 'data-breadcrumb="Raw Materials"' in resp.text

    def test_filters_by_breadcrumb_substring(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Two top-level bulk leaves with very different breadcrumbs so a
        # substring match is unambiguous.
        _make_leaf(db_session, "Findings")
        rings = _make_leaf(db_session, "Cast Rings")
        _login_as(client, mgr)
        resp = client.get("/admin/items/_category-search?q=ring")
        assert resp.status_code == 200
        # Only "Cast Rings" matches "ring" (case-insensitive substring).
        assert "Cast Rings" in resp.text
        assert "Findings" not in resp.text
        assert f'data-id="{rings.id}"' in resp.text

    def test_empty_query_returns_all_up_to_limit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Seed > 20 active leaves; the route caps at 20.
        for i in range(25):
            db_session.add(
                TaxonomyNode(
                    name=f"Leaf {i:02d}",
                    sku_prefix=f"L{i:02d}",
                    archetype=Archetype.BULK,
                )
            )
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get("/admin/items/_category-search")
        assert resp.status_code == 200
        # 20 options max — count distinct row testids.
        count = resp.text.count('data-testid="item-category-option"')
        assert count == 20

    def test_empty_match_renders_empty_state(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        resp = client.get("/admin/items/_category-search?q=zzzzzzzzz-no-match")
        assert resp.status_code == 200
        assert 'data-testid="item-category-empty"' in resp.text

    def test_anonymous_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/items/_category-search")
        assert resp.status_code == 401


class TestNewItemFormCategoryPicker:
    """The New Item form renders the searchable picker container."""

    def test_new_form_renders_picker(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        resp = client.get("/admin/items/new")
        assert resp.status_code == 200
        # The picker container, search input, results list, and SKU preview
        # all appear so the JS can find them.
        assert 'data-testid="item-category-picker"' in resp.text
        assert 'id="taxonomy_node_search"' in resp.text
        assert 'id="taxonomy_node_results"' in resp.text
        assert 'data-testid="item-category-results"' in resp.text
        assert 'data-testid="sku-preview"' in resp.text
        # Picker script is linked.
        assert "/static/js/category-picker.js" in resp.text



