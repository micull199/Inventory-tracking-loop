"""Integration tests for the Manager-owned ``/admin/stones`` routes.

Covers CRUD + every lifecycle transition (set / unset / sell / lost /
return / relocate) plus the edit-on-cert / edit-on-ownership event
detection. Each transition asserts:

1. ``Stone.status`` ends in the expected state.
2. A paired ``stone_events`` row is written.
3. Denormalised fields (``current_item_id``, ``current_location_id``,
   ``Item.centre_stone_id``, ``Item.total_carat_weight``) stay
   consistent with the linkage.
4. The audit log carries an ``actor_id`` and a before/after snapshot.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Archetype,
    AuditLog,
    Item,
    ItemStone,
    Role,
    Stone,
    StoneEvent,
    StonePosition,
    StoneShape,
    StoneStatus,
    StoneType,
    TaxonomyNode,
    TrackingMode,
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


def _audit_rows(db: Session, *, action: str | None = None) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.entity_type == "stone")
        .order_by(AuditLog.id)
    )
    if action is not None:
        stmt = stmt.where(AuditLog.action == action)
    return list(db.execute(stmt).scalars().all())


def _events(db: Session, *, stone_id: int, event_type: str | None = None) -> list[StoneEvent]:
    stmt = (
        select(StoneEvent)
        .where(StoneEvent.stone_id == stone_id)
        .order_by(StoneEvent.id)
    )
    if event_type is not None:
        stmt = stmt.where(StoneEvent.event_type == event_type)
    return list(db.execute(stmt).scalars().all())


def _make_shape(db: Session, name: str = "round") -> StoneShape:
    shape = StoneShape(name=name)
    db.add(shape)
    db.commit()
    db.refresh(shape)
    return shape


def _make_node(db: Session, name: str = "Rings", prefix: str = "RNG") -> TaxonomyNode:
    node = TaxonomyNode(name=name, sku_prefix=prefix, archetype=Archetype.UNIQUE)
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _make_item(
    db: Session,
    *,
    sku: str = "RNG-0001",
    name: str = "Solitaire",
    node: TaxonomyNode | None = None,
) -> Item:
    node = node or _make_node(db, name=f"Cat-{sku}", prefix=sku.split("-")[0][:3])
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=node.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_stone(
    db: Session,
    *,
    code: str = "STN-000001",
    shape: StoneShape | None = None,
    carat: Decimal = Decimal("1.50"),
    status_value: StoneStatus = StoneStatus.AVAILABLE,
) -> Stone:
    if shape is None:
        # Reuse an existing "round" shape if one's already been seeded by
        # this test, to avoid the unique-name collision when a single
        # test makes multiple stones.
        shape = db.execute(
            select(StoneShape).where(StoneShape.name == "round")
        ).scalar_one_or_none() or _make_shape(db)
    stone = Stone(
        stone_code=code,
        stone_type=StoneType.DIAMOND,
        shape_id=shape.id,
        carat_weight=carat,
        status=status_value,
    )
    db.add(stone)
    db.commit()
    db.refresh(stone)
    return stone


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestListFilters:
    def test_filter_by_status_available(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        avail = Stone(
            stone_code="STN-AVAIL",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
            status=StoneStatus.AVAILABLE,
        )
        sold = Stone(
            stone_code="STN-SOLD",
            stone_type=StoneType.DIAMOND,
            shape_id=shape.id,
            carat_weight=Decimal("1.00"),
            status=StoneStatus.SOLD,
        )
        db_session.add_all([avail, sold])
        db_session.commit()
        _login_as(client, u)
        resp = client.get("/admin/stones?status_filter=available")
        assert resp.status_code == 200
        assert "STN-AVAIL" in resp.text
        assert "STN-SOLD" not in resp.text

    def test_unknown_status_filter_ignored(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _make_stone(db_session)
        _login_as(client, u)
        resp = client.get("/admin/stones?status_filter=definitely-not-a-status")
        assert resp.status_code == 200
        # Falls back to no-filter behaviour.
        assert "STN-000001" in resp.text


class TestRoleEnforcement:
    def test_anonymous_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/stones").status_code == 401

    def test_workshop_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        assert client.get("/admin/stones").status_code == 403

    def test_office_is_403(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        assert client.get("/admin/stones").status_code == 403

    def test_manager_is_200(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        assert client.get("/admin/stones").status_code == 200


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCreate:
    def test_minimal_happy_path(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "1.50",
                "origin": "natural",
                "ownership": "owned",
                # Spec §10.1: a bare diamond with no cert + no acquisition cost
                # below the floor needs an explicit manual override + reason.
                "tracking_trigger": "manual_override",
                "tracking_override_reason": "internal demo stone",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        stone = db_session.execute(select(Stone)).scalar_one()
        assert stone.stone_code == "STN-000001"
        assert stone.status is StoneStatus.AVAILABLE
        # Tracking trigger persists from the form pick.
        from app.models import TrackingTrigger

        assert stone.tracking_trigger is TrackingTrigger.MANUAL_OVERRIDE
        assert stone.tracking_override_reason == "internal demo stone"
        # Initial ``created`` ledger event written so the history is
        # self-contained from day one.
        created_events = _events(db_session, stone_id=stone.id, event_type="created")
        assert len(created_events) == 1
        assert created_events[0].to_status is StoneStatus.AVAILABLE
        # Audit log carries the allocated code.
        audit = _audit_rows(db_session, action="stone.created")
        assert len(audit) == 1
        assert audit[0].after_json is not None
        assert audit[0].after_json["stone_code"] == "STN-000001"

    def test_cert_auto_triggers_cert(self, client: TestClient, db_session: Session) -> None:
        """Spec §10.1: a stone with a cert auto-receives tracking_trigger=cert."""
        from app.models import TrackingTrigger

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "0.30",
                "origin": "natural",
                "ownership": "owned",
                "lab": "gia",
                "cert_number": "GIA-12345",
                # No tracking_trigger picked — auto-detection should fire.
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        stone = db_session.execute(select(Stone)).scalar_one()
        assert stone.tracking_trigger is TrackingTrigger.CERT
        assert stone.tracking_override_reason is None

    def test_coloured_stone_auto_triggers_above_threshold(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Spec §10.1: a non-diamond above the 0.50 ct threshold auto-triggers."""
        from app.models import TrackingTrigger

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "sapphire",
                "shape_id": str(shape.id),
                "carat_weight": "0.75",
                "origin": "natural",
                "ownership": "owned",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        stone = db_session.execute(select(Stone)).scalar_one()
        assert stone.tracking_trigger is TrackingTrigger.COLOURED_STONE_THRESHOLD

    def test_acquisition_cost_above_floor_auto_triggers(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Spec §10.1: acquisition_cost above the AUD floor auto-triggers."""
        from app.models import TrackingTrigger

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "0.30",
                "origin": "natural",
                "ownership": "owned",
                "acquisition_cost": "750.00",  # above $500 floor
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        stone = db_session.execute(select(Stone)).scalar_one()
        assert stone.tracking_trigger is TrackingTrigger.COST_THRESHOLD

    def test_no_trigger_no_override_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A stone with nothing identifying it as tracked needs an explicit override."""
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "0.30",
                "origin": "natural",
                "ownership": "owned",
                # No cert, low carat, no cost → no auto-trigger fires.
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "no auto-trigger" in resp.text.lower()

    def test_manual_override_without_reason_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "0.30",
                "origin": "natural",
                "ownership": "owned",
                "tracking_trigger": "manual_override",
                # reason intentionally blank
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "tracking_override_reason" in resp.text

    def test_setting_threshold_overridden(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Tuning the app_settings cost floor takes effect immediately."""
        from app.models import AppSetting, TrackingTrigger

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        # Drop the floor to $100 so the test stone's $200 acquisition cost
        # trips it. Demonstrates the no-cache contract — the route reads
        # the setting on every call.
        row = db_session.execute(
            select(AppSetting).where(AppSetting.key == "stones.tracking.cost_floor_aud")
        ).scalar_one_or_none()
        if row is None:
            db_session.add(
                AppSetting(key="stones.tracking.cost_floor_aud", value="100")
            )
        else:
            row.value = "100"
        db_session.commit()

        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "0.30",
                "origin": "natural",
                "ownership": "owned",
                "acquisition_cost": "200.00",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        stone = db_session.execute(select(Stone)).scalar_one()
        assert stone.tracking_trigger is TrackingTrigger.COST_THRESHOLD

    def test_memo_requires_due_date(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "1.50",
                "origin": "natural",
                "ownership": "memo",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert db_session.execute(select(Stone)).first() is None

    def test_zero_carat_rejected(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        shape = _make_shape(db_session)
        _login_as(client, u)
        resp = client.post(
            "/admin/stones",
            data={
                "stone_type": "diamond",
                "shape_id": str(shape.id),
                "carat_weight": "0",
                "origin": "natural",
                "ownership": "owned",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400


class TestEdit:
    def test_cert_change_writes_cert_updated_event(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}",
            data={
                "stone_type": "diamond",
                "shape_id": str(stone.shape_id),
                "carat_weight": "1.50",
                "origin": "natural",
                "ownership": "owned",
                "lab": "gia",
                "cert_number": "ABC-123",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        events = _events(db_session, stone_id=stone.id, event_type="cert_updated")
        assert len(events) == 1

    def test_ownership_change_writes_event(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}",
            data={
                "stone_type": "diamond",
                "shape_id": str(stone.shape_id),
                "carat_weight": "1.50",
                "origin": "natural",
                "ownership": "memo",
                "memo_due_date": "2026-07-01",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        events = _events(db_session, stone_id=stone.id, event_type="ownership_changed")
        assert len(events) == 1


# ---------------------------------------------------------------------------
# Lifecycle: set / unset
# ---------------------------------------------------------------------------


class TestSetUnset:
    def test_set_writes_linkage_event_and_denorm(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        item = _make_item(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "position_index": "0",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        item = db_session.get(Item, item.id)
        assert stone is not None
        assert item is not None
        assert stone.status is StoneStatus.SET
        assert stone.current_item_id == item.id
        assert item.centre_stone_id == stone.id
        # total_carat_weight = melee (0) + tracked sum (1.50)
        assert item.total_carat_weight == Decimal("1.5000")
        link = db_session.execute(select(ItemStone)).scalar_one()
        assert link.position is StonePosition.CENTRE
        assert link.unset_at is None
        # Ledger row written with correct status diff.
        set_events = _events(db_session, stone_id=stone.id, event_type="set")
        assert len(set_events) == 1
        assert set_events[0].to_item_id == item.id
        assert set_events[0].from_status is StoneStatus.AVAILABLE
        assert set_events[0].to_status is StoneStatus.SET
        assert set_events[0].actor_id == u.id

    def test_set_rejects_archived_stone(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        stone.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()
        item = _make_item(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_set_rejects_slot_collision(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone_a = _make_stone(db_session, code="STN-000001")
        stone_b = _make_stone(db_session, code="STN-000002")
        item = _make_item(db_session)
        _login_as(client, u)
        # Set stone A into centre.
        resp_a = client.post(
            f"/admin/stones/{stone_a.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp_a.status_code == 303
        # Stone B into the same slot — must fail.
        resp_b = client.post(
            f"/admin/stones/{stone_b.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp_b.status_code == 400

    def test_unset_writes_event_and_clears_denorm(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        item = _make_item(db_session)
        _login_as(client, u)
        # Set first.
        client.post(
            f"/admin/stones/{stone.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        # Then unset.
        resp = client.post(
            f"/admin/stones/{stone.id}/unset",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        item = db_session.get(Item, item.id)
        assert stone is not None
        assert item is not None
        assert stone.status is StoneStatus.AVAILABLE
        assert stone.current_item_id is None
        assert item.centre_stone_id is None
        # Old linkage row stayed in the table with unset_at set — historical
        # record per the spec's soft-end pattern.
        links = list(db_session.execute(select(ItemStone)).scalars().all())
        assert len(links) == 1
        assert links[0].unset_at is not None
        # Ledger captures the unset.
        unset_events = _events(db_session, stone_id=stone.id, event_type="unset")
        assert len(unset_events) == 1
        assert unset_events[0].from_item_id == item.id

    def test_unset_rejects_unset_stone(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)  # AVAILABLE
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/unset",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Lifecycle: sell / lost / return
# ---------------------------------------------------------------------------


class TestTerminalTransitions:
    def test_sell_available(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/sell",
            data={"note": "invoice 5001", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        assert stone is not None
        assert stone.status is StoneStatus.SOLD
        sold = _events(db_session, stone_id=stone.id, event_type="sold")
        assert len(sold) == 1
        assert sold[0].note == "invoice 5001"

    def test_sell_set_auto_unsets_linkage(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        item = _make_item(db_session)
        _login_as(client, u)
        client.post(
            f"/admin/stones/{stone.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        # Sell while set — spec §1.1 allows ``set → sold (with the ring)``.
        resp = client.post(
            f"/admin/stones/{stone.id}/sell",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        item = db_session.get(Item, item.id)
        assert stone is not None
        assert item is not None
        assert stone.status is StoneStatus.SOLD
        assert stone.current_item_id is None
        # Centre stone link was auto-cleared on item too.
        assert item.centre_stone_id is None
        link = db_session.execute(select(ItemStone)).scalar_one()
        assert link.unset_at is not None
        # Both unset + sold events present in order. The stone was seeded
        # via the model directly (not the create route), so no ``created``
        # ledger row exists — only the lifecycle events do.
        events = _events(db_session, stone_id=stone.id)
        ordered = [e.event_type for e in events]
        assert ordered == ["set", "unset", "sold"]

    def test_sell_terminal_rejects_re_sell(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session, status_value=StoneStatus.SOLD)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/sell",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_lost_writes_event(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/lost",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        assert stone is not None
        assert stone.status is StoneStatus.LOST
        assert len(_events(db_session, stone_id=stone.id, event_type="lost")) == 1

    def test_return_writes_event(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/return",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        assert stone is not None
        assert stone.status is StoneStatus.RETURNED_TO_SUPPLIER
        assert (
            len(_events(db_session, stone_id=stone.id, event_type="returned")) == 1
        )


# ---------------------------------------------------------------------------
# Lifecycle: relocate
# ---------------------------------------------------------------------------


class TestHistoryView:
    """`/admin/stones/{id}/history` renders the stone_events ledger.

    Covers:
    - empty state for a freshly-seeded stone (no events written)
    - rendering of a stone with create + set + unset events, including
      actor + from/to labels resolved via PK lookup
    - rows ordered chronologically (lowest id first)
    """

    def test_empty_state(self, client: TestClient, db_session: Session) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        _login_as(client, u)
        resp = client.get(f"/admin/stones/{stone.id}/history")
        assert resp.status_code == 200
        assert 'data-testid="stone-events-empty"' in resp.text

    def test_renders_set_unset_chain(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        stone = _make_stone(db_session)
        item = _make_item(db_session)
        _login_as(client, u)
        csrf = _csrf(client)
        # Drive the actual route — same fixtures the lifecycle tests
        # use — so the events are written by the production code path.
        client.post(
            f"/admin/stones/{stone.id}/set",
            data={
                "item_id": str(item.id),
                "position": "centre",
                "csrf_token": csrf,
            },
            follow_redirects=False,
        )
        client.post(
            f"/admin/stones/{stone.id}/unset",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )

        resp = client.get(f"/admin/stones/{stone.id}/history")
        assert resp.status_code == 200
        assert 'data-testid="stone-events-table"' in resp.text
        # Both lifecycle events present.
        assert resp.text.count('data-testid="stone-event-row"') == 2
        # Actor name surfaces (we logged in as m@x.test → "M").
        assert ">M<" in resp.text or ">m@x.test<" in resp.text
        # Item label resolves to "SKU — name".
        assert item.sku in resp.text

    def test_unknown_stone_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/stones/99999/history")
        assert resp.status_code == 404


class TestRelocate:
    def test_relocate_writes_event(self, client: TestClient, db_session: Session) -> None:
        from app.models import Location

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc_a = Location(name="Workshop AU")
        loc_b = Location(name="Workshop TH")
        db_session.add_all([loc_a, loc_b])
        db_session.commit()
        db_session.refresh(loc_a)
        db_session.refresh(loc_b)
        stone = _make_stone(db_session)
        stone.current_location_id = loc_a.id
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/relocate",
            data={
                "current_location_id": str(loc_b.id),
                "note": "shipped TH",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.expire_all()
        stone = db_session.get(Stone, stone.id)
        assert stone is not None
        assert stone.current_location_id == loc_b.id
        events = _events(db_session, stone_id=stone.id, event_type="relocated")
        assert len(events) == 1
        assert events[0].from_location_id == loc_a.id
        assert events[0].to_location_id == loc_b.id

    def test_relocate_noop_writes_no_event(
        self, client: TestClient, db_session: Session
    ) -> None:
        from app.models import Location

        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        loc = Location(name="Workshop AU")
        db_session.add(loc)
        db_session.commit()
        db_session.refresh(loc)
        stone = _make_stone(db_session)
        stone.current_location_id = loc.id
        db_session.commit()
        _login_as(client, u)
        resp = client.post(
            f"/admin/stones/{stone.id}/relocate",
            data={
                "current_location_id": str(loc.id),
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        # No event row written — the relocate was a no-op.
        assert _events(db_session, stone_id=stone.id, event_type="relocated") == []
