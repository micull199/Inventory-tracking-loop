"""Integration tests for the Workshop-facing ``/scan`` routes (SC1a).

Smallest end-to-end SC1 sub-slice: a focused-input scan landing page that
resolves a code (qr_code or sku exact match) to an item and 303-redirects to
the existing per-item edit page where in/out/adjust links live. Subsequent
slices (SC1b/c, SC2) layer the action picker, qty/cost entry, and camera
fallback on top.

Coverage:
- Role enforcement on both routes.
- Render shape (heading, form action/method, autofocus input).
- Resolve by ``qr_code`` and by ``sku`` (qr_code wins on collision).
- Whitespace-trim before lookup.
- Empty code + no-match → 303 back to ``/scan`` with a flash.
- Archived item still resolves (the destination page handles read-only state).
- Read-only invariant: neither route writes an audit row.
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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"

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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"

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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"


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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"

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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"

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
        assert (
            resp.headers["location"] == f"/admin/items/{item_by_qr.id}/edit"
        )

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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"

    def test_archived_item_still_resolves(
        self, client: TestClient, db_session: Session
    ) -> None:
        """A scanner doesn't know an item was archived; the destination edit
        page already handles the read-only archived presentation."""
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
        assert resp.headers["location"] == f"/admin/items/{item.id}/edit"


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
