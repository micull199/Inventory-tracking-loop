"""Integration tests for the reorder dashboard (PO1).

Covers:
- Role enforcement: anonymous 401; pending 403; Workshop 403; Office / Manager
  / Admin all 200.
- Empty state: no items, or only above-threshold items, render the empty-state
  block and no group sections.
- Single supplier: one at-or-below-threshold item renders one group with one
  row carrying the right SKU / name / current_qty / threshold / reorder_qty /
  deficit / detail-link / stock-in-link.
- No-supplier bucket: items with ``supplier_id IS NULL`` group under
  "(no supplier)" with ``data-supplier-id="none"``; coexists with named-
  supplier groups.
- Multiple suppliers: groups ordered alphabetically by supplier name; rows
  within each group ordered by SKU.
- Threshold edge: ``current_qty == reorder_threshold`` is included (the
  trigger is "at or below"); strictly above is excluded.
- Archived item filter: archived items don't show even when below threshold;
  un-archiving brings them back.
- Archived supplier surfacing: an archived supplier still groups its items
  but the label gets an "(archived)" suffix and the section carries
  ``data-supplier-archived="true"``.
- Zero threshold: ``threshold=0 AND current_qty=0`` matches (deficit=0); a
  positive current_qty against a zero threshold does not.

The route is read-only (no audit, no movement). PO2 will add the write
surface.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Item,
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


def _make_leaf(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_supplier(
    db: Session, name: str = "ACME", *, archived: bool = False
) -> Supplier:
    s = Supplier(
        name=name,
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
    sku: str,
    name: str = "Item",
    current_qty: Decimal = Decimal("0"),
    threshold: Decimal = Decimal("10"),
    reorder_qty: Decimal = Decimal("100"),
    supplier: Supplier | None = None,
    archived: bool = False,
) -> Item:
    item = Item(
        sku=sku,
        name=name,
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
        current_qty=current_qty,
        reorder_threshold=threshold,
        reorder_qty=reorder_qty,
        supplier_id=supplier.id if supplier is not None else None,
        archived_at=datetime(2026, 1, 1, tzinfo=UTC) if archived else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


# ---------------------------------------------------------------------------
# Role enforcement
# ---------------------------------------------------------------------------


class TestReorderRoleEnforcement:
    def test_anonymous_get_is_401(self, client: TestClient) -> None:
        resp = client.get("/admin/reorder")
        assert resp.status_code == 401

    def test_pending_user_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session,
            email="p@x.test",
            role=Role.MANAGER,
            status=UserStatus.PENDING,
        )
        _login_as(client, u)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 403

    def test_workshop_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        ws = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, ws)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 403

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 200

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 200

    def test_admin_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------


class TestReorderEmptyState:
    def test_no_items_renders_empty_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 200
        assert 'data-testid="reorder-empty"' in resp.text
        assert 'data-testid="reorder-group"' not in resp.text

    def test_only_above_threshold_renders_empty_state(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        # current_qty (50) is strictly above threshold (10) — should not show.
        _make_item(
            db_session,
            leaf=leaf,
            sku="SKU-OK",
            current_qty=Decimal("50"),
            threshold=Decimal("10"),
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 200
        assert 'data-testid="reorder-empty"' in resp.text
        assert 'data-testid="reorder-row"' not in resp.text


# ---------------------------------------------------------------------------
# Single-supplier rendering
# ---------------------------------------------------------------------------


class TestSingleSupplierRendering:
    def test_one_below_threshold_item_renders_group_and_row(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="Bullion Co")
        _make_item(
            db_session,
            leaf=leaf,
            sku="SLV-001",
            name="Silver wire",
            current_qty=Decimal("3"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("100"),
            supplier=sup,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert resp.status_code == 200
        assert 'data-testid="reorder-empty"' not in resp.text
        assert resp.text.count('data-testid="reorder-group"') == 1
        assert resp.text.count('data-testid="reorder-row"') == 1
        assert "Bullion Co" in resp.text
        assert 'data-supplier-archived="true"' not in resp.text

    def test_row_shows_qty_threshold_reorder_qty_and_deficit(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="Bullion Co")
        _make_item(
            db_session,
            leaf=leaf,
            sku="SLV-001",
            name="Silver wire",
            current_qty=Decimal("3"),
            threshold=Decimal("10"),
            reorder_qty=Decimal("100"),
            supplier=sup,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        # Slice to the row block to keep assertions tight.
        row_idx = resp.text.find('data-testid="reorder-row"')
        row = resp.text[row_idx : row_idx + 1500]
        assert 'data-testid="reorder-current-qty">3' in row
        assert 'data-testid="reorder-threshold">10' in row
        assert 'data-testid="reorder-reorder-qty">100' in row
        assert 'data-testid="reorder-deficit">7' in row  # 10 - 3
        assert 'data-testid="reorder-item-sku">SLV-001' in row
        assert 'data-testid="reorder-item-name">Silver wire' in row

    def test_row_links_to_detail_and_stock_in(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="Bullion Co")
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="SLV-001",
            current_qty=Decimal("0"),
            threshold=Decimal("10"),
            supplier=sup,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert f'href="/admin/items/{item.id}/detail"' in resp.text
        assert f'href="/admin/items/{item.id}/in"' in resp.text
        assert 'data-testid="reorder-detail-link"' in resp.text
        assert 'data-testid="reorder-stock-in-link"' in resp.text


# ---------------------------------------------------------------------------
# No-supplier bucket
# ---------------------------------------------------------------------------


class TestNoSupplierBucket:
    def test_unassigned_item_buckets_under_no_supplier(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="ORPHAN",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=None,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert 'data-supplier-id="none"' in resp.text
        assert "(no supplier)" in resp.text

    def test_no_supplier_bucket_coexists_with_supplier_group(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="Bullion Co")
        _make_item(
            db_session,
            leaf=leaf,
            sku="SUP-001",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="ORPHAN",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=None,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        # Exactly two groups (named supplier + no-supplier bucket).
        assert resp.text.count('data-testid="reorder-group"') == 2
        assert f'data-supplier-id="{sup.id}"' in resp.text
        assert 'data-supplier-id="none"' in resp.text


# ---------------------------------------------------------------------------
# Multiple suppliers — ordering
# ---------------------------------------------------------------------------


class TestMultipleSuppliers:
    def test_groups_ordered_alphabetically_by_supplier_name(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        zebra = _make_supplier(db_session, name="Zebra Metals")
        alpha = _make_supplier(db_session, name="Alpha Bullion")
        _make_item(
            db_session,
            leaf=leaf,
            sku="Z-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=zebra,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="A-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=alpha,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        alpha_idx = resp.text.find("Alpha Bullion")
        zebra_idx = resp.text.find("Zebra Metals")
        assert alpha_idx > 0
        assert zebra_idx > 0
        assert alpha_idx < zebra_idx

    def test_rows_within_a_group_ordered_by_sku(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="ACME")
        _make_item(
            db_session,
            leaf=leaf,
            sku="C-3",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="A-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _make_item(
            db_session,
            leaf=leaf,
            sku="B-2",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        a_idx = resp.text.find('data-testid="reorder-item-sku">A-1')
        b_idx = resp.text.find('data-testid="reorder-item-sku">B-2')
        c_idx = resp.text.find('data-testid="reorder-item-sku">C-3')
        assert 0 < a_idx < b_idx < c_idx


# ---------------------------------------------------------------------------
# Threshold edge cases
# ---------------------------------------------------------------------------


class TestThresholdEdgeCases:
    def test_at_threshold_is_included(
        self, client: TestClient, db_session: Session
    ) -> None:
        """``current_qty == reorder_threshold`` is the trigger point."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="EQ",
            current_qty=Decimal("10"),
            threshold=Decimal("10"),
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert 'data-testid="reorder-row"' in resp.text
        # Slice to the row block.
        row_idx = resp.text.find('data-testid="reorder-row"')
        row = resp.text[row_idx : row_idx + 1500]
        assert 'data-testid="reorder-deficit">0' in row

    def test_strictly_above_threshold_is_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="OK",
            current_qty=Decimal("11"),
            threshold=Decimal("10"),
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert 'data-testid="reorder-row"' not in resp.text
        assert 'data-testid="reorder-empty"' in resp.text


# ---------------------------------------------------------------------------
# Archived filtering
# ---------------------------------------------------------------------------


class TestArchivedFiltering:
    def test_archived_item_excluded_even_when_below_threshold(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="GONE",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            archived=True,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert "GONE" not in resp.text
        assert 'data-testid="reorder-empty"' in resp.text

    def test_unarchiving_restores_to_dashboard(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="BACK",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            archived=True,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert "BACK" not in resp.text
        # Unarchive in-place.
        item.archived_at = None
        db_session.commit()
        resp = client.get("/admin/reorder")
        assert "BACK" in resp.text


# ---------------------------------------------------------------------------
# Archived supplier surfacing
# ---------------------------------------------------------------------------


class TestArchivedSupplierSurfacing:
    def test_archived_supplier_label_is_suffixed(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="Old Co", archived=True)
        _make_item(
            db_session,
            leaf=leaf,
            sku="OLD-1",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert "Old Co (archived)" in resp.text
        assert 'data-supplier-archived="true"' in resp.text

    def test_clearing_supplier_drops_into_no_supplier_bucket(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        sup = _make_supplier(db_session, name="Bullion Co")
        item = _make_item(
            db_session,
            leaf=leaf,
            sku="MOVED",
            current_qty=Decimal("0"),
            threshold=Decimal("5"),
            supplier=sup,
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert "Bullion Co" in resp.text
        # Now clear the supplier on the item.
        item.supplier_id = None
        db_session.commit()
        resp = client.get("/admin/reorder")
        assert 'data-supplier-id="none"' in resp.text
        # Bullion Co (the supplier) still exists in the DB but should not
        # render now that no items are bucketed under it.
        assert "Bullion Co" not in resp.text


# ---------------------------------------------------------------------------
# Zero threshold
# ---------------------------------------------------------------------------


class TestZeroThreshold:
    def test_zero_threshold_zero_qty_is_included(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Zero threshold + zero stock is genuinely "at threshold"."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="ZZ-1",
            current_qty=Decimal("0"),
            threshold=Decimal("0"),
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert 'data-testid="reorder-row"' in resp.text
        row_idx = resp.text.find('data-testid="reorder-row"')
        row = resp.text[row_idx : row_idx + 1500]
        assert 'data-testid="reorder-deficit">0' in row

    def test_zero_threshold_positive_qty_is_excluded(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        leaf = _make_leaf(db_session)
        _make_item(
            db_session,
            leaf=leaf,
            sku="ZZ-2",
            current_qty=Decimal("1"),
            threshold=Decimal("0"),
        )
        _login_as(client, mgr)
        resp = client.get("/admin/reorder")
        assert "ZZ-2" not in resp.text
        assert 'data-testid="reorder-empty"' in resp.text
