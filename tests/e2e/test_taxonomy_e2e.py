"""End-to-end manager workflow for taxonomy: promote → create / archive / unarchive.

Mirrors ``test_locations_e2e.py``. Uses a distinct manager email so the test is
order-independent relative to the other Manager-owned settings e2e tests (they
all share the same session-scoped DB).
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, expect


def _dev_login(
    page: Page, base_url: str, email: str, sub: str, name: str = "Test User"
) -> None:
    page.set_content(
        f"""<form id="f" method="post" action="{base_url}/auth/_dev-login">
              <input name="email" value="{email}">
              <input name="name" value="{name}">
              <input name="sub" value="{sub}">
            </form>"""
    )
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_url(f"{base_url}/")


def test_manager_creates_views_archives_and_unarchives_a_category(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future taxonomy manager signs in for the first time (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="tax-mgr@uc.test",
        sub="g-e2e-tax-mgr",
        name="Taxonomy Manager",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: Admin signs in. Bootstrap promotion fires only once across the
    # session DB; subsequent dev-logins as the same email are idempotent.
    admin_context = context.browser.new_context() if context.browser else context
    admin_page = admin_context.new_page()
    _dev_login(
        admin_page,
        app_server,
        email="admin@uc.test",
        sub="g-e2e-admin",
        name="Seed Admin",
    )
    expect(admin_page.get_by_test_id("welcome")).to_be_visible()

    # Step 3: Admin promotes the pending user → manager + active.
    admin_page.goto(f"{app_server}/admin/users")
    pending_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="tax-mgr@uc.test"
    )
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    promoted_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="tax-mgr@uc.test"
    )
    promoted_row.locator('[data-testid="status-select"]').select_option("active")
    promoted_row.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()

    # Step 4: Manager signs back in — should land at welcome with role=manager.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="tax-mgr@uc.test",
        sub="g-e2e-tax-mgr",
        name="Taxonomy Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    # Step 5: The Taxonomy link appears in the role-aware primary nav.
    expect(mgr_page.get_by_test_id("nav-taxonomy")).to_be_visible()

    # Step 6: Click into Taxonomy. List starts empty for this test (other tests
    # don't create taxonomy nodes).
    mgr_page.get_by_test_id("nav-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")
    expect(mgr_page.get_by_test_id("taxonomy-empty")).to_be_visible()

    # Step 7: Create "Raw Materials".
    mgr_page.get_by_test_id("new-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Raw Materials")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Flash and row both visible.
    expect(mgr_page.get_by_test_id("flash")).to_contain_text("Raw Materials")
    rm_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Raw Materials"
    )
    expect(rm_row).to_be_visible()

    # Step 8: Archive the category.
    rm_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")
    expect(
        mgr_page.locator(
            '[data-testid="taxonomy-row"]', has_text="Raw Materials"
        )
    ).to_have_count(0)

    # Step 9: Switch to archived tab — Raw Materials is there.
    mgr_page.get_by_test_id("tab-archived").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy?show=archived")
    archived_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Raw Materials"
    )
    expect(archived_row).to_be_visible()

    # Step 10: Unarchive — moves back to active.
    archived_row.get_by_test_id("unarchive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")
    restored_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Raw Materials"
    )
    expect(restored_row).to_be_visible()

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
