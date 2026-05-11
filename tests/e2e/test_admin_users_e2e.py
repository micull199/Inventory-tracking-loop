"""End-to-end admin workflow: admin signs in, promotes a pending user, the user signs in.

Closes the round-trip for DoD #1: a new user signs in (lands pending) → admin
assigns a role + activates → that user signs back in and sees the welcome page.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, expect


def _dev_login(page: Page, base_url: str, email: str, sub: str, name: str = "Test User") -> None:
    """Hit the test-only dev-login endpoint via a synthetic form post."""
    page.set_content(
        f"""<form id="f" method="post" action="{base_url}/auth/_dev-login">
              <input name="email" value="{email}">
              <input name="name" value="{name}">
              <input name="sub" value="{sub}">
            </form>"""
    )
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_url(f"{base_url}/")


def test_admin_promotes_pending_user_who_then_signs_in(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: The future workshop user signs in for the first time. They land
    # on the pending page because no role is assigned yet.
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="incoming@uc.test",
        sub="g-e2e-incoming",
        name="Incoming Worker",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: The admin signs in (BOOTSTRAP_ADMIN_EMAIL=admin@uc.test in the
    # e2e env, so first sign-in with that email auto-promotes to admin/active).
    # We use a separate browser context so the two users have independent
    # sessions — closer to reality than reusing cookies.
    admin_context = context.browser.new_context() if context.browser else context
    admin_page = admin_context.new_page()
    _dev_login(
        admin_page,
        app_server,
        email="admin@uc.test",
        sub="g-e2e-admin",
        name="Seed Admin",
    )
    # Confirm admin landed on the welcome page (not pending).
    expect(admin_page.get_by_test_id("welcome")).to_be_visible()

    # Step 3: Admin opens the user-management page and finds the pending user.
    admin_page.goto(f"{app_server}/admin/users")
    expect(admin_page.get_by_test_id("admin-users-table")).to_be_visible()

    pending_row = admin_page.locator('[data-testid="user-row"]', has_text="incoming@uc.test")
    expect(pending_row).to_have_attribute("data-user-status", "pending")

    # Step 4: Admin assigns the workshop role.
    pending_row.locator('[data-testid="role-select"]').select_option("workshop")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    # Step 5: Admin activates the now-roled user.
    refreshed_row = admin_page.locator('[data-testid="user-row"]', has_text="incoming@uc.test")
    refreshed_row.locator('[data-testid="status-select"]').select_option("active")
    refreshed_row.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    # The row now shows status=active.
    final_row = admin_page.locator('[data-testid="user-row"]', has_text="incoming@uc.test")
    expect(final_row).to_have_attribute("data-user-status", "active")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()

    # Step 6: The (now-promoted) user signs back in. They should see the welcome
    # page with role=workshop, not the pending page.
    workshop_context = context.browser.new_context() if context.browser else context
    workshop_page = workshop_context.new_page()
    _dev_login(
        workshop_page,
        app_server,
        email="incoming@uc.test",
        sub="g-e2e-incoming",
        name="Incoming Worker",
    )
    expect(workshop_page.get_by_test_id("welcome")).to_be_visible()
    expect(workshop_page.get_by_test_id("welcome")).to_contain_text("workshop")
    expect(workshop_page.get_by_test_id("user-status")).to_have_text("active")
    workshop_page.close()
    if workshop_context is not context:
        workshop_context.close()
