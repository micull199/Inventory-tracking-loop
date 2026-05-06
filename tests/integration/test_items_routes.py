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


def _audit_rows(
    db: Session, *, action: str | None = None
) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "item")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


def _make_leaf(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    """A top-level taxonomy node with no children — i.e. a leaf."""
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


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

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/admin/items")
        assert resp.status_code == 403

    def test_office_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        """I1a is Manager-owned; Office access lands in I1b (read+edit, restricted fields)."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/items")
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/items")
        assert resp.status_code == 200

    def test_workshop_create_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
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
        resp = client.get("/admin/items")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


class TestItemList:
    def test_list_shows_active_by_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
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

    def test_list_show_archived_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
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

    def test_list_orders_by_sku(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
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

    def test_list_filters_by_node_id(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_list_renders_new_item_cta(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert "/admin/items/new" in resp.text

    def test_list_shows_category_label(
        self, client: TestClient, db_session: Session
    ) -> None:
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
    def test_get_new_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_leaf(db_session, "Raw Materials")
        _login_as(client, mgr)
        resp = client.get("/admin/items/new")
        assert resp.status_code == 200
        assert 'name="sku"' in resp.text
        assert 'name="taxonomy_node_id"' in resp.text
        assert 'name="csrf_token"' in resp.text
        assert "Raw Materials" in resp.text

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
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
        assert resp.headers["location"] == "/admin/items"

        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku == "RM-001"
        assert item.name == "Silver wire"
        assert item.taxonomy_node_id == leaf.id
        assert item.unit == "g"
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

    def test_create_with_optional_fields(
        self, client: TestClient, db_session: Session
    ) -> None:
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
        assert item.tracking_mode is TrackingMode.UNIQUE
        assert item.requires_checkout is True
        assert item.supplier_id == supplier.id
        assert item.location_id == location.id
        assert item.qr_code == "qr-123"
        assert item.notes == "Some notes"

    def test_create_strips_whitespace(
        self, client: TestClient, db_session: Session
    ) -> None:
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
        assert item.sku == "X-1"
        assert item.name == "Wire"
        assert item.unit == "g"
        assert item.qr_code == "qr-x"

    def test_create_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.before_json is None
        assert row.after_json is not None
        assert row.after_json["sku"] == "RM-001"
        assert row.after_json["taxonomy_node_id"] == leaf.id
        assert row.after_json["tracking_mode"] == "qty"

    def test_create_empty_sku_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, sku="   ", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Item)).first() is None
        assert _audit_rows(db_session) == []

    def test_create_empty_name_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, name="", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_empty_unit_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, unit="", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_duplicate_sku_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        db_session.add(
            Item(
                sku="RM-001",
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
            data=_create_payload(
                taxonomy_node_id=leaf.id, sku="RM-001", csrf=_csrf(client)
            ),
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
                sku="RM-001",
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
            data=_create_payload(
                taxonomy_node_id=leaf.id, sku="RM-001", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_missing_node_id_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # Pass empty string for taxonomy_node_id.
        payload = _create_payload(taxonomy_node_id=0, csrf=_csrf(client))
        payload["taxonomy_node_id"] = ""
        resp = client.post(
            "/admin/items", data=payload, follow_redirects=False
        )
        assert resp.status_code == 400

    def test_create_unknown_node_id_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(taxonomy_node_id=9999, csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_archived_node_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        leaf.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_non_leaf_node_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A top-level node with an active sub-cat is NOT a leaf."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        top, _child = _make_top_with_child(db_session)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=top.id, csrf=_csrf(client)
            ),
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
            data=_create_payload(
                taxonomy_node_id=child.id, csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.taxonomy_node_id == child.id

    def test_create_invalid_tracking_mode_400(
        self, client: TestClient, db_session: Session
    ) -> None:
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
        assert resp.status_code == 400

    def test_create_negative_threshold_400(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_create_archived_supplier_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        supplier = Supplier(
            name="Old Co", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
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

    def test_create_archived_location_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        loc = Location(
            name="Old Bench", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
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

    def test_create_unknown_supplier_400(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_create_dup_qr_code_400(
        self, client: TestClient, db_session: Session
    ) -> None:
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
            data=_create_payload(
                taxonomy_node_id=leaf.id, sku="A", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp1.status_code == 303
        resp2 = client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, sku="B", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp2.status_code == 303
        rows = list(db_session.execute(select(Item)).scalars().all())
        assert len(rows) == 2
        assert all(r.qr_code is None for r in rows)

    def test_create_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _login_as(client, mgr)
        client.post(
            "/admin/items",
            data=_create_payload(
                taxonomy_node_id=leaf.id, sku="", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


class TestItemEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_unknown_id_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/9999/edit")
        assert resp.status_code == 404

    def test_edit_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_records_sparse_diff_only(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_no_op_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_can_move_to_another_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_rejects_move_to_non_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_duplicate_sku_400(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_edit_keeps_current_qty(
        self, client: TestClient, db_session: Session
    ) -> None:
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

    def test_archive_unknown_id_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items/9999/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_unarchive_unknown_id_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items/9999/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404
