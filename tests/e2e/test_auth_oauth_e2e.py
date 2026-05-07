"""End-to-end Google OAuth flow using the local stub provider.

Exercises the real /auth/google/login and /auth/google/callback routes
end-to-end through a real Chromium browser.  The local stub (app/oauth_test_stub.py)
replaces Google's authorize, token, and userinfo endpoints — no external
network call is made and no JWT verification occurs.

Walk:
  1. Anonymous user clicks "Sign in with Google" → OAuth stub flow → pending page.
  2. Admin promotes the stub user via /admin/users.
  3. Stub user signs in again via OAuth → sees the welcome page (active/workshop).

This is the final gap to ticking DoD #1: previous coverage used _dev-login to
bypass the OAuth routes; this walk exercises them end-to-end in a browser.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, expect

# The fixed identity returned by the OAuth stub for every sign-in.
_STUB_EMAIL = "oauthstub@uc.example"
_STUB_NAME = "OAuth Stub User"


def _dev_login(page: Page, base_url: str, email: str, sub: str, name: str) -> None:
    """Sign in via the dev-login backdoor (used for the admin context only)."""
    page.set_content(
        f"""<form id="f" method="post" action="{base_url}/auth/_dev-login">
              <input name="email" value="{email}">
              <input name="name" value="{name}">
              <input name="sub" value="{sub}">
            </form>"""
    )
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_url(f"{base_url}/")


def _oauth_sign_in(page: Page, base_url: str) -> None:
    """Click 'Sign in with Google' and complete the stub OAuth flow."""
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("sign-in")).to_be_visible()
    page.get_by_test_id("sign-in").click()
    # The OAuth redirect chain ends at / after the callback handler commits the
    # user and issues a 303.  wait_for_url handles the full redirect sequence.
    page.wait_for_url(f"{base_url}/")


def test_google_oauth_stub_new_user_lands_pending(
    page: Page, oauth_stub_app_server: str
) -> None:
    """A first-time OAuth sign-in creates a pending user."""
    _oauth_sign_in(page, oauth_stub_app_server)
    expect(page.get_by_test_id("pending-heading")).to_be_visible()


def test_google_oauth_stub_full_cycle(
    context: BrowserContext, oauth_stub_app_server: str
) -> None:
    """Full DoD #1 cycle via Google OAuth stub: sign-in → pending → promote → active.

    Step 1: stub user signs in via Google OAuth → lands on pending page.
    Step 2: admin (via _dev-login, which is the existing tested path) promotes
            and activates the stub user.
    Step 3: stub user signs in again via Google OAuth → sees welcome (active).
    """
    base = oauth_stub_app_server

    # --- Step 1: stub user signs in for the first time via Google OAuth ---
    stub_context = context.browser.new_context() if context.browser else context
    stub_page = stub_context.new_page()
    _oauth_sign_in(stub_page, base)
    expect(stub_page.get_by_test_id("pending-heading")).to_be_visible()
    stub_page.close()
    stub_context.close()

    # --- Step 2: admin signs in and promotes the pending stub user ---
    admin_context = context.browser.new_context() if context.browser else context
    admin_page = admin_context.new_page()
    _dev_login(admin_page, base, email="admin@uc.test", sub="g-admin-stub", name="Seed Admin")
    expect(admin_page.get_by_test_id("welcome")).to_be_visible()

    admin_page.goto(f"{base}/admin/users")
    expect(admin_page.get_by_test_id("admin-users-table")).to_be_visible()

    pending_row = admin_page.locator('[data-testid="user-row"]', has_text=_STUB_EMAIL)
    expect(pending_row).to_have_attribute("data-user-status", "pending")

    # Assign workshop role.
    pending_row.locator('[data-testid="role-select"]').select_option("workshop")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{base}/admin/users")

    # Activate the user.
    refreshed_row = admin_page.locator('[data-testid="user-row"]', has_text=_STUB_EMAIL)
    refreshed_row.locator('[data-testid="status-select"]').select_option("active")
    refreshed_row.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{base}/admin/users")
    admin_page.close()
    admin_context.close()

    # --- Step 3: stub user signs in again via Google OAuth → active/welcome ---
    workshop_context = context.browser.new_context() if context.browser else context
    workshop_page = workshop_context.new_page()
    _oauth_sign_in(workshop_page, base)
    # The stub always returns the same sub/email, so upsert_user_from_userinfo
    # finds the existing (now-active) user and updates name/email without
    # changing role or status.
    expect(workshop_page.get_by_test_id("welcome")).to_be_visible()
    expect(workshop_page.get_by_test_id("welcome")).to_contain_text("workshop")
    expect(workshop_page.get_by_test_id("user-status")).to_have_text("active")
    workshop_page.close()
    workshop_context.close()
