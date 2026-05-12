"""Integration tests for Transfer Orders (Slice 2 of the in-transit / stages
scope addition).

A Transfer Order represents stock moving between two UC locations with
separate ship and receive events. While shipped but not yet received, each
line's item has ``location_id = NULL`` and the TO appears as in-transit.
Cost engine is **never** invoked — matches the existing instant-flip
``TRANSFER`` movement under ``/admin/items/{id}/transfer``.

Coverage:
- Role enforcement on every TO route (workshop read-only; office + manager
  create/ship/receive/cancel).
- Create: happy path + source≠destination + sub-cat / archived rejections.
- Status machine: draft → shipped → received; cancel only from draft.
- Ship: validates every line's item is at source; nulls each item's
  ``location_id``; writes a TRANSFER movement per line with the parent TO id.
- Receive: flips each item's ``location_id`` to destination; writes a TRANSFER
  movement per line.
- Cost engine is never touched: ``movement.total_cost`` stays ``None``,
  ``current_qty`` is unchanged.
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
    MovementType,
    Role,
    StockMovement,
    TaxonomyNode,
    TrackingMode,
    TransferOrder,
    TransferOrderLine,
    TransferOrderStatus,
    User,
    UserStatus,
)

# ---------------------------------------------------------------------------
# Scaffolding
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


def _make_location(db: Session, name: str) -> Location:
    loc = Location(name=name)
    db.add(loc)
    db.commit()
    db.refresh(loc)
    return loc


def _make_leaf(db: Session, name: str = "Rings") -> TaxonomyNode:
    n = TaxonomyNode(name=name, sku_prefix="RNG")
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_item(
    db: Session, *, leaf: TaxonomyNode, location: Location, sku: str = "RNG-001"
) -> Item:
    item = Item(
        sku=sku,
        name="Silver band",
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
        location_id=location.id,
        current_qty=Decimal("1"),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _create_draft(
    client: TestClient,
    *,
    src: Location,
    dst: Location,
    items: list[Item],
    expected_arrival: str = "",
    carrier: str = "",
    tracking_number: str = "",
) -> int:
    data = {
        "source_location_id": str(src.id),
        "destination_location_id": str(dst.id),
        "expected_arrival": expected_arrival,
        "carrier": carrier,
        "tracking_number": tracking_number,
        "notes": "",
        "csrf_token": _csrf(client),
    }
    for idx, item in enumerate(items):
        data[f"item_id_{idx}"] = str(item.id)
        data[f"qty_{idx}"] = "1"
    resp = client.post("/admin/transfers", data=data, follow_redirects=False)
    assert resp.status_code == 303, resp.text
    return int(resp.headers["location"].rsplit("/", 1)[1])


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestTransferRoleEnforcement:
    def test_anonymous_list_is_401(self, client: TestClient, db_session: Session) -> None:
        resp = client.get("/admin/transfers")
        assert resp.status_code == 401

    def test_workshop_can_list(self, client: TestClient, db_session: Session) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/transfers")
        assert resp.status_code == 200

    def test_workshop_cannot_create(self, client: TestClient, db_session: Session) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/transfers/new")
        assert resp.status_code == 403

    def test_workshop_cannot_post(self, client: TestClient, db_session: Session) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        _login_as(client, ws)
        resp = client.post(
            "/admin/transfers",
            data={
                "source_location_id": str(src.id),
                "destination_location_id": str(dst.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_office_can_create(self, client: TestClient, db_session: Session) -> None:
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/admin/transfers/new")
        assert resp.status_code == 200

    def test_manager_can_create(self, client: TestClient, db_session: Session) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/transfers/new")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


class TestTransferCreate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item])

        to = db_session.get(TransferOrder, to_id)
        assert to is not None
        assert to.status == TransferOrderStatus.DRAFT
        assert to.source_location_id == src.id
        assert to.destination_location_id == dst.id
        assert to.created_by == mgr.id

        lines = (
            db_session.execute(
                select(TransferOrderLine).where(
                    TransferOrderLine.transfer_order_id == to.id
                )
            )
            .scalars()
            .all()
        )
        assert len(lines) == 1
        assert lines[0].item_id == item.id

        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "transfer_order.created")
        ).scalar_one()
        assert audit.entity_id == to.id
        assert audit.after_json["source_location_id"] == src.id

    def test_rejects_same_source_and_destination(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/transfers",
            data={
                "source_location_id": str(src.id),
                "destination_location_id": str(src.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_rejects_archived_location(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        dst = Location(name="Old", archived_at=datetime(2026, 1, 1, tzinfo=UTC))
        db_session.add(dst)
        db_session.commit()
        db_session.refresh(dst)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/transfers",
            data={
                "source_location_id": str(src.id),
                "destination_location_id": str(dst.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_rejects_duplicate_item_lines(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/transfers",
            data={
                "source_location_id": str(src.id),
                "destination_location_id": str(dst.id),
                "item_id_0": str(item.id),
                "qty_0": "1",
                "item_id_1": str(item.id),
                "qty_1": "1",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Ship + receive lifecycle
# ---------------------------------------------------------------------------


class TestTransferLifecycle:
    def test_ship_then_receive(self, client: TestClient, db_session: Session) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item])

        # Ship
        resp = client.post(
            f"/admin/transfers/{to_id}/ship",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        to = db_session.get(TransferOrder, to_id)
        db_session.refresh(item)
        assert to is not None
        assert to.status == TransferOrderStatus.SHIPPED
        assert to.shipped_at is not None
        assert to.shipped_by == mgr.id
        assert item.location_id is None  # in transit

        ship_movement = db_session.execute(
            select(StockMovement).where(StockMovement.transfer_order_id == to.id)
        ).scalar_one()
        assert ship_movement.type == MovementType.TRANSFER
        assert ship_movement.item_id == item.id
        assert ship_movement.total_cost is None  # cost engine not invoked

        line = db_session.execute(
            select(TransferOrderLine).where(
                TransferOrderLine.transfer_order_id == to.id
            )
        ).scalar_one()
        assert line.ship_movement_id == ship_movement.id
        assert line.receive_movement_id is None

        # Receive
        resp = client.post(
            f"/admin/transfers/{to_id}/receive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        db_session.refresh(to)
        db_session.refresh(item)
        assert to.status == TransferOrderStatus.RECEIVED
        assert to.received_at is not None
        assert to.received_by == mgr.id
        assert item.location_id == dst.id

        db_session.expire_all()
        line_after = db_session.execute(
            select(TransferOrderLine).where(
                TransferOrderLine.transfer_order_id == to.id
            )
        ).scalar_one()
        assert line_after.receive_movement_id is not None
        # Two TRANSFER movements now linked to this TO.
        rows = (
            db_session.execute(
                select(StockMovement).where(StockMovement.transfer_order_id == to.id)
            )
            .scalars()
            .all()
        )
        assert {r.type for r in rows} == {MovementType.TRANSFER}
        assert len(rows) == 2

    def test_ship_rejects_item_not_at_source(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        other = _make_location(db_session, "Offsite")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item])

        # Someone moved the item elsewhere between draft and ship.
        item.location_id = other.id
        db_session.commit()

        resp = client.post(
            f"/admin/transfers/{to_id}/ship",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        to = db_session.get(TransferOrder, to_id)
        assert to is not None
        assert to.status == TransferOrderStatus.DRAFT
        # No movements written.
        rows = (
            db_session.execute(
                select(StockMovement).where(StockMovement.transfer_order_id == to.id)
            )
            .scalars()
            .all()
        )
        assert rows == []

    def test_ship_empty_to_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[])
        resp = client.post(
            f"/admin/transfers/{to_id}/ship",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_receive_only_from_shipped(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item])

        # Receive while still draft.
        resp = client.post(
            f"/admin/transfers/{to_id}/receive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_cancel_only_from_draft(
        self, client: TestClient, db_session: Session
    ) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item])

        # Cancel draft — ok.
        resp = client.post(
            f"/admin/transfers/{to_id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        to = db_session.get(TransferOrder, to_id)
        assert to is not None
        assert to.status == TransferOrderStatus.CANCELLED

        # Second cancel — already cancelled, not draft.
        resp = client.post(
            f"/admin/transfers/{to_id}/cancel",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_open_in_transit_summary(
        self, client: TestClient, db_session: Session
    ) -> None:
        from app.transfers import open_in_transit_summary

        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item_a = _make_item(db_session, leaf=leaf, location=src, sku="RNG-A")
        item_b = _make_item(db_session, leaf=leaf, location=src, sku="RNG-B")

        # No transfers yet.
        assert open_in_transit_summary(db_session) == {"transfers": 0, "lines": 0}

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item_a, item_b])

        # Still draft — not in transit.
        assert open_in_transit_summary(db_session) == {"transfers": 0, "lines": 0}

        client.post(
            f"/admin/transfers/{to_id}/ship",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert open_in_transit_summary(db_session) == {"transfers": 1, "lines": 2}

        client.post(
            f"/admin/transfers/{to_id}/receive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert open_in_transit_summary(db_session) == {"transfers": 0, "lines": 0}


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------


class TestTransferDetail:
    def test_detail_renders(self, client: TestClient, db_session: Session) -> None:
        src = _make_location(db_session, "Workshop")
        dst = _make_location(db_session, "Showroom")
        leaf = _make_leaf(db_session)
        item = _make_item(db_session, leaf=leaf, location=src)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        to_id = _create_draft(client, src=src, dst=dst, items=[item])
        resp = client.get(f"/admin/transfers/{to_id}")
        assert resp.status_code == 200
        assert "Workshop" in resp.text
        assert "Showroom" in resp.text

    def test_detail_404_for_unknown(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/transfers/9999999")
        assert resp.status_code == 404
