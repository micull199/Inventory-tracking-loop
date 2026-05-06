"""Integration tests for the base layout: role-aware nav, accessibility, HTMX.

The layout has to:
- Show the role-appropriate primary nav (e.g. Users link only for admins).
- Mark the current page with ``aria-current="page"`` so assistive tech can
  announce it.
- Render a "skip to content" link for keyboard users.
- Load HTMX once globally (no duplicate <script> tags per page).
- Render the sign-out form (with CSRF) only when there's a current user.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import Role, User, UserStatus


def _make_user(
    db: Session,
    *,
    email: str,
    role: Role | None,
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
    client.post(
        "/auth/_dev-login",
        data={"email": user.email, "sub": user.google_sub},
        follow_redirects=False,
    )


class TestBaseLayoutAccessibility:
    def test_skip_link_is_first_focusable_element(self, client: TestClient) -> None:
        resp = client.get("/")
        assert resp.status_code == 200
        # The skip link must precede the header so it's the first tab stop.
        skip_idx = resp.text.find('class="skip-link"')
        header_idx = resp.text.find("<header")
        assert skip_idx > 0
        assert skip_idx < header_idx

    def test_skip_link_targets_main_content(self, client: TestClient) -> None:
        resp = client.get("/")
        assert 'href="#main"' in resp.text
        assert 'id="main"' in resp.text

    def test_htmx_script_is_loaded(self, client: TestClient) -> None:
        resp = client.get("/")
        assert "htmx.org" in resp.text

    def test_htmx_script_loaded_only_once(self, client: TestClient) -> None:
        resp = client.get("/")
        # Tolerate version bumps; just count the import.
        assert resp.text.count("unpkg.com/htmx.org") == 1


class TestRoleAwareNav:
    def test_anonymous_has_no_primary_nav(self, client: TestClient) -> None:
        resp = client.get("/")
        # Header is shown unconditionally, but the role-gated primary nav
        # should NOT render for an anonymous visitor.
        assert 'data-testid="primary-nav"' not in resp.text

    def test_pending_user_has_no_primary_nav(
        self, client: TestClient, db_session: Session
    ) -> None:
        # Pending = signed in but not yet activated. They get the holding page,
        # not the workshop nav.
        _login_as(
            client,
            _make_user(
                db_session,
                email="pending@x.test",
                role=None,
                status=UserStatus.PENDING,
            ),
        )
        resp = client.get("/")
        assert 'data-testid="primary-nav"' not in resp.text

    def test_workshop_nav_excludes_users_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        _login_as(
            client,
            _make_user(db_session, email="w@x.test", role=Role.WORKSHOP),
        )
        resp = client.get("/")
        assert 'data-testid="primary-nav"' in resp.text
        assert 'data-testid="nav-users"' not in resp.text

    def test_admin_nav_includes_users_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/")
        assert 'data-testid="primary-nav"' in resp.text
        assert 'data-testid="nav-users"' in resp.text
        assert 'href="/admin/users"' in resp.text

    def test_aria_current_set_on_active_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/admin/users")
        # The Users link is the active page → aria-current="page".
        snippet = resp.text[resp.text.find('data-testid="nav-users"') :]
        assert 'aria-current="page"' in snippet[:300]

    def test_manager_nav_includes_suppliers_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/")
        assert 'data-testid="nav-suppliers"' in resp.text
        assert 'href="/admin/suppliers"' in resp.text
        # Manager is not an admin — no Users link.
        assert 'data-testid="nav-users"' not in resp.text

    def test_admin_nav_includes_suppliers_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/")
        assert 'data-testid="nav-suppliers"' in resp.text

    def test_workshop_nav_excludes_suppliers_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/")
        assert 'data-testid="nav-suppliers"' not in resp.text

    def test_office_nav_excludes_suppliers_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Suppliers are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/")
        assert 'data-testid="nav-suppliers"' not in resp.text

    def test_aria_current_on_suppliers_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/suppliers")
        snippet = resp.text[resp.text.find('data-testid="nav-suppliers"') :]
        assert 'aria-current="page"' in snippet[:300]

    def test_manager_nav_includes_locations_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/")
        assert 'data-testid="nav-locations"' in resp.text
        assert 'href="/admin/locations"' in resp.text

    def test_admin_nav_includes_locations_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/")
        assert 'data-testid="nav-locations"' in resp.text

    def test_workshop_nav_excludes_locations_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/")
        assert 'data-testid="nav-locations"' not in resp.text

    def test_office_nav_excludes_locations_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Locations are Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/")
        assert 'data-testid="nav-locations"' not in resp.text

    def test_aria_current_on_locations_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/locations")
        snippet = resp.text[resp.text.find('data-testid="nav-locations"') :]
        assert 'aria-current="page"' in snippet[:300]

    def test_manager_nav_includes_taxonomy_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/")
        assert 'data-testid="nav-taxonomy"' in resp.text
        assert 'href="/admin/taxonomy"' in resp.text

    def test_admin_nav_includes_taxonomy_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/")
        assert 'data-testid="nav-taxonomy"' in resp.text

    def test_workshop_nav_excludes_taxonomy_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/")
        assert 'data-testid="nav-taxonomy"' not in resp.text

    def test_office_nav_excludes_taxonomy_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Taxonomy is Manager-owned (MISSION §3) — Office is a sibling, not a subset."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/")
        assert 'data-testid="nav-taxonomy"' not in resp.text

    def test_aria_current_on_taxonomy_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/taxonomy")
        snippet = resp.text[resp.text.find('data-testid="nav-taxonomy"') :]
        assert 'aria-current="page"' in snippet[:300]

    def test_manager_nav_includes_items_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/")
        assert 'data-testid="nav-items"' in resp.text
        assert 'href="/admin/items"' in resp.text

    def test_admin_nav_includes_items_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        admin = _make_user(db_session, email="admin@x.test", role=Role.ADMIN)
        _login_as(client, admin)
        resp = client.get("/")
        assert 'data-testid="nav-items"' in resp.text

    def test_workshop_nav_excludes_items_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        worker = _make_user(db_session, email="w@x.test", role=Role.WORKSHOP)
        _login_as(client, worker)
        resp = client.get("/")
        assert 'data-testid="nav-items"' not in resp.text

    def test_office_nav_includes_items_link(
        self, client: TestClient, db_session: Session
    ) -> None:
        """I1b: Office gets read+edit access to items (MISSION §3)."""
        office = _make_user(db_session, email="o@x.test", role=Role.OFFICE)
        _login_as(client, office)
        resp = client.get("/")
        assert 'data-testid="nav-items"' in resp.text
        assert 'href="/admin/items"' in resp.text

    def test_aria_current_on_items_page(
        self, client: TestClient, db_session: Session
    ) -> None:
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        resp = client.get("/admin/items")
        snippet = resp.text[resp.text.find('data-testid="nav-items"') :]
        assert 'aria-current="page"' in snippet[:300]


class TestFlashRegion:
    def test_no_flash_renders_nothing(
        self, client: TestClient, db_session: Session
    ) -> None:
        _login_as(client, _make_user(db_session, email="m@x.test", role=Role.MANAGER))
        resp = client.get("/")
        assert 'data-testid="flash"' not in resp.text

    def test_flash_appears_after_set_then_cleared_on_next_load(
        self, client: TestClient, db_session: Session
    ) -> None:
        """Flash is one-shot: appears once, then is consumed."""
        mgr = _make_user(db_session, email="m@x.test", role=Role.MANAGER)
        _login_as(client, mgr)
        # Trigger a flash by creating a supplier.
        token = _csrf(client)
        resp = client.post(
            "/admin/suppliers",
            data={"name": "Flashed Co", "csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert 'data-testid="flash"' in resp.text
        assert "Flashed Co" in resp.text

        # Reloading the same page should NOT re-render the flash.
        again = client.get("/admin/suppliers")
        assert 'data-testid="flash"' not in again.text


def _csrf(client: TestClient) -> str:
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


class TestHeaderSignOut:
    def test_anonymous_has_no_signout_form(self, client: TestClient) -> None:
        resp = client.get("/")
        assert 'action="/auth/logout"' not in resp.text

    def test_signed_in_user_sees_signout_form_with_csrf(
        self, client: TestClient, db_session: Session
    ) -> None:
        user = _make_user(db_session, email="signed@x.test", role=Role.OFFICE)
        _login_as(client, user)
        resp = client.get("/")
        assert 'action="/auth/logout"' in resp.text
        assert 'name="csrf_token"' in resp.text
        assert 'data-testid="sign-out"' in resp.text
