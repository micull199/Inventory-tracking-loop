"""Download → re-upload round-trip pinning tests.

CSV-uploads spec promise: "The upload format mirrors the download exactly
(1:1 column match)". These tests download a non-empty CSV from each list
view, immediately re-upload it as-is, and assert:

- The header check passes (no missing required column, no unknown column).
- Every row is tagged ``skip`` (each existing row's id matches an existing
  database row).
- No new rows are created, no audit ``*.created`` rows are written.

A regression that breaks the column contract (download adds a header the
upload doesn't accept, or upload requires a header the download doesn't
emit) fails *one* of these tests with a clear message.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Archetype,
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


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role,
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


def _download(client: TestClient, url: str) -> bytes:
    resp = client.get(url)
    assert resp.status_code == 200, (url, resp.status_code)
    return resp.content


def _reupload(client: TestClient, url: str, csv_bytes: bytes) -> object:
    return client.post(
        url,
        files={"file": ("download.csv", csv_bytes, "text/csv")},
        data={"csrf_token": _csrf(client), "dry_run": "1"},
        follow_redirects=False,
    )


def _assert_clean_skip_roundtrip(resp: object, expected_data_rows: int) -> None:
    """Common assertions: no top-level error, all rows tagged ``skip``."""
    body = resp.content  # type: ignore[attr-defined]
    assert resp.status_code == 200, body[:500]  # type: ignore[attr-defined]
    assert b"csv-upload-top-error" not in body, body[:500]
    assert (
        body.count(b'data-row-tag="skip"') == expected_data_rows
    ), (expected_data_rows, body[:1000])
    assert b'data-row-tag="error"' not in body, body[:1000]
    assert b'data-row-tag="new"' not in body, body[:1000]


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------


def test_suppliers_download_roundtrips(
    client: TestClient, db_session: Session
) -> None:
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    db_session.add_all(
        [
            Supplier(name="Acme Wax", email="orders@acme.test", phone="0123", notes="trade"),
            Supplier(name="Beta Metal"),
        ]
    )
    db_session.commit()
    _login_as(client, mgr)
    csv = _download(client, "/admin/suppliers?format=csv&show=active")
    resp = _reupload(client, "/admin/suppliers/upload", csv)
    _assert_clean_skip_roundtrip(resp, expected_data_rows=2)


# ---------------------------------------------------------------------------
# Locations
# ---------------------------------------------------------------------------


def test_locations_download_roundtrips(
    client: TestClient, db_session: Session
) -> None:
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    db_session.add_all([Location(name="Bench A", notes="front"), Location(name="Safe")])
    db_session.commit()
    _login_as(client, mgr)
    csv = _download(client, "/admin/locations?format=csv&show=active")
    resp = _reupload(client, "/admin/locations/upload", csv)
    _assert_clean_skip_roundtrip(resp, expected_data_rows=2)


# ---------------------------------------------------------------------------
# Taxonomy — top-level
# ---------------------------------------------------------------------------


def test_taxonomy_top_download_roundtrips(
    client: TestClient, db_session: Session
) -> None:
    """Download has to carry ``archetype`` for the upload to accept the
    header. This test fails if the download regresses to the pre-fix
    ``id,sort_order,name`` shape.
    """
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    db_session.add_all(
        [
            TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK),
            TaxonomyNode(name="Chains", sku_prefix="CHN", archetype=Archetype.UNIQUE),
        ]
    )
    db_session.commit()
    _login_as(client, mgr)
    csv = _download(client, "/admin/taxonomy?format=csv&show=active")
    # Sanity: the header carries the upload-required ``archetype``.
    assert csv.split(b"\r\n", 1)[0] == b"id,sort_order,name,sku_prefix,archetype"
    resp = _reupload(client, "/admin/taxonomy/upload", csv)
    _assert_clean_skip_roundtrip(resp, expected_data_rows=2)


# ---------------------------------------------------------------------------
# Taxonomy — sub-categories
# ---------------------------------------------------------------------------


def test_taxonomy_subcategories_download_roundtrips(
    client: TestClient, db_session: Session
) -> None:
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    parent = TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
    db_session.add(parent)
    db_session.commit()
    db_session.refresh(parent)
    db_session.add_all(
        [
            TaxonomyNode(parent_id=parent.id, name="Silver", sku_prefix="SLV"),
            TaxonomyNode(parent_id=parent.id, name="Gold", sku_prefix="GLD"),
        ]
    )
    db_session.commit()
    _login_as(client, mgr)
    csv = _download(client, f"/admin/taxonomy/{parent.id}/children?format=csv&show=active")
    resp = _reupload(client, f"/admin/taxonomy/{parent.id}/children/upload", csv)
    _assert_clean_skip_roundtrip(resp, expected_data_rows=2)


# ---------------------------------------------------------------------------
# Taxonomy — grandchildren
# ---------------------------------------------------------------------------


def test_taxonomy_grandchildren_download_roundtrips(
    client: TestClient, db_session: Session
) -> None:
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    parent = TaxonomyNode(name="Rings", sku_prefix="RNG", archetype=Archetype.BULK)
    db_session.add(parent)
    db_session.commit()
    db_session.refresh(parent)
    sub = TaxonomyNode(parent_id=parent.id, name="Silver", sku_prefix="SLV")
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)
    db_session.add_all(
        [
            TaxonomyNode(parent_id=sub.id, name="925", sku_prefix="925"),
            TaxonomyNode(parent_id=sub.id, name="950", sku_prefix="950"),
        ]
    )
    db_session.commit()
    _login_as(client, mgr)
    url = f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren?format=csv&show=active"
    csv = _download(client, url)
    resp = _reupload(
        client,
        f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren/upload",
        csv,
    )
    _assert_clean_skip_roundtrip(resp, expected_data_rows=2)


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def test_items_download_roundtrips_depth0(
    client: TestClient, db_session: Session
) -> None:
    """Depth-0 leaf: the category cell carries the leaf name ("Tools")."""
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    leaf = TaxonomyNode(name="Tools", archetype=Archetype.BULK, sku_prefix="TOOL")
    db_session.add(leaf)
    db_session.commit()
    db_session.refresh(leaf)
    db_session.add(
        Item(
            sku="TOOL-A",
            name="Hammer",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.QTY,
            current_qty=Decimal("3"),
            reorder_threshold=Decimal("1"),
            reorder_qty=Decimal("5"),
        )
    )
    db_session.commit()
    _login_as(client, mgr)
    csv = _download(client, "/admin/items?format=csv&show=active")
    resp = _reupload(client, "/admin/items/upload", csv)
    _assert_clean_skip_roundtrip(resp, expected_data_rows=1)


def test_items_download_roundtrips_depth2(
    client: TestClient, db_session: Session
) -> None:
    """Depth-2 leaf: the category cell must carry the full 3-segment path
    (``Top / Sub / Leaf``) — not the legacy 2-segment ``_category_label``.
    This is the test that catches the truncation bug.
    """
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    top = TaxonomyNode(name="Raw Materials", archetype=Archetype.BULK, sku_prefix="RM")
    db_session.add(top)
    db_session.commit()
    sub = TaxonomyNode(parent_id=top.id, name="Silver", sku_prefix="SLV")
    db_session.add(sub)
    db_session.commit()
    leaf = TaxonomyNode(parent_id=sub.id, name="925", sku_prefix="925")
    db_session.add(leaf)
    db_session.commit()
    db_session.refresh(leaf)
    db_session.add(
        Item(
            sku="RM-SLV-925-0001",
            name="Wire",
            taxonomy_node_id=leaf.id,
            unit="g",
            tracking_mode=TrackingMode.QTY,
        )
    )
    db_session.commit()
    _login_as(client, mgr)
    csv = _download(client, "/admin/items?format=csv&show=active")
    # Sanity: the category cell carries the full path, not just "Silver / 925".
    assert b"Raw Materials / Silver / 925" in csv, csv
    resp = _reupload(client, "/admin/items/upload", csv)
    _assert_clean_skip_roundtrip(resp, expected_data_rows=1)


def test_no_creates_after_roundtrip(
    client: TestClient, db_session: Session
) -> None:
    """No ``*.created`` audit rows after re-uploading the download.

    A regression that flips a skip to a "new" would silently create
    duplicates; this test surfaces it via the audit trail.
    """
    mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
    db_session.add(Supplier(name="Acme"))
    db_session.add(Location(name="Bench A"))
    db_session.commit()
    _login_as(client, mgr)

    # Suppliers
    csv = _download(client, "/admin/suppliers?format=csv&show=active")
    _reupload(client, "/admin/suppliers/upload", csv)
    # Locations
    csv = _download(client, "/admin/locations?format=csv&show=active")
    _reupload(client, "/admin/locations/upload", csv)

    created_actions = [
        a
        for (a,) in db_session.execute(
            select(AuditLog.action).where(AuditLog.action.like("%.created"))
        ).all()
    ]
    assert created_actions == [], created_actions
