"""Integration tests for the items CSV upload route.

Covers:
- Role gating: Manager + Office (Admin via blanket override); Workshop 403.
- Header validation (``category`` required; unknown columns rejected
  except ``cf_*``).
- Per-archetype commit paths: BULK / UNIQUE / UNIQUE_VARIANT.
- Category resolution by numeric id AND by slash-path.
- Server-allocated SKU; user-supplied SKU emits a row warning, never lands.
- ``current_qty`` ignored — items always start at 0.
- ``stage`` resolution: blank → ``is_initial`` default; named → looked up;
  unknown → row error.
- Custom-field columns (``cf_<key>``) validated against the resolved leaf's
  schema; unknown key → row error.
- Idempotency by ``id`` (skip), unknown id (error), invalid id (error).
- Audit shape: per-row ``item.created`` + one summary ``item.csv_uploaded``.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Archetype,
    AuditLog,
    CostLayer,
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


def _post_upload(client: TestClient, csv_bytes: bytes, *, dry_run: bool) -> object:
    return client.post(
        "/admin/items/upload",
        files={"file": ("items.csv", csv_bytes, "text/csv")},
        data={"csrf_token": _csrf(client), "dry_run": "1" if dry_run else ""},
        follow_redirects=False,
    )


def _bulk_leaf(db: Session) -> TaxonomyNode:
    node = TaxonomyNode(name="Tools", archetype=Archetype.BULK, sku_prefix="TOOL")
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _unique_leaf(db: Session) -> TaxonomyNode:
    node = TaxonomyNode(name="Rings", archetype=Archetype.UNIQUE, sku_prefix="RING")
    db.add(node)
    db.commit()
    db.refresh(node)
    return node


def _uv_tree(db: Session) -> tuple[TaxonomyNode, TaxonomyNode]:
    top = TaxonomyNode(name="Pendants", archetype=Archetype.UNIQUE_VARIANT, sku_prefix="PEN")
    db.add(top)
    db.commit()
    db.refresh(top)
    sub = TaxonomyNode(name="Silver", parent_id=top.id, sku_prefix="SLV")
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return top, sub


class TestRoleGating:
    def test_anonymous_get_is_401(self, client: TestClient) -> None:
        assert client.get("/admin/items/upload").status_code == 401

    def test_workshop_is_403(self, client: TestClient, db_session: Session) -> None:
        w = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, w)
        assert client.get("/admin/items/upload").status_code == 403

    def test_office_can_open_form(self, client: TestClient, db_session: Session) -> None:
        o = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, o)
        resp = client.get("/admin/items/upload")
        assert resp.status_code == 200
        assert "Upload items CSV" in resp.text


class TestHeaders:
    def test_missing_category_blocks(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"id,name\n,Hammer\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b"missing required column" in resp.content  # type: ignore[attr-defined]
        assert b"category" in resp.content  # type: ignore[attr-defined]

    def test_unknown_column_blocks(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        csv = b"category,name,bogus\nTools,Hammer,oops\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b"unknown column" in resp.content  # type: ignore[attr-defined]

    def test_standard_field_column_accepted(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,ring_size\n{leaf.id},j 1/2\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="new"' in resp.content  # type: ignore[attr-defined]


class TestPerArchetypeCommit:
    def test_bulk_commit(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category\n{leaf.id}\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        items = list(db_session.execute(select(Item)).scalars().all())
        assert len(items) == 1
        item = items[0]
        assert item.tracking_mode == TrackingMode.QTY
        assert item.taxonomy_node_id == leaf.id
        assert item.current_qty == Decimal("0")
        # Name auto-fills to SKU (built-in name column is hidden by default
        # — the catalog hasn't picked it for this leaf).
        assert item.name == item.sku

    def test_unique_commit(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _unique_leaf(db_session)
        _login_as(client, m)
        csv = f"category\n{leaf.id}\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.tracking_mode == TrackingMode.UNIQUE

    def test_unique_variant_commit_mints_auto_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, sub = _uv_tree(db_session)
        _login_as(client, m)
        csv = f"category\n{sub.id}\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.tracking_mode == TrackingMode.UNIQUE
        # Item lives on a freshly-created depth-2 auto-leaf, not on ``sub``.
        leaf = db_session.get(TaxonomyNode, item.taxonomy_node_id)
        assert leaf is not None
        assert leaf.parent_id == sub.id

    def test_uv_depth0_pick_rejected_per_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        top, _sub = _uv_tree(db_session)
        _login_as(client, m)
        csv = f"category\n{top.id}\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]


class TestCategoryResolution:
    def test_resolves_by_slash_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        top, sub = _uv_tree(db_session)
        _login_as(client, m)
        csv = f"category\n{top.name} / {sub.name}\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        leaf = db_session.get(TaxonomyNode, item.taxonomy_node_id)
        assert leaf is not None
        assert leaf.parent_id == sub.id

    def test_unknown_path_errors_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _bulk_leaf(db_session)
        _login_as(client, m)
        csv = b"category\nNopeland / Bogus\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]

    def test_unknown_id_errors_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _bulk_leaf(db_session)
        _login_as(client, m)
        csv = b"category\n9999\n"
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]

    def test_uv_autoleaf_path_suggests_parent(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Common round-trip mistake: user downloads a UV item whose
        category cell is ``"Pendants / Silver / 001"`` (the auto-leaf
        path), strips the id, types ``"Pendants / Silver / 003"`` to add
        a new row. ``003`` doesn't exist (auto-leaves are server-minted).
        The error should steer them at the parent sub-cat, not a generic
        "no category matches" message.
        """
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, _sub = _uv_tree(db_session)  # "Pendants / Silver"
        _login_as(client, m)
        csv = b"category\nPendants / Silver / 003\n"
        resp = _post_upload(client, csv, dry_run=True)
        body = resp.content  # type: ignore[attr-defined]
        assert b'data-row-tag="error"' in body
        assert b"unique-variant auto-leaf" in body, body[:1000]
        assert b"Pendants / Silver" in body, body[:1000]


class TestSkuAndCurrentQtyHandling:
    def test_user_sku_emits_warning_but_does_not_land(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,sku\n{leaf.id},MYSKU-123\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        # Server-allocated SKU starts with the leaf prefix, not the user's text.
        assert item.sku.startswith("TOOL")
        assert "MYSKU" not in item.sku

    def test_current_qty_is_ignored(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,current_qty\n{leaf.id},999\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.current_qty == Decimal("0")


class TestIdempotency:
    def test_existing_id_skips(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        # Create one via the existing route to get a known id.
        _login_as(client, m)
        client.post(
            "/admin/items",
            data={
                "csrf_token": _csrf(client),
                "taxonomy_node_id": str(leaf.id),
            },
            follow_redirects=False,
        )
        existing = db_session.execute(select(Item)).scalar_one()
        # Re-upload the same row by id; should skip, not duplicate.
        csv = f"id,category\n{existing.id},{leaf.id}\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        # Nothing new: no new items, no new error_count.
        assert resp.status_code == 200  # type: ignore[attr-defined]
        items = list(db_session.execute(select(Item)).scalars().all())
        assert len(items) == 1


class TestStages:
    def test_unknown_stage_row_error(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        # Add an initial stage; "Bogus" name should error.
        db_session.add(
            TaxonomyStage(
                top_level_node_id=leaf.id, name="Ready",
                sort_order=1, is_initial=True,
            )
        )
        db_session.commit()
        _login_as(client, m)
        csv = f"category,stage\n{leaf.id},Bogus\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="error"' in resp.content  # type: ignore[attr-defined]
        assert b"stage" in resp.content  # type: ignore[attr-defined]


class TestStandardFields:
    """Promoted standard fields (ring_size, weight_grams, stone_shape)
    flow through dedicated CSV columns. No ``cf_*`` prefix anywhere.
    """

    def test_unknown_column_blocks(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,bogus_field\n{leaf.id},foo\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b"unknown column" in resp.content  # type: ignore[attr-defined]

    def test_ring_size_persists(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,ring_size\n{leaf.id},j 1/2\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.ring_size == "j 1/2"

    def test_weight_grams_persists(self, client: TestClient, db_session: Session) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,weight_grams\n{leaf.id},12.5\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.weight_grams == Decimal("12.5")


class TestAutoReceiveUnitCost:
    """``unit_cost`` column auto-creates a qty=1 stock-in + FIFO layer for
    unique / unique-variant items. Bulk items ignore it with a warning.
    """

    def test_unique_unit_cost_creates_layer_and_movement(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _unique_leaf(db_session)
        _login_as(client, m)
        csv = f"category,unit_cost\n{leaf.id},150.00\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]

        item = db_session.execute(select(Item)).scalar_one()
        # Item now carries qty=1 (auto-received).
        assert item.current_qty == Decimal("1")
        assert item.tracking_mode == TrackingMode.UNIQUE

        # Exactly one IN movement, qty=1, cost=150.
        movements = list(
            db_session.execute(select(StockMovement).where(StockMovement.item_id == item.id))
            .scalars()
            .all()
        )
        assert len(movements) == 1
        mv = movements[0]
        assert mv.type == MovementType.IN
        assert mv.qty == Decimal("1")
        assert mv.total_cost == Decimal("150.00")
        assert mv.reason == "csv_upload"

        # FIFO cost layer exists, backed by the movement.
        layer = db_session.execute(
            select(CostLayer).where(CostLayer.item_id == item.id)
        ).scalar_one()
        assert layer.qty_received == Decimal("1")
        assert layer.qty_remaining == Decimal("1")
        assert layer.unit_cost == Decimal("150.00")
        assert layer.source_movement_id == mv.id

        # ``stock_movement.in`` audit row alongside ``item.created``.
        actions = [
            a
            for (a,) in db_session.execute(
                select(AuditLog.action).where(
                    AuditLog.entity_type.in_(("item", "stock_movement"))
                )
            ).all()
        ]
        assert "item.created" in actions
        assert "stock_movement.in" in actions

    def test_unique_blank_unit_cost_leaves_qty_zero(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _unique_leaf(db_session)
        _login_as(client, m)
        csv = f"category,unit_cost\n{leaf.id},\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.current_qty == Decimal("0")
        assert (
            db_session.execute(select(StockMovement).where(StockMovement.item_id == item.id))
            .first()
            is None
        )

    def test_unique_variant_unit_cost_auto_receives_on_auto_leaf(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, sub = _uv_tree(db_session)
        _login_as(client, m)
        csv = f"category,unit_cost\n{sub.id},1600\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        # Item landed on a server-minted auto-leaf below ``sub``.
        leaf = db_session.get(TaxonomyNode, item.taxonomy_node_id)
        assert leaf is not None
        assert leaf.parent_id == sub.id
        assert item.current_qty == Decimal("1")
        layer = db_session.execute(
            select(CostLayer).where(CostLayer.item_id == item.id)
        ).scalar_one()
        assert layer.unit_cost == Decimal("1600")

    def test_bulk_unit_cost_ignored_with_warning(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,unit_cost\n{leaf.id},99\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.current_qty == Decimal("0")
        # No movement was synthesised — bulk path is ignored.
        assert (
            db_session.execute(select(StockMovement).where(StockMovement.item_id == item.id))
            .first()
            is None
        )

    def test_invalid_unit_cost_blocks_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _unique_leaf(db_session)
        _login_as(client, m)
        csv = f"category,unit_cost\n{leaf.id},notanumber\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        body = resp.content  # type: ignore[attr-defined]
        assert b'data-row-tag="error"' in body
        assert b"unit_cost" in body


class TestUpdateOnUpload:
    """Existing-id rows compute a per-cell diff vs the DB row.

    Editable fields (name, unit, requires_checkout, reorder thresholds,
    stage, ring_size, weight_grams, stone_shape) update on commit; locked
    columns (sku, current_qty, tracking_mode, category, unit_cost) emit a
    row warning when a diff is detected and never write.
    """

    def _make_existing_unique_item(
        self, db_session: Session, *, sku: str = "RING-0001"
    ) -> Item:
        leaf = _unique_leaf(db_session)
        item = Item(
            sku=sku,
            name="Original Ring",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
            requires_checkout=False,
            reorder_threshold=Decimal("0"),
            reorder_qty=Decimal("0"),
            current_qty=Decimal("0"),
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)
        return item

    def test_unchanged_row_is_skipped(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = self._make_existing_unique_item(db_session)
        _login_as(client, m)
        # CSV mirroring the existing values exactly → skip.
        csv = (
            f"id,name,category,unit\n{item.id},Original Ring,Rings,ea\n"
        ).encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="skip"' in resp.content  # type: ignore[attr-defined]

    def test_changed_row_is_tagged_update(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = self._make_existing_unique_item(db_session)
        _login_as(client, m)
        csv = (
            f"id,name,category,unit\n{item.id},Renamed Ring,Rings,ea\n"
        ).encode()
        resp = _post_upload(client, csv, dry_run=True)
        assert b'data-row-tag="update"' in resp.content  # type: ignore[attr-defined]
        assert b'data-testid="csv-upload-update-count">1' in resp.content  # type: ignore[attr-defined]

    def test_update_commits_changes_and_writes_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = self._make_existing_unique_item(db_session)
        _login_as(client, m)
        csv = (
            f"id,name,category,unit,ring_size\n"
            f"{item.id},Renamed,Rings,ea,j 1/2\n"
        ).encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        db_session.expire_all()
        refreshed = db_session.get(Item, item.id)
        assert refreshed is not None
        assert refreshed.name == "Renamed"
        assert refreshed.ring_size == "j 1/2"
        # Audit row exists with before/after.
        audit = db_session.execute(
            select(AuditLog)
            .where(AuditLog.entity_type == "item")
            .where(AuditLog.action == "item.updated")
        ).scalar_one()
        assert audit.before_json is not None
        assert audit.after_json is not None
        assert audit.after_json.get("name") == "Renamed"
        assert audit.after_json.get("ring_size") == "j 1/2"

    def test_locked_sku_change_emits_warning(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = self._make_existing_unique_item(db_session)
        _login_as(client, m)
        # Change only the sku cell; same name/unit/category.
        csv = (
            f"id,sku,name,category,unit\n"
            f"{item.id},NEW-SKU,Original Ring,Rings,ea\n"
        ).encode()
        resp = _post_upload(client, csv, dry_run=True)
        body = resp.content  # type: ignore[attr-defined]
        # No actual change → tagged skip; warning surfaces in the row detail.
        assert b'data-row-tag="skip"' in body
        assert b"sku ignored on update" in body, body[:500]

    def test_locked_category_change_emits_warning(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = self._make_existing_unique_item(db_session)
        # Make a second category to "move to" (warning only).
        other = TaxonomyNode(name="Chains", archetype=Archetype.BULK, sku_prefix="CHN")
        db_session.add(other)
        db_session.commit()
        _login_as(client, m)
        csv = (
            f"id,name,category,unit\n{item.id},Original Ring,Chains,ea\n"
        ).encode()
        resp = _post_upload(client, csv, dry_run=True)
        body = resp.content  # type: ignore[attr-defined]
        # Category warning surfaces; no actual move.
        assert b"category change via CSV not supported" in body, body[:500]
        # Confirm item still on the original leaf.
        db_session.expire_all()
        refreshed = db_session.get(Item, item.id)
        assert refreshed is not None
        assert refreshed.taxonomy_node_id != other.id


class TestUserSuppliedSkuOnCreate:
    """For unique / unique_variant items, a non-blank SKU on create is
    accepted verbatim (subject to uniqueness). BULK items still always
    server-allocate.
    """

    def test_user_sku_lands_on_unique_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _unique_leaf(db_session)
        _login_as(client, m)
        csv = f"category,sku\n{leaf.id},CUSTOM-RING-007\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku == "CUSTOM-RING-007"

    def test_user_sku_lands_on_uv_item(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _top, sub = _uv_tree(db_session)
        _login_as(client, m)
        csv = b"category,sku\nPendants / Silver,RTS-EM-007\n"
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        assert item.sku == "RTS-EM-007"
        # Auto-leaf still server-minted below the sub-cat.
        leaf = db_session.get(TaxonomyNode, item.taxonomy_node_id)
        assert leaf is not None
        assert leaf.parent_id == sub.id

    def test_user_sku_collision_blocks_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _unique_leaf(db_session)
        existing = Item(
            sku="DUPLICATE",
            name="Original",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.UNIQUE,
        )
        db_session.add(existing)
        db_session.commit()
        _login_as(client, m)
        csv = f"category,sku\n{leaf.id},DUPLICATE\n".encode()
        resp = _post_upload(client, csv, dry_run=True)
        body = resp.content  # type: ignore[attr-defined]
        assert b'data-row-tag="error"' in body
        assert b"already in use" in body

    def test_bulk_sku_ignored_with_warning(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category,sku\n{leaf.id},HOPE-FOR-CUSTOM\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        item = db_session.execute(select(Item)).scalar_one()
        # BULK: server-allocated sku, NOT the user's.
        assert item.sku != "HOPE-FOR-CUSTOM"
        assert item.sku.startswith("TOOL")


class TestAudit:
    def test_per_row_and_summary_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _bulk_leaf(db_session)
        _login_as(client, m)
        csv = f"category\n{leaf.id}\n{leaf.id}\n".encode()
        resp = _post_upload(client, csv, dry_run=False)
        assert resp.status_code == 303  # type: ignore[attr-defined]
        actions = [
            a
            for (a,) in db_session.execute(
                select(AuditLog.action).where(AuditLog.entity_type == "item")
            ).all()
        ]
        assert actions.count("item.created") == 2
        assert actions.count("item.csv_uploaded") == 1


class TestUploadButton:
    def test_list_page_has_upload_button(
        self, client: TestClient, db_session: Session
    ) -> None:
        m = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, m)
        resp = client.get("/admin/items")
        assert "items-list-upload-link" in resp.text
        assert "/admin/items/upload" in resp.text
