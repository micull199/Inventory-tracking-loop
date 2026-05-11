"""Integration tests for the unique-tracked item-units routes (I3).

Mirrors the items / suppliers / locations test shape, plus I3-specifics:
- Item must be unique-tracked to add units (qty-tracked items 400 on
  create/list-form).
- ``serial_or_label`` is unique within an item, across active + archived rows.
- Different items can share a serial.
- Archived items cannot have new units (existing ones still editable).
- Archived locations preserved on edit but cannot be assigned fresh.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Item,
    ItemUnit,
    ItemUnitStatus,
    Location,
    Role,
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
        .where(AuditLog.entity_type == "item_unit")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


def _make_leaf(
    db: Session, name: str = "Tools", sku_prefix: str | None = None
) -> TaxonomyNode:
    # Default ``sku_prefix`` is derived from ``name`` (see ``TaxonomyNode``).
    # Callers that build sibling leaves from similar names (e.g. ``Cat-X``,
    # ``Cat-Y``) must pass an explicit prefix to dodge the partial unique
    # index on ``taxonomy_nodes(sku_prefix)``.
    kwargs: dict[str, object] = {"name": name}
    if sku_prefix is not None:
        kwargs["sku_prefix"] = sku_prefix
    node = TaxonomyNode(**kwargs)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_unique_item(
    db: Session,
    *,
    sku: str = "T-001",
    name: str = "Mould",
    archived: bool = False,
) -> Item:
    _alnum = "".join(c for c in sku if c.isalnum())[:8] or "TST"
    leaf = _make_leaf(db, name=f"Cat-{sku}", sku_prefix=_alnum)
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_qty_item(
    db: Session, *, sku: str = "RM-001", name: str = "Wire"
) -> Item:
    _alnum = "".join(c for c in sku if c.isalnum())[:8] or "TST"
    leaf = _make_leaf(db, name=f"Cat-{sku}", sku_prefix=_alnum)
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_unit(
    db: Session,
    *,
    item: Item,
    serial: str = "SN-001",
    status: ItemUnitStatus = ItemUnitStatus.AVAILABLE,
    archived: bool = False,
    location_id: int | None = None,
) -> ItemUnit:
    unit = ItemUnit(
        item_id=item.id,
        serial_or_label=serial,
        status=status,
        location_id=location_id,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(unit)
    db.commit()
    db.refresh(unit)
    return unit


def _create_payload(
    *,
    serial_or_label: str = "SN-001",
    status: str = "available",
    location_id: str = "",
    csrf: str = "",
) -> dict[str, str]:
    return {
        "serial_or_label": serial_or_label,
        "status": status,
        "location_id": location_id,
        "csrf_token": csrf,
    }


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestUnitsRoleEnforcement:
    def test_anonymous_get_list_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_unique_item(db_session)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert resp.status_code == 401

    def test_workshop_get_list_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item = _make_unique_item(db_session)
        _login_as(client, worker)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert resp.status_code == 403

    def test_office_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert resp.status_code == 200

    def test_office_get_new_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/units/new")
        assert resp.status_code == 403

    def test_office_create_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert db_session.execute(select(ItemUnit)).first() is None

    def test_office_edit_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item)
        _login_as(client, office)
        resp = client.get(f"/admin/items/units/{unit.id}/edit")
        assert resp.status_code == 200

    def test_office_update_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, serial="OLD")
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(serial_or_label="NEW", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.serial_or_label == "NEW"

    def test_office_archive_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/units/{unit.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_office_unarchive_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, archived=True)
        _login_as(client, office)
        resp = client.post(
            f"/admin/items/units/{unit.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_manager_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert resp.status_code == 200

    def test_admin_get_list_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        item = _make_unique_item(db_session)
        _login_as(client, admin)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert resp.status_code == 200

    def test_admin_create_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        """DoD #2: Admin creates units. ``require_role(MANAGER)`` lets Admin through."""
        admin = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        item = _make_unique_item(db_session)
        _login_as(client, admin)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert db_session.execute(select(ItemUnit)).scalar_one() is not None


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------


class TestUnitsList:
    def test_list_unknown_item_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/9999/units")
        assert resp.status_code == 404

    def test_list_qty_tracked_renders_with_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Qty-tracked items still render the page; the note explains why no units."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_qty_item(db_session)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert resp.status_code == 200
        assert "qty-tracked" in resp.text.lower()
        # CTA is hidden because tracking mode is qty.
        assert f"/admin/items/{item.id}/units/new" not in resp.text

    def test_list_active_default(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _make_unit(db_session, item=item, serial="ACTIVE")
        _make_unit(db_session, item=item, serial="ARCHIVED", archived=True)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert "ACTIVE" in resp.text
        assert "ARCHIVED" not in resp.text

    def test_list_show_archived_filter(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _make_unit(db_session, item=item, serial="ACTIVE")
        _make_unit(db_session, item=item, serial="ARCHIVED", archived=True)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units?show=archived")
        assert "ARCHIVED" in resp.text
        assert "ACTIVE" not in resp.text

    def test_list_orders_by_serial(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        for s in ("ZULU", "ALPHA", "BRAVO"):
            _make_unit(db_session, item=item, serial=s)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        body = resp.text
        assert 0 < body.find("ALPHA") < body.find("BRAVO") < body.find("ZULU")

    def test_list_renders_new_cta_for_manager(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert f"/admin/items/{item.id}/units/new" in resp.text

    def test_list_hides_new_cta_for_office(
        self, client: TestClient, db_session: Session
    ) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_unique_item(db_session)
        _login_as(client, office)
        resp = client.get(f"/admin/items/{item.id}/units")
        # The "New unit" link for Office is hidden.
        assert 'data-testid="new-item-unit"' not in resp.text

    def test_list_hides_new_cta_when_item_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session, archived=True)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert 'data-testid="new-item-unit"' not in resp.text
        assert "archived" in resp.text.lower()

    def test_list_renders_location_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        loc = Location(name="Workshop bench")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        _make_unit(db_session, item=item, location_id=loc.id)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units")
        assert "Workshop bench" in resp.text


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestUnitCreate:
    def test_get_new_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units/new")
        assert resp.status_code == 200
        assert 'name="serial_or_label"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_get_new_form_unknown_item_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/9999/units/new")
        assert resp.status_code == 404

    def test_get_new_form_qty_tracked_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_qty_item(db_session)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units/new")
        assert resp.status_code == 400

    def test_get_new_form_archived_item_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session, archived=True)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/units/new")
        assert resp.status_code == 400

    def test_create_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="SN-001", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/units"
        unit = db_session.execute(select(ItemUnit)).scalar_one()
        assert unit.serial_or_label == "SN-001"
        assert unit.status is ItemUnitStatus.AVAILABLE
        assert unit.location_id is None
        assert unit.archived_at is None

    def test_create_with_lost_status_and_location(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        loc = Location(name="Bench")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="SN-LOST",
                status="lost",
                location_id=str(loc.id),
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        unit = db_session.execute(select(ItemUnit)).scalar_one()
        assert unit.status is ItemUnitStatus.LOST
        assert unit.location_id == loc.id

    def test_create_strips_whitespace(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="  SN-A  ", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        unit = db_session.execute(select(ItemUnit)).scalar_one()
        assert unit.serial_or_label == "SN-A"

    def test_create_writes_audit_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item_unit.created")
        assert len(rows) == 1
        row = rows[0]
        assert row.actor_id == mgr.id
        assert row.before_json is None
        assert row.after_json is not None
        assert row.after_json["item_id"] == item.id
        assert row.after_json["serial_or_label"] == "SN-001"
        assert row.after_json["status"] == "available"
        assert row.after_json["location_id"] is None

    def test_create_blank_serial_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="   ", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(ItemUnit)).first() is None

    def test_create_dup_serial_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _make_unit(db_session, item=item, serial="SN-001")
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="SN-001", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_dup_serial_spans_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archived sibling with same serial should still 400 — archive doesn't free the label."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _make_unit(db_session, item=item, serial="SN-001", archived=True)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="SN-001", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_two_items_same_serial_ok(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Different items can have units with the same label."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item_a = _make_unique_item(db_session, sku="A", name="A")
        item_b = _make_unique_item(db_session, sku="B", name="B")
        _make_unit(db_session, item=item_a, serial="SHARED")
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item_b.id}/units",
            data=_create_payload(
                serial_or_label="SHARED", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303

    def test_create_qty_tracked_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_qty_item(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(ItemUnit)).first() is None

    def test_create_archived_item_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session, archived=True)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(ItemUnit)).first() is None

    def test_create_unknown_item_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items/9999/units",
            data=_create_payload(csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_create_bad_status_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                status="checked-out", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(ItemUnit)).first() is None

    def test_create_unknown_location_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                location_id="9999", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_archived_location_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        loc = Location(
            name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                location_id=str(loc.id), csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_create_failure_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/{item.id}/units",
            data=_create_payload(
                serial_or_label="", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert _audit_rows(db_session) == []


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


class TestUnitEdit:
    def test_get_edit_form_renders(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, serial="SN-EDIT")
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/units/{unit.id}/edit")
        assert resp.status_code == 200
        assert "SN-EDIT" in resp.text

    def test_edit_unknown_id_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items/units/9999/edit")
        assert resp.status_code == 404

    def test_update_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, serial="OLD")
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(
                serial_or_label="NEW",
                status="lost",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/units"
        db_session.refresh(unit)
        assert unit.serial_or_label == "NEW"
        assert unit.status is ItemUnitStatus.LOST

    def test_update_sparse_audit_diff(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, serial="OLD")
        _login_as(client, mgr)
        client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(
                serial_or_label="OLD",
                status="lost",
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        rows = _audit_rows(db_session, action="item_unit.updated")
        assert len(rows) == 1
        before = rows[0].before_json or {}
        after = rows[0].after_json or {}
        assert "serial_or_label" not in before
        assert before.get("status") == "available"
        assert after.get("status") == "lost"

    def test_update_no_op_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, serial="SAME")
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(
                serial_or_label="SAME", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _audit_rows(db_session, action="item_unit.updated") == []

    def test_update_dup_serial_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        _make_unit(db_session, item=item, serial="A")
        unit_b = _make_unit(db_session, item=item, serial="B")
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit_b.id}",
            data=_create_payload(serial_or_label="A", csrf=_csrf(client)),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_update_archived_location_preserved(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Editing without changing an archived FK keeps the link (I1b contract)."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        loc = Location(name="Old")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        unit = _make_unit(
            db_session, item=item, serial="SN", location_id=loc.id
        )
        # Archive the location after assignment.
        loc.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(
                serial_or_label="SN-NEW",
                location_id=str(loc.id),
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.location_id == loc.id
        assert unit.serial_or_label == "SN-NEW"

    def test_update_switch_to_archived_location_400(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item)
        archived_loc = Location(
            name="Archived", archived_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        db_session.add(archived_loc)
        db_session.commit()
        db_session.refresh(archived_loc)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(
                location_id=str(archived_loc.id),
                csrf=_csrf(client),
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_update_clears_optional_location(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        loc = Location(name="Bench")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        unit = _make_unit(db_session, item=item, location_id=loc.id)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}",
            data=_create_payload(
                location_id="", csrf=_csrf(client)
            ),
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.location_id is None

    def test_update_form_lists_archived_location_with_suffix(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        loc = Location(name="Old")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        unit = _make_unit(db_session, item=item, location_id=loc.id)
        loc.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/units/{unit.id}/edit")
        assert resp.status_code == 200
        assert "Old (archived)" in resp.text


# ---------------------------------------------------------------------------
# Archive / unarchive
# ---------------------------------------------------------------------------


class TestUnitArchive:
    def test_archive_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.archived_at is not None
        rows = _audit_rows(db_session, action="item_unit.archived")
        assert len(rows) == 1

    def test_archive_idempotent(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, archived=True)
        original = unit.archived_at
        _login_as(client, mgr)
        client.post(
            f"/admin/items/units/{unit.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        db_session.refresh(unit)
        assert unit.archived_at == original
        assert _audit_rows(db_session, action="item_unit.archived") == []

    def test_unarchive_happy_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item, archived=True)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(unit)
        assert unit.archived_at is None
        rows = _audit_rows(db_session, action="item_unit.unarchived")
        assert len(rows) == 1

    def test_unarchive_idempotent(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session)
        unit = _make_unit(db_session, item=item)
        _login_as(client, mgr)
        client.post(
            f"/admin/items/units/{unit.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        db_session.refresh(unit)
        assert unit.archived_at is None
        assert _audit_rows(db_session, action="item_unit.unarchived") == []

    def test_archive_unknown_id_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items/units/9999/archive",
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
            "/admin/items/units/9999/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_archive_unit_under_archived_item_still_works(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Archived items still allow unit cleanup (cleanup, not new structure)."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_unique_item(db_session, archived=True)
        unit = _make_unit(db_session, item=item)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/units/{unit.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
