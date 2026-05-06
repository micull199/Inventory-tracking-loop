"""End-to-end manager workflow: promote a manager → manager creates / archives / unarchives a supplier.

Closes the user-visible loop for S1 (suppliers CRUD) and exercises the
role-aware nav added in F4 + this slice (Suppliers link visible to Manager).
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, expect


def _dev_login(page: Page, base_url: str, email: str, sub: str, name: str = "Test User") -> None:
    page.set_content(
        f"""<form id="f" method="post" action="{base_url}/auth/_dev-login">
              <input name="email" value="{email}">
              <input name="name" value="{name}">
              <input name="sub" value="{sub}">
            </form>"""
    )
    page.evaluate("document.getElementById('f').submit()")
    page.wait_for_url(f"{base_url}/")


def test_manager_creates_views_archives_and_unarchives_a_supplier(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future manager signs in for the first time (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="manager@uc.test",
        sub="g-e2e-supplier-mgr",
        name="Supplier Manager",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: Admin signs in. Bootstrap promotion fires on the first sign-in
    # with this email; later sign-ins are idempotent (already admin/active).
    # If a previous e2e test already created the bootstrap admin, this still
    # signs us in as that admin — the test doesn't depend on which run.
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
        '[data-testid="user-row"]', has_text="manager@uc.test"
    )
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    promoted_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="manager@uc.test"
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
        email="manager@uc.test",
        sub="g-e2e-supplier-mgr",
        name="Supplier Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    # Step 5: The Suppliers link appears in the role-aware primary nav.
    expect(mgr_page.get_by_test_id("nav-suppliers")).to_be_visible()

    # Step 6: Click into Suppliers → empty state (no suppliers seeded for this
    # test in particular; if a previous e2e run left some, they are isolated by
    # the per-session temp DB so the list starts empty).
    mgr_page.get_by_test_id("nav-suppliers").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers")
    expect(mgr_page.get_by_test_id("suppliers-empty")).to_be_visible()

    # Step 7: Create "Acme Wax Co".
    mgr_page.get_by_test_id("new-supplier").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers/new")
    mgr_page.get_by_test_id("supplier-name-input").fill("Acme Wax Co")
    mgr_page.get_by_test_id("supplier-email-input").fill("orders@acme.test")
    mgr_page.get_by_test_id("supplier-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers")

    # Flash and row both visible.
    expect(mgr_page.get_by_test_id("flash")).to_contain_text("Acme Wax Co")
    acme_row = mgr_page.locator(
        '[data-testid="supplier-row"]', has_text="Acme Wax Co"
    )
    expect(acme_row).to_be_visible()

    # Step 8: Archive the supplier.
    acme_row.get_by_test_id("archive-supplier").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers")

    # Active tab should now be empty for Acme.
    expect(mgr_page.locator(
        '[data-testid="supplier-row"]', has_text="Acme Wax Co"
    )).to_have_count(0)

    # Step 9: Switch to archived tab — Acme is there.
    mgr_page.get_by_test_id("tab-archived").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers?show=archived")
    archived_row = mgr_page.locator(
        '[data-testid="supplier-row"]', has_text="Acme Wax Co"
    )
    expect(archived_row).to_be_visible()

    # Step 10: Unarchive — moves back to active.
    archived_row.get_by_test_id("unarchive-supplier").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers")
    restored_row = mgr_page.locator(
        '[data-testid="supplier-row"]', has_text="Acme Wax Co"
    )
    expect(restored_row).to_be_visible()

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
