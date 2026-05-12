"""Integration tests for lifecycle stages (Slice 1 of the in-transit / stages
scope addition).

Two surfaces:

- **Taxonomy admin** (`/admin/taxonomy/{node_id}/stages` + flat
  `/admin/taxonomy/stages/{id}/...`). Manager-only CRUD. Stages are owned by a
  top-level taxonomy node; sub-category as the parent_id is rejected.
- **Item stage transition** (`/admin/items/{item_id}/stage`). Workshop /
  Office / Manager / Admin can transition. The route writes a
  ``STAGE_CHANGE`` stock movement (``qty=0``, ``from_stage_id`` /
  ``to_stage_id`` populated, mandatory reason) and updates
  ``item.current_stage_id``. Cost engine is never invoked.

Out of scope (deferred to Slice 2 + 3): transfer orders, PO in-transit.
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
    MovementType,
    Role,
    StockMovement,
    TaxonomyNode,
    TaxonomyStage,
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


def _make_top_node(db: Session, name: str = "Rings", sku_prefix: str = "RNG") -> TaxonomyNode:
    n = TaxonomyNode(name=name, sku_prefix=sku_prefix)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_sub_node(db: Session, parent: TaxonomyNode, name: str = "Silver") -> TaxonomyNode:
    n = TaxonomyNode(name=name, parent_id=parent.id, sku_prefix="SIL")
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_stage(
    db: Session,
    *,
    top_level: TaxonomyNode,
    name: str,
    sort_order: int = 0,
    is_initial: bool = False,
    archived: bool = False,
) -> TaxonomyStage:
    s = TaxonomyStage(
        top_level_node_id=top_level.id,
        name=name,
        sort_order=sort_order,
        is_initial=is_initial,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _make_item(
    db: Session,
    *,
    leaf: TaxonomyNode,
    sku: str = "RNG-001",
    stage: TaxonomyStage | None = None,
) -> Item:
    item = Item(
        sku=sku,
        name="Silver band",
        taxonomy_node_id=leaf.id,
        unit="ea",
        tracking_mode=TrackingMode.UNIQUE,
        current_stage_id=stage.id if stage is not None else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# Stages admin — role enforcement
# ---------------------------------------------------------------------------


class TestStagesAdminRoleEnforcement:
    def test_anonymous_list_is_401(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        resp = client.get(f"/admin/taxonomy/{node.id}/stages")
        assert resp.status_code == 401

    def test_workshop_list_is_403(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get(f"/admin/taxonomy/{node.id}/stages")
        assert resp.status_code == 403

    def test_office_list_is_403(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get(f"/admin/taxonomy/{node.id}/stages")
        assert resp.status_code == 403

    def test_manager_list_is_200(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/taxonomy/{node.id}/stages")
        assert resp.status_code == 200

    def test_admin_list_is_200(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        adm = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, adm)
        resp = client.get(f"/admin/taxonomy/{node.id}/stages")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Stages admin — create / update / archive / unarchive
# ---------------------------------------------------------------------------


class TestStageCreate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/stages",
            data={
                "name": "Raw",
                "sort_order": "10",
                "is_initial": "true",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        stage = db_session.execute(select(TaxonomyStage)).scalar_one()
        assert stage.name == "Raw"
        assert stage.sort_order == 10
        assert stage.is_initial is True
        assert stage.top_level_node_id == node.id

        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "taxonomy_stage.created")
        ).scalar_one()
        assert audit.entity_id == stage.id
        assert audit.after_json["name"] == "Raw"
        assert audit.after_json["is_initial"] is True

    def test_sort_order_defaults_to_next_step(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_top_node(db_session)
        _make_stage(db_session, top_level=node, name="Raw", sort_order=10)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/stages",
            data={"name": "Polishing", "sort_order": "", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        polishing = db_session.execute(
            select(TaxonomyStage).where(TaxonomyStage.name == "Polishing")
        ).scalar_one()
        assert polishing.sort_order == 20  # 10 + step

    def test_rejects_sub_category(self, client: TestClient, db_session: Session) -> None:
        top = _make_top_node(db_session)
        sub = _make_sub_node(db_session, top)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{sub.id}/stages",
            data={"name": "Raw", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_rejects_duplicate_name(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        _make_stage(db_session, top_level=node, name="Raw")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/stages",
            data={"name": "Raw", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_rejects_blank_name(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/stages",
            data={"name": "  ", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_setting_initial_clears_other_initial(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_top_node(db_session)
        existing = _make_stage(db_session, top_level=node, name="Raw", is_initial=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/{node.id}/stages",
            data={"name": "Polishing", "is_initial": "true", "csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(existing)
        assert existing.is_initial is False
        polishing = db_session.execute(
            select(TaxonomyStage).where(TaxonomyStage.name == "Polishing")
        ).scalar_one()
        assert polishing.is_initial is True


class TestStageUpdate:
    def test_happy_path(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        stage = _make_stage(db_session, top_level=node, name="Raw", sort_order=0)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/stages/{stage.id}",
            data={
                "name": "Raw Material",
                "sort_order": "5",
                "is_initial": "true",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(stage)
        assert stage.name == "Raw Material"
        assert stage.sort_order == 5
        assert stage.is_initial is True

        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "taxonomy_stage.updated")
        ).scalar_one()
        assert audit.before_json["name"] == "Raw"
        assert audit.after_json["name"] == "Raw Material"

    def test_noop_writes_no_audit(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        stage = _make_stage(db_session, top_level=node, name="Raw", sort_order=0)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/stages/{stage.id}",
            data={
                "name": "Raw",
                "sort_order": "0",
                "is_initial": "",  # checkbox unchecked
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        rows = db_session.execute(
            select(AuditLog).where(AuditLog.action == "taxonomy_stage.updated")
        ).scalars().all()
        assert list(rows) == []


class TestStageArchiveUnarchive:
    def test_archive_unarchive(self, client: TestClient, db_session: Session) -> None:
        node = _make_top_node(db_session)
        stage = _make_stage(db_session, top_level=node, name="Polishing")
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # archive
        resp = client.post(
            f"/admin/taxonomy/stages/{stage.id}/archive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(stage)
        assert stage.archived_at is not None
        # unarchive
        resp = client.post(
            f"/admin/taxonomy/stages/{stage.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(stage)
        assert stage.archived_at is None

    def test_unarchive_clears_initial_when_other_active_initial_exists(
        self, client: TestClient, db_session: Session
    ) -> None:
        node = _make_top_node(db_session)
        archived = _make_stage(
            db_session,
            top_level=node,
            name="Old Initial",
            is_initial=True,
            archived=True,
        )
        _make_stage(db_session, top_level=node, name="New Initial", is_initial=True)
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/taxonomy/stages/{archived.id}/unarchive",
            data={"csrf_token": _csrf(client)},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(archived)
        assert archived.archived_at is None
        assert archived.is_initial is False  # cleared to preserve unique-initial invariant


# ---------------------------------------------------------------------------
# Item stage transitions
# ---------------------------------------------------------------------------


class TestItemStageTransition:
    def test_workshop_can_transition(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True, sort_order=0)
        polishing = _make_stage(db_session, top_level=top, name="Polishing", sort_order=10)
        item = _make_item(db_session, leaf=top, stage=raw)

        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)

        resp = client.post(
            f"/admin/items/{item.id}/stage",
            data={
                "to_stage_id": str(polishing.id),
                "reason": "Started polishing",
                "note": "Looks good",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        db_session.refresh(item)
        assert item.current_stage_id == polishing.id

        movement = db_session.execute(select(StockMovement)).scalar_one()
        assert movement.type == MovementType.STAGE_CHANGE
        assert movement.qty == Decimal("0")
        assert movement.from_stage_id == raw.id
        assert movement.to_stage_id == polishing.id
        assert movement.reason == "Started polishing"
        assert movement.note == "Looks good"
        assert movement.total_cost is None
        assert movement.user_id == ws.id

        audit = db_session.execute(
            select(AuditLog).where(AuditLog.action == "stock_movement.stage_change")
        ).scalar_one()
        assert audit.entity_id == movement.id
        assert audit.before_json["current_stage_id"] == raw.id
        assert audit.before_json["current_stage_name"] == "Raw"
        assert audit.after_json["current_stage_id"] == polishing.id
        assert audit.after_json["current_stage_name"] == "Polishing"

    def test_reason_required(self, client: TestClient, db_session: Session) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True)
        polishing = _make_stage(db_session, top_level=top, name="Polishing", sort_order=10)
        item = _make_item(db_session, leaf=top, stage=raw)

        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.post(
            f"/admin/items/{item.id}/stage",
            data={
                "to_stage_id": str(polishing.id),
                "reason": "",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.refresh(item)
        assert item.current_stage_id == raw.id  # unchanged
        assert db_session.execute(select(StockMovement)).first() is None

    def test_target_must_belong_to_same_category(
        self, client: TestClient, db_session: Session
    ) -> None:
        rings = _make_top_node(db_session, name="Rings", sku_prefix="RNG")
        raw_rings = _make_stage(db_session, top_level=rings, name="Raw", is_initial=True)
        other_top = _make_top_node(db_session, name="Tools", sku_prefix="TOL")
        other_stage = _make_stage(db_session, top_level=other_top, name="Issued")
        item = _make_item(db_session, leaf=rings, stage=raw_rings)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/stage",
            data={
                "to_stage_id": str(other_stage.id),
                "reason": "trying to cheat",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400
        db_session.refresh(item)
        assert item.current_stage_id == raw_rings.id

    def test_archived_target_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True)
        archived = _make_stage(
            db_session, top_level=top, name="Old Stage", archived=True
        )
        item = _make_item(db_session, leaf=top, stage=raw)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/stage",
            data={
                "to_stage_id": str(archived.id),
                "reason": "should fail",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_same_stage_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True)
        item = _make_item(db_session, leaf=top, stage=raw)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/stage",
            data={
                "to_stage_id": str(raw.id),
                "reason": "no-op",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_archived_item_rejected(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True)
        polishing = _make_stage(db_session, top_level=top, name="Polishing", sort_order=10)
        item = _make_item(db_session, leaf=top, stage=raw)
        item.archived_at = datetime(2026, 1, 1, tzinfo=UTC)
        db_session.commit()

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            f"/admin/items/{item.id}/stage",
            data={
                "to_stage_id": str(polishing.id),
                "reason": "trying",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_form_excludes_current_stage_and_archived(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True, sort_order=0)
        polishing = _make_stage(db_session, top_level=top, name="Polishing", sort_order=10)
        archived = _make_stage(
            db_session, top_level=top, name="Old", sort_order=20, archived=True
        )
        item = _make_item(db_session, leaf=top, stage=raw)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get(f"/admin/items/{item.id}/stage")
        assert resp.status_code == 200
        body = resp.text
        # Polishing should appear as a valid option.
        assert f'value="{polishing.id}"' in body
        # Raw (current) and archived (Old) must not.
        assert f'value="{raw.id}"' not in body
        assert f'value="{archived.id}"' not in body


class TestItemCreateDefaultsToInitialStage:
    """Item create should default ``current_stage_id`` to the leaf's top-level
    category's ``is_initial`` stage when one exists."""

    def test_defaults_to_initial(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Build a depth-0 BULK category with two stages; the initial one is
        # Raw. Item-create POST should leave the new row on Raw.
        from app.models import Archetype  # local import to avoid top-level churn

        top = TaxonomyNode(
            name="Findings",
            sku_prefix="FND",
            archetype=Archetype.BULK,
        )
        db_session.add(top)
        db_session.commit()
        raw = _make_stage(db_session, top_level=top, name="On hand", is_initial=True, sort_order=0)
        _make_stage(db_session, top_level=top, name="Issued", sort_order=10)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data={
                "name": "Jump rings 4mm",
                "taxonomy_node_id": str(top.id),
                "unit": "ea",
                "tracking_mode": "qty",
                "reorder_threshold": "0",
                "reorder_qty": "0",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.current_stage_id == raw.id

    def test_no_initial_leaves_null(
        self, client: TestClient, db_session: Session
    ) -> None:
        from app.models import Archetype

        top = TaxonomyNode(name="Findings", sku_prefix="FND", archetype=Archetype.BULK)
        db_session.add(top)
        db_session.commit()
        # No stages — current_stage_id must stay null.
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.post(
            "/admin/items",
            data={
                "name": "Jump rings 4mm",
                "taxonomy_node_id": str(top.id),
                "unit": "ea",
                "tracking_mode": "qty",
                "reorder_threshold": "0",
                "reorder_qty": "0",
                "csrf_token": _csrf(client),
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        item = db_session.execute(select(Item)).scalar_one()
        assert item.current_stage_id is None


# ---------------------------------------------------------------------------
# Items list — Stage column + CSV export
# ---------------------------------------------------------------------------


class TestItemsListStageColumn:
    def test_html_list_shows_stage(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True)
        _make_item(db_session, leaf=top, stage=raw)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        assert resp.status_code == 200
        assert ">Raw<" in resp.text or ">Raw " in resp.text or "Raw\n" in resp.text

    def test_csv_export_includes_stage_column(
        self, client: TestClient, db_session: Session
    ) -> None:
        top = _make_top_node(db_session)
        raw = _make_stage(db_session, top_level=top, name="Raw", is_initial=True)
        _make_item(db_session, leaf=top, stage=raw)

        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items?format=csv")
        assert resp.status_code == 200
        header_line = resp.text.splitlines()[0]
        assert "stage" in header_line.lower()
        body_line = resp.text.splitlines()[1]
        assert "Raw" in body_line
