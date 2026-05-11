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


def _make_leaf(
    db: Session, name: str = "Raw Materials", sku_prefix: str | None = None
) -> TaxonomyNode:
    # Sibling leaves built from similar names (e.g. ``Cat-A`` and ``Cat-B``)
    # collide on the partial unique index on ``taxonomy_nodes(sku_prefix)``
    # because the name-derived default truncates to ``CAT``. Callers pass an
    # explicit ``sku_prefix`` to dodge.
    kwargs: dict[str, object] = {"name": name}
    if sku_prefix is not None:
        kwargs["sku_prefix"] = sku_prefix
    n = TaxonomyNode(**kwargs)
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
    _alnum = "".join(c for c in sku if c.isalnum())[:8] or "TST"
    leaf = _make_leaf(db, name=f"Cat-{sku}", sku_prefix=_alnum)
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


def _scan_camera_script_block(body: str) -> str:
    """Return the body of the inline ``data-testid="scan-camera-script"``
    block (the SC2b glue IIFE). Tests assert against substring markers
    inside this block — TestClient isn't a browser so we can't execute
    the JS, but we can pin the source contents.
    """
    marker = body.find('data-testid="scan-camera-script"')
    assert marker >= 0, "scan-camera-script block not found in response"
    end = body.find("</script>", marker)
    assert end > marker, "closing </script> not found after scan-camera-script"
    return body[marker:end]


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

    def test_renders_three_inline_action_forms(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC1c: action links are now inline forms posting to the existing
        movement routes. Each form has a submit button + the right action."""
        item = _make_item(db_session, sku="LINK-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-out-form"' in resp.text
        assert f'action="/admin/items/{item.id}/out"' in resp.text
        assert 'data-testid="scan-out-submit"' in resp.text
        assert 'data-testid="scan-in-form"' in resp.text
        assert f'action="/admin/items/{item.id}/in"' in resp.text
        assert 'data-testid="scan-in-submit"' in resp.text
        assert 'data-testid="scan-adjust-form"' in resp.text
        assert f'action="/admin/items/{item.id}/adjust"' in resp.text
        assert 'data-testid="scan-adjust-submit"' in resp.text

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
        the inline forms would 400 on submit. SC1c omits the forms entirely
        and renders the archived note instead. The check-out link is also
        omitted on archived items (no posture change from SC1b)."""
        item = _make_item(db_session, sku="AR-2", archived=True)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert 'data-testid="scan-out-form"' not in resp.text
        assert 'data-testid="scan-in-form"' not in resp.text
        assert 'data-testid="scan-adjust-form"' not in resp.text
        assert 'data-testid="scan-out-submit"' not in resp.text
        assert 'data-testid="scan-in-submit"' not in resp.text
        assert 'data-testid="scan-adjust-submit"' not in resp.text
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
        # SC1c: the action picker is now three inline forms, not links.
        assert 'data-testid="scan-out-form"' in resp.text


# ---------------------------------------------------------------------------
# SC1c: inline action forms — shape (action / method / CSRF)
# ---------------------------------------------------------------------------


class TestScanItemPageInlineForms:
    def test_stock_out_form_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="OF-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        marker = resp.text.find('data-testid="scan-out-form"')
        assert marker >= 0
        block = resp.text[marker : marker + 800]
        assert 'method="post"' in block
        assert f'action="/admin/items/{item.id}/out"' in block
        assert 'name="csrf_token"' in block

    def test_stock_in_form_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="IF-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        marker = resp.text.find('data-testid="scan-in-form"')
        assert marker >= 0
        block = resp.text[marker : marker + 1200]
        assert 'method="post"' in block
        assert f'action="/admin/items/{item.id}/in"' in block
        assert 'name="csrf_token"' in block

    def test_adjust_form_shape(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="AF-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        marker = resp.text.find('data-testid="scan-adjust-form"')
        assert marker >= 0
        block = resp.text[marker : marker + 2000]
        assert 'method="post"' in block
        assert f'action="/admin/items/{item.id}/adjust"' in block
        assert 'name="csrf_token"' in block


# ---------------------------------------------------------------------------
# SC1c: inline action forms — fields visible on each form
# ---------------------------------------------------------------------------


class TestScanItemPageInlineFormFields:
    def test_stock_out_form_has_qty_input_only(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Out form has the qty input + submit. No unit_cost (consumption is
        per-layer FIFO), no reason (optional in the route — kept off the
        scan-flow surface to keep the hot-path tight)."""
        item = _make_item(db_session, sku="OF-2", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        out_marker = resp.text.find('data-testid="scan-out-form"')
        in_marker = resp.text.find('data-testid="scan-in-form"')
        assert out_marker >= 0
        assert in_marker > out_marker
        out_block = resp.text[out_marker:in_marker]
        assert 'data-testid="scan-out-qty-input"' in out_block
        assert 'data-testid="scan-out-submit"' in out_block
        # Out form should not carry unit_cost / reason / direction inputs.
        assert "scan-out-unit-cost" not in out_block
        assert "scan-out-reason" not in out_block
        assert "scan-out-direction" not in out_block

    def test_stock_in_form_has_qty_and_unit_cost(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="IF-2", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        in_marker = resp.text.find('data-testid="scan-in-form"')
        adj_marker = resp.text.find('data-testid="scan-adjust-form"')
        assert in_marker >= 0
        assert adj_marker > in_marker
        in_block = resp.text[in_marker:adj_marker]
        assert 'data-testid="scan-in-qty-input"' in in_block
        assert 'data-testid="scan-in-unit-cost-input"' in in_block
        assert 'data-testid="scan-in-submit"' in in_block
        # In form should not carry direction / reason inputs.
        assert "scan-in-direction" not in in_block
        assert "scan-in-reason" not in in_block

    def test_adjust_form_has_direction_qty_unit_cost_reason(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="AF-2", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        adj_marker = resp.text.find('data-testid="scan-adjust-form"')
        assert adj_marker >= 0
        block = resp.text[adj_marker:]
        assert 'data-testid="scan-adjust-direction-input"' in block
        assert 'data-testid="scan-adjust-qty-input"' in block
        assert 'data-testid="scan-adjust-unit-cost-input"' in block
        assert 'data-testid="scan-adjust-reason-input"' in block
        assert 'data-testid="scan-adjust-submit"' in block

    def test_adjust_direction_select_offers_increase_and_decrease(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="AF-3", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        marker = resp.text.find('data-testid="scan-adjust-direction-input"')
        assert marker >= 0
        select_end = resp.text.find("</select>", marker)
        assert select_end > marker
        select_block = resp.text[marker:select_end]
        assert '<option value="increase">' in select_block
        assert '<option value="decrease">' in select_block

    def test_required_attributes_on_qty_inputs(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC1c: qty inputs on all three forms are server-side required and
        client-side flagged with `required` so the browser blocks empty
        submits before they hit the route's 400 path."""
        item = _make_item(db_session, sku="REQ-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        for testid in (
            "scan-out-qty-input",
            "scan-in-qty-input",
            "scan-adjust-qty-input",
        ):
            marker = resp.text.find(f'data-testid="{testid}"')
            assert marker >= 0
            tag_start = resp.text.rfind("<input", 0, marker)
            tag_end = resp.text.find(">", marker)
            assert tag_start >= 0
            assert tag_end > tag_start
            tag = resp.text[tag_start : tag_end + 1]
            assert "required" in tag, f"{testid} missing required"

    def test_adjust_reason_input_is_required(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Reason is required server-side on adjust (variance attribution).
        The inline form must mark the input as required so the browser
        blocks empty submits."""
        item = _make_item(db_session, sku="REQ-2", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        marker = resp.text.find('data-testid="scan-adjust-reason-input"')
        assert marker >= 0
        tag_start = resp.text.rfind("<input", 0, marker)
        tag_end = resp.text.find(">", marker)
        assert tag_start >= 0
        assert tag_end > tag_start
        tag = resp.text[tag_start : tag_end + 1]
        assert "required" in tag

    def test_each_inline_form_carries_hidden_next_input(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC1d: each of the three inline forms includes a hidden
        `<input name="next" value="/scan/item/{id}">` so a successful submit
        redirects back into scan flow instead of landing on the per-action
        form. The qty inputs and submit buttons stay unchanged."""
        item = _make_item(db_session, sku="NEXT-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        out_marker = resp.text.find('data-testid="scan-out-form"')
        in_marker = resp.text.find('data-testid="scan-in-form"')
        adj_marker = resp.text.find('data-testid="scan-adjust-form"')
        assert out_marker >= 0
        assert in_marker > out_marker
        assert adj_marker > in_marker
        out_block = resp.text[out_marker:in_marker]
        in_block = resp.text[in_marker:adj_marker]
        adj_block = resp.text[adj_marker:]
        expected_value = f'value="/scan/item/{item.id}"'
        for block_name, block in (
            ("out", out_block),
            ("in", in_block),
            ("adjust", adj_block),
        ):
            assert 'name="next"' in block, (
                f"{block_name} form missing hidden next input"
            )
            assert expected_value in block, (
                f"{block_name} form's next value is wrong"
            )


# ---------------------------------------------------------------------------
# SC1c: inline action forms — end-to-end submission goes through to the
# existing /admin/items/{id}/{in,out,adjust} routes (so the engine wires up).
# ---------------------------------------------------------------------------


class TestScanItemPageInlineFormSubmission:
    def test_workshop_submits_stock_in_via_inline_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Posting the inline stock-in form lands the engine: cost layer
        created, current_qty bumped, audit row written, 303 redirect."""
        item = _make_item(db_session, sku="SUB-IN-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        before = _audit_count(db_session)
        resp = client.post(
            f"/admin/items/{item.id}/in",
            data={
                "qty": "5",
                "unit_cost": "1.50",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/in"
        after = _audit_count(db_session)
        assert after == before + 1
        db_session.expire_all()
        refreshed = db_session.get(Item, item.id)
        assert refreshed is not None
        # Engine bumped current_qty from 0 by 5.
        from decimal import Decimal

        assert refreshed.current_qty == Decimal("5.0000")

    def test_workshop_submits_stock_out_via_inline_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Posting the inline stock-out form consumes a cost layer FIFO."""
        from decimal import Decimal

        item = _make_item(db_session, sku="SUB-OUT-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        # Seed a layer first via the existing stock-in route so the out has
        # something to consume.
        resp_in = client.post(
            f"/admin/items/{item.id}/in",
            data={
                "qty": "10",
                "unit_cost": "2.00",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp_in.status_code == 303
        before = _audit_count(db_session)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data={"qty": "3", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/out"
        after = _audit_count(db_session)
        assert after == before + 1
        db_session.expire_all()
        refreshed = db_session.get(Item, item.id)
        assert refreshed is not None
        assert refreshed.current_qty == Decimal("7.0000")

    def test_workshop_submits_adjust_increase_via_inline_form(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Adjust direction=increase + unit_cost + reason creates a layer
        and bumps current_qty (positive-adjustment path)."""
        from decimal import Decimal

        item = _make_item(db_session, sku="SUB-ADJ-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        before = _audit_count(db_session)
        resp = client.post(
            f"/admin/items/{item.id}/adjust",
            data={
                "direction": "increase",
                "qty": "4",
                "unit_cost": "3.25",
                "reason": "found in storage",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/admin/items/{item.id}/adjust"
        after = _audit_count(db_session)
        assert after == before + 1
        db_session.expire_all()
        refreshed = db_session.get(Item, item.id)
        assert refreshed is not None
        assert refreshed.current_qty == Decimal("4.0000")

    def test_inline_form_reaches_route_validation_path(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Empty qty (which the browser would block client-side via
        `required`, but we test the server's 400 path directly here)
        still returns 400 from the existing route — the inline form is
        a thin posting surface, not its own validation layer."""
        item = _make_item(db_session, sku="VAL-1", qr_code=None)
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        token = _csrf(client)
        resp = client.post(
            f"/admin/items/{item.id}/out",
            data={"qty": "", "csrf_token": token},
            follow_redirects=False,
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# SC2a: camera surface scaffolding + feature-detect + graceful degradation
# ---------------------------------------------------------------------------


class TestScanCameraSurface:
    """Pin the server-rendered scaffolding that SC2b's camera library will
    hook into. SC2a renders a hidden ``<button data-testid="scan-camera-toggle">``
    + a hidden ``<section data-testid="scan-camera-surface">`` + an inline
    feature-detect script. The button only becomes visible (in a real
    browser) when ``navigator.mediaDevices.getUserMedia`` is available;
    SC2a's tests pin the *server-rendered* HTML, not the JS behaviour
    (which has no scan output yet — SC2b will add it).
    """

    def test_camera_block_rendered_on_scan_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        assert resp.status_code == 200
        assert 'data-testid="scan-camera"' in resp.text
        # Camera disclosure sits between the keyboard form and any resolved
        # item: pinned by relative ordering of the markers.
        form_pos = resp.text.find('data-testid="scan-form"')
        camera_pos = resp.text.find('data-testid="scan-camera"')
        assert form_pos >= 0
        assert camera_pos > form_pos

    def test_camera_block_rendered_on_scan_item_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        item = _make_item(db_session, sku="CAM-1", qr_code="CAM-Q1")
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 200
        assert 'data-testid="scan-camera"' in resp.text
        # On a resolved-item page the camera disclosure sits *above* the
        # ``scan-resolved-item`` block, so a user can scan the next code
        # without scrolling past the action picker.
        camera_pos = resp.text.find('data-testid="scan-camera"')
        resolved_pos = resp.text.find('data-testid="scan-resolved-item"')
        assert camera_pos >= 0
        assert resolved_pos >= 0
        assert camera_pos < resolved_pos

    def test_camera_toggle_button_starts_hidden(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The toggle button renders with the ``hidden`` HTML attribute so
        no-JS / no-camera devices don't see a "Use camera" affordance they
        can't action. Inline JS removes ``hidden`` only when the browser
        supports ``getUserMedia``.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-toggle"')
        assert marker >= 0
        tag_start = resp.text.rfind("<button", 0, marker)
        tag_end = resp.text.find(">", marker)
        assert tag_start >= 0
        assert tag_end > tag_start
        tag = resp.text[tag_start : tag_end + 1]
        assert " hidden" in tag or tag.endswith("hidden>")

    def test_camera_surface_starts_hidden(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-surface"')
        assert marker >= 0
        tag_start = resp.text.rfind("<section", 0, marker)
        tag_end = resp.text.find(">", marker)
        assert tag_start >= 0
        assert tag_end > tag_start
        tag = resp.text[tag_start : tag_end + 1]
        assert " hidden" in tag or tag.endswith("hidden>")

    def test_camera_toggle_has_aria_expanded_false(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-toggle"')
        assert marker >= 0
        tag_start = resp.text.rfind("<button", 0, marker)
        tag_end = resp.text.find(">", marker)
        tag = resp.text[tag_start : tag_end + 1]
        assert 'aria-expanded="false"' in tag

    def test_camera_toggle_aria_controls_scan_camera_surface(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Button declares it controls the surface via ``aria-controls``;
        the surface element exposes the matching ``id`` so the relationship
        resolves in screen readers.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        # Button: aria-controls
        btn_marker = resp.text.find('data-testid="scan-camera-toggle"')
        btn_start = resp.text.rfind("<button", 0, btn_marker)
        btn_end = resp.text.find(">", btn_marker)
        btn_tag = resp.text[btn_start : btn_end + 1]
        assert 'aria-controls="scan-camera-surface"' in btn_tag
        # Surface: id matches.
        sec_marker = resp.text.find('data-testid="scan-camera-surface"')
        sec_start = resp.text.rfind("<section", 0, sec_marker)
        sec_end = resp.text.find(">", sec_marker)
        sec_tag = resp.text[sec_start : sec_end + 1]
        assert 'id="scan-camera-surface"' in sec_tag

    def test_camera_status_region_has_aria_live_polite(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Status region carries ``aria-live="polite"`` so SC2b's
        ``getUserMedia`` progress / error messages reach screen-reader
        users without interrupting other content.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-status"')
        assert marker >= 0
        tag_start = resp.text.rfind("<p", 0, marker)
        tag_end = resp.text.find(">", marker)
        tag = resp.text[tag_start : tag_end + 1]
        assert 'aria-live="polite"' in tag

    def test_camera_viewfinder_div_present(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC2b will inject the html5-qrcode viewfinder into this
        container; SC2a renders it empty as a placeholder hook.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        assert 'data-testid="scan-camera-viewfinder"' in resp.text

    def test_inline_camera_script_present(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The inline feature-detect script is server-rendered and
        contains the ``navigator.mediaDevices.getUserMedia`` check (the
        guard that keeps the toggle button hidden on devices without
        camera support) and an ``addEventListener('click', ...)`` call
        (the toggle wiring).
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-script"')
        assert marker >= 0
        # Walk to the closing </script> for the script block body.
        block_end = resp.text.find("</script>", marker)
        assert block_end > marker
        block = resp.text[marker:block_end]
        assert "navigator.mediaDevices" in block
        assert "getUserMedia" in block
        assert "addEventListener" in block

    def test_html5_qrcode_lib_loaded_with_sri(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC2b loads ``html5-qrcode`` from jsDelivr with an SRI integrity
        hash + ``crossorigin="anonymous"`` so a CDN compromise can't ship a
        malicious payload. SRI hash was computed locally against the live
        jsDelivr file (``2.3.8``, 375364 bytes) using
        ``openssl dgst -sha384 -binary | openssl base64 -A``.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-lib"')
        assert marker >= 0
        tag_start = resp.text.rfind("<script", 0, marker)
        tag_end = resp.text.find(">", marker)
        tag = resp.text[tag_start : tag_end + 1]
        assert "src=" in tag
        assert "html5-qrcode" in tag
        assert (
            'integrity="sha384-c9d8RFSL+u3exBOJ4Yp3HUJXS4znl9f+'
            'z66d1y54ig+ea249SpqR+w1wyvXz/lk+"'
        ) in tag
        assert 'crossorigin="anonymous"' in tag

    def test_html5_qrcode_lib_pinned_to_2_3_8(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The CDN URL pins ``@2.3.8`` so a future floating-version edit
        (e.g. ``@latest`` or no version) is caught — SRI only protects
        against payload tampering, not against accidentally bumping the
        URL without re-pinning the hash.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-lib"')
        assert marker >= 0
        tag_start = resp.text.rfind("<script", 0, marker)
        tag_end = resp.text.find(">", marker)
        tag = resp.text[tag_start : tag_end + 1]
        assert "html5-qrcode@2.3.8" in tag

    def test_no_other_scanning_libs_loaded(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC2b picked html5-qrcode; this test pins that we did *not*
        also accidentally pull in jsQR / zxing / qrcode-scanner via a
        copy-paste from a how-to.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        body = resp.text.lower()
        assert "qrcode-scanner" not in body
        assert "jsqr" not in body
        assert "zxing" not in body

    def test_lib_script_loads_before_glue_script(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The library script tag must appear earlier in the response than
        the inline glue script so ``Html5Qrcode`` is defined globally by
        the time the IIFE runs (synchronous-load contract).
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        lib_pos = resp.text.find('data-testid="scan-camera-lib"')
        glue_pos = resp.text.find('data-testid="scan-camera-script"')
        assert lib_pos >= 0
        assert glue_pos >= 0
        assert lib_pos < glue_pos

    def test_viewfinder_div_has_id_for_html5_qrcode(
        self, client: TestClient, db_session: Session
    ) -> None:
        """``Html5Qrcode``'s constructor takes a string element id and uses
        ``document.getElementById`` internally — so the viewfinder div
        needs an ``id``, not just a ``data-testid``.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        marker = resp.text.find('data-testid="scan-camera-viewfinder"')
        assert marker >= 0
        tag_start = resp.text.rfind("<div", 0, marker)
        tag_end = resp.text.find(">", marker)
        tag = resp.text[tag_start : tag_end + 1]
        assert 'id="scan-camera-viewfinder"' in tag

    def test_inline_glue_calls_html5qrcode_constructor(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "new Html5Qrcode(" in block

    def test_inline_glue_uses_environment_facing_mode(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Back camera (``facingMode: "environment"``) is the workshop
        hot-path: scan a label sitting on a workbench from a phone or
        tablet. No camera-select UI in v1.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert 'facingMode: "environment"' in block

    def test_inline_glue_writes_decoded_to_input(
        self, client: TestClient, db_session: Session
    ) -> None:
        """On a successful decode, the IIFE writes the decoded value into
        the keyboard ``<input name="code">`` (so the existing
        ``/scan/resolve`` route handles the lookup — no separate
        camera-resolve endpoint).
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "input.value =" in block

    def test_inline_glue_submits_form_on_decode(
        self, client: TestClient, db_session: Session
    ) -> None:
        """After writing the decoded value, the IIFE submits the scan
        form so the resolve happens automatically (matches USB-scanner
        behaviour where Enter is the scanner's terminator — DoD #3's
        two-interaction goal).
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "form.submit(" in block

    def test_inline_glue_handles_permission_denial(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The permission-denial branch writes a plain-English message
        that points the user at the keyboard fallback. The full sentence
        is split across a JS ``+`` concatenation in source for line
        length, so the test asserts each half independently — both must
        appear inside the same script block.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "Camera permission denied" in block
        assert "Use the keyboard input above to type a code instead" in block

    def test_inline_glue_handles_no_camera(
        self, client: TestClient, db_session: Session
    ) -> None:
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "No camera detected on this device" in block
        assert "Use the keyboard input above to type a code instead" in block

    def test_inline_glue_stops_scanner_on_close_and_after_scan(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Calling ``scanner.stop()`` is the teardown path for both
        toggle-close *and* successful decode (don't leak the camera
        resource during full-page navigation). One marker covers both
        sites because both go through the same ``stopScanner`` helper.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "scanner.stop(" in block

    def test_inline_glue_preserves_existing_feature_detect(
        self, client: TestClient, db_session: Session
    ) -> None:
        """SC2a's feature-detect (``navigator.mediaDevices.getUserMedia``)
        still gates the "Use camera" button reveal — SC2b adds the start /
        stop wiring on top, doesn't replace the gate.
        """
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get("/scan")
        block = _scan_camera_script_block(resp.text)
        assert "navigator.mediaDevices" in block
        assert "getUserMedia" in block

    def test_camera_lib_present_on_scan_item_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        """The library + glue must be loaded on ``/scan/item/{id}`` too —
        a Workshop user finishes a movement on the resolved-item page and
        the next scan should be camera-driven without a manual nav back
        to ``/scan``.
        """
        item = _make_item(db_session, sku="CAM-2", qr_code="CAM-Q2")
        u = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, u)
        resp = client.get(f"/scan/item/{item.id}")
        assert resp.status_code == 200
        assert 'data-testid="scan-camera-lib"' in resp.text
        block = _scan_camera_script_block(resp.text)
        assert "new Html5Qrcode(" in block
