"""Integration tests for the Workshop-facing ``/scan`` routes (SC1a + SC1b).

Smallest end-to-end SC1 sub-slice: a focused-input scan landing page that
resolves a code (qr_code or sku exact match) to an item and 303-redirects to
the in-flow action-picker page (``/scan/item/{id}``) where Stock-in /
Stock-out / Adjust (and Check out for flagged items) links live. Subsequent
slices (SC1c, SC2) layer qty/cost entry inline and a camera fallback on top.

Coverage:
- Role enforcement on all three routes.
- Render shape on the scan landing page (heading, form action/method,
  autofocus input).
- Resolve by ``qr_code`` and by ``sku`` (qr_code wins on collision).
- Whitespace-trim before lookup.
- Empty code + no-match → 303 back to ``/scan`` with a flash.
- Archived item still resolves; the action-picker surfaces an archived
  badge + omits action buttons (since /in / /out / /adjust would 400).
- Action picker renders the three core action links + the optional
  Check out link for ``requires_checkout`` items.
- Read-only invariant: no route writes an audit row.
- Nav link visibility per role.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Item,
    Role,
    TaxonomyNode,
    TrackingMode,
    User,
    UserStatus,
)

# ---------------------------------------------------------------------------
# Fixtures (kept local — same shape as other route test modules)
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


def _make_leaf(db: Session, name: str = "Raw Materials") -> TaxonomyNode:
    n = TaxonomyNode(name=name)
    db.add(n)
    db.commit()
    db.refresh(n)
    return n


def _make_item(
    db: Session,
    *,
    sku: str,
    qr_code: str | None = None,
    name: str | None = None,
    archived: bool = False,
) -> Item:
    leaf = _make_leaf(db, name=f"Cat-{sku}")
    item = Item(
        sku=sku,
        name=name or f"Item {sku}",
        taxonomy_node_id=leaf.id,
        unit="g",
        tracking_mode=TrackingMode.QTY,
        qr_code=qr_code,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    if archived:
        from datetime import UTC, datetime

        item.archived_at = datetime.now(UTC)
        db.commit()
        db.refresh(item)
    return item


def _audit_count(db: Session) -> int:
    return len(list(db.execute(select(AuditLog)).scalars().all()))


# ---------------------------------------------------------------------------
# GET /scan: role enforcement
# ---------------------------------------------------------------------------


class TestScanGetRoleEnforcement:
    def test_anonymous_get_is_401(self, client: TestClient) -> None:
        resp = client.get("/scan")
        assert resp.status_code == 401

    def test_pending_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session, email="p@x.test", status=UserStatus.PENDING
        )
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 403

    def test_workshop_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 200

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 200

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 200

    def test_admin_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /scan: render shape
# ---------------------------------------------------------------------------


class TestScanGetRender:
    def test_renders_heading(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 200
        assert 'data-testid="scan-heading"' in resp.text

    def test_renders_form_with_post_action(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        assert 'data-testid="scan-form"' in resp.text
        assert 'action="/scan/resolve"' in resp.text
        assert 'method="post"' in resp.text

    def test_input_has_autofocus_and_name_code(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        # The single text input is autofocused and named ``code`` so a USB
        # scanner's first keystroke goes into the right place. Walk back from
        # the data-testid to the opening ``<input`` to grab the full tag.
        marker = resp.text.find('data-testid="scan-code-input"')
        assert marker >= 0
        tag_start = resp.text.rfind("<input", 0, marker)
        tag_end = resp.text.find(">", marker)
        assert tag_start >= 0
        assert tag_end > tag_start
        tag = resp.text[tag_start : tag_end + 1]
        assert "autofocus" in tag
        assert 'name="code"' in tag


# ---------------------------------------------------------------------------
# POST /scan/resolve: role enforcement
# ---------------------------------------------------------------------------


class TestScanResolveRoleEnforcement:
    def test_anonymous_post_is_401(self, client: TestClient) -> None:
        # Bootstrap a CSRF cookie first; anon CSRF-checks pass with the cookie
        # but auth blocks before role logic.
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "anything", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 401

    def test_pending_post_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session, email="p@x.test", status=UserStatus.PENDING
        )
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "anything", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_workshop_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item = _make_item(db_session, sku="W-1", qr_code="QR-W")
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "QR-W", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"

    def test_office_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        item = _make_item(db_session, sku="O-1", qr_code="QR-O")
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "QR-O", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"

    def test_manager_post_is_303(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        item = _make_item(db_session, sku="M-1", qr_code="QR-M")
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "QR-M", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"


# ---------------------------------------------------------------------------
# POST /scan/resolve: matching behaviour
# ---------------------------------------------------------------------------


class TestScanResolveMatching:
    def test_resolves_by_qr_code(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item = _make_item(db_session, sku="ALLOY-A", qr_code="QR-ALLOY")
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "QR-ALLOY", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"

    def test_resolves_by_sku_when_no_qr_match(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item = _make_item(db_session, sku="SKU-XYZ", qr_code=None)
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "SKU-XYZ", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"

    def test_qr_code_wins_over_sku_on_collision(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Two different items where one has sku=X and the other has qr=X.

        Precedence is qr_code-first. Pinned so a future refactor that swaps
        the lookup order is caught.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item_by_sku = _make_item(db_session, sku="X", qr_code=None)
        item_by_qr = _make_item(db_session, sku="OTHER", qr_code="X")
        assert item_by_sku.id != item_by_qr.id
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "X", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item_by_qr.id}"

    def test_resolves_after_trimming_whitespace(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item = _make_item(db_session, sku="TRIM-1", qr_code="QR-TRIM")
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "  QR-TRIM  ", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"

    def test_archived_item_still_resolves(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A scanner doesn't know an item was archived; the action picker
        page surfaces an archived badge + omits the action buttons."""
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        item = _make_item(
            db_session, sku="ARCH-1", qr_code="QR-ARCH", archived=True
        )
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "QR-ARCH", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/scan/item/{item.id}"


# ---------------------------------------------------------------------------
# POST /scan/resolve: empty + no-match
# ---------------------------------------------------------------------------


class TestScanResolveEmptyOrNoMatch:
    def test_empty_code_redirects_back_with_flash(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/scan"

        followed = client.get("/scan")
        assert 'data-testid="flash"' in followed.text

    def test_whitespace_only_code_redirects_back_with_flash(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "   ", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/scan"
        followed = client.get("/scan")
        assert 'data-testid="flash"' in followed.text

    def test_unknown_code_redirects_back_with_flash_including_code(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "GARBAGE-XYZ", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/scan"
        followed = client.get("/scan")
        assert 'data-testid="flash"' in followed.text
        # The flash echoes the offending code so the user sees what was
        # captured (helps spot scanner-emitted control characters).
        assert "GARBAGE-XYZ" in followed.text


# ---------------------------------------------------------------------------
# Read-only invariant
# ---------------------------------------------------------------------------


class TestScanReadOnly:
    def test_get_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        before = _audit_count(db_session)
        client.get("/scan")
        after = _audit_count(db_session)
        assert before == after

    def test_resolve_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _make_item(db_session, sku="N-1", qr_code="QR-N")
        _login_as(client, u)
        before = _audit_count(db_session)
        token = _csrf(client)
        client.post(
            "/scan/resolve",
            data={"code": "QR-N", "csrf_token": token},
            follow_redirects=False,
        )
        after = _audit_count(db_session)
        assert before == after


# ---------------------------------------------------------------------------
# Nav link visibility
# ---------------------------------------------------------------------------


class TestScanNav:
    def test_workshop_sees_scan_nav(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/")
        assert 'data-testid="nav-scan"' in resp.text

    def test_office_sees_scan_nav(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get("/")
        assert 'data-testid="nav-scan"' in resp.text

    def test_manager_sees_scan_nav(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get("/")
        assert 'data-testid="nav-scan"' in resp.text

    def test_admin_sees_scan_nav(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="a@x.test", role=Role.ADMIN)
        _login_as(client, u)
        resp = client.get("/")
        assert 'data-testid="nav-scan"' in resp.text

    def test_pending_sees_no_nav_at_all(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(
            db_session, email="p@x.test", status=UserStatus.PENDING
        )
        _login_as(client, u)
        resp = client.get("/")
        # base.html only renders the primary nav for active users — the scan
        # link rides that gate.
        assert 'data-testid="nav-scan"' not in resp.text

    def test_anonymous_sees_no_scan_nav(self, client: TestClient) -> None:
        resp = client.get("/")
        assert 'data-testid="nav-scan"' not in resp.text

    def test_scan_page_marks_nav_link_current(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        snippet = resp.text[
            resp.text.find('data-testid="nav-scan"') :
        ][:200]
        assert 'aria-current="page"' in snippet


# ---------------------------------------------------------------------------
# SC1b: GET /scan/item/{id} action picker — role enforcement
# ---------------------------------------------------------------------------


class TestScanItemPageRoleEnforcement:
    def test_anonymous_get_is_401(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="A-1", qr_code=None)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 401

    def test_pending_get_is_403(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="P-1", qr_code=None)
        u = _make_user(
            db_session, email="p@x.test", status=UserStatus.PENDING
        )
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 403

    def test_workshop_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="W-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 200

    def test_office_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="O-1", qr_code=None)
        u = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 200

    def test_manager_get_is_200(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="M-1", qr_code=None)
        u = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# SC1b: GET /scan/item/{id} action picker — render shape
# ---------------------------------------------------------------------------


class TestScanItemPageRender:
    def test_renders_resolved_item_block(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="REND-1", name="Rendered Item")
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-resolved-item"' in resp.text
        assert f'data-item-id="{item.id}"' in resp.text
        # Identity surfaces sku + name.
        assert "REND-1" in resp.text
        assert "Rendered Item" in resp.text

    def test_renders_three_action_links_with_correct_hrefs(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="LINK-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert f'href="/admin/items/{item.id}/in"' in resp.text
        assert 'data-testid="scan-action-in"' in resp.text
        assert f'href="/admin/items/{item.id}/out"' in resp.text
        assert 'data-testid="scan-action-out"' in resp.text
        assert f'href="/admin/items/{item.id}/adjust"' in resp.text
        assert 'data-testid="scan-action-adjust"' in resp.text

    def test_scan_input_still_rendered_so_next_scan_works(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The action-picker page keeps the scan input + autofocus so a
        USB scanner can drive a fresh resolve without manual nav."""
        item = _make_item(db_session, sku="NEXT-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-form"' in resp.text
        assert 'data-testid="scan-code-input"' in resp.text
        marker = resp.text.find('data-testid="scan-code-input"')
        tag_start = resp.text.rfind("<input", 0, marker)
        tag_end = resp.text.find(">", marker)
        assert tag_start >= 0
        assert tag_end > tag_start
        tag = resp.text[tag_start : tag_end + 1]
        assert "autofocus" in tag


# ---------------------------------------------------------------------------
# SC1b: requires_checkout flag drives the fourth action link
# ---------------------------------------------------------------------------


class TestScanItemPageRequiresCheckout:
    def test_flagged_item_shows_checkout_action(
        self, client: TestClient, db_session: Session
    ) -> None:
        leaf = _make_leaf(db_session, name="Tools-RC")
        item = Item(
            sku="TOOL-1",
            name="Drill",
            taxonomy_node_id=leaf.id,
            unit="ea",
            tracking_mode=TrackingMode.QTY,
            requires_checkout=True,
        )
        db_session.add(item)
        db_session.commit()
        db_session.refresh(item)

        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-action-checkout"' in resp.text
        assert f'href="/admin/items/{item.id}/checkout"' in resp.text

    def test_non_flagged_item_omits_checkout_action(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="NOFLAG-1")
        # _make_item does not set requires_checkout, so it stays False.
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-action-checkout"' not in resp.text


# ---------------------------------------------------------------------------
# SC1b: archived items render the badge + omit the action buttons
# ---------------------------------------------------------------------------


class TestScanItemPageArchived:
    def test_archived_item_shows_badge_and_note(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="AR-1", archived=True)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-resolved-archived-badge"' in resp.text
        assert 'data-testid="scan-resolved-archived-note"' in resp.text

    def test_archived_item_omits_action_buttons(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The /in /out /adjust routes _reject_archived with HTTP 400, so
        linking from the action picker would be a dead-end. The page
        omits those buttons and instead renders the archived note."""
        item = _make_item(db_session, sku="AR-2", archived=True)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-action-in"' not in resp.text
        assert 'data-testid="scan-action-out"' not in resp.text
        assert 'data-testid="scan-action-adjust"' not in resp.text
        assert 'data-testid="scan-action-checkout"' not in resp.text

    def test_archived_item_still_renders_scan_input(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The user can still scan another item from the archived
        action-picker page — the scan input is part of the page header."""
        item = _make_item(db_session, sku="AR-3", archived=True)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-form"' in resp.text
        assert 'data-testid="scan-code-input"' in resp.text


# ---------------------------------------------------------------------------
# SC1b: 404 for unknown id, read-only invariant, full chain
# ---------------------------------------------------------------------------


class TestScanItemPageNotFound:
    def test_unknown_item_id_is_404(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan/item/999999")
        assert resp.status_code == 404


class TestScanItemPageReadOnly:
    def test_get_writes_no_audit(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="RO-1")
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        before = _audit_count(db_session)
        client.get(f"/scan/item/{item.id}")
        after = _audit_count(db_session)
        assert before == after


class TestScanItemPageResolveChain:
    def test_post_resolve_then_follow_renders_action_picker(
        self, client: TestClient, db_session: Session
    ) -> None:
        """End-to-end POST /scan/resolve → 303 → GET /scan/item/{id}."""
        item = _make_item(db_session, sku="CHAIN-1", qr_code="QR-CHAIN")
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            "/scan/resolve",
            data={"code": "QR-CHAIN", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert 'data-testid="scan-resolved-item"' in resp.text
        assert f'data-item-id="{item.id}"' in resp.text
        assert 'data-testid="scan-action-in"' in resp.text
