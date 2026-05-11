"""End-to-end manager workflow for locations: promote → create / archive / unarchive.

Mirrors ``test_suppliers_e2e.py`` (S1) so that the user-visible loop for S2 is
covered. Uses a distinct manager email so the test is order-independent
relative to the suppliers e2e (both share the same session-scoped DB).
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


def test_manager_creates_views_archives_and_unarchives_a_location(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future locations manager signs in for the first time (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="loc-mgr@uc.test",
        sub="g-e2e-location-mgr",
        name="Location Manager",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: Admin signs in. Bootstrap promotion fires only on the first
    # sign-in across the session DB; subsequent dev-logins as the same email
    # are idempotent (already admin/active), so this works regardless of test
    # ordering.
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
    pending_row = admin_page.locator('[data-testid="user-row"]', has_text="loc-mgr@uc.test")
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    promoted_row = admin_page.locator('[data-testid="user-row"]', has_text="loc-mgr@uc.test")
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
        email="loc-mgr@uc.test",
        sub="g-e2e-location-mgr",
        name="Location Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    # Step 5: The Locations link appears in the role-aware primary nav.
    expect(mgr_page.get_by_test_id("nav-locations")).to_be_visible()

    # Step 6: Click into Locations. Other e2e tests don't create locations, so
    # the list starts empty for this session.
    mgr_page.get_by_test_id("nav-locations").click()
    mgr_page.wait_for_url(f"{app_server}/admin/locations")
    expect(mgr_page.get_by_test_id("locations-empty")).to_be_visible()

    # Step 7: Create "Workshop Bench".
    mgr_page.get_by_test_id("new-location").click()
    mgr_page.wait_for_url(f"{app_server}/admin/locations/new")
    mgr_page.get_by_test_id("location-name-input").fill("Workshop Bench")
    mgr_page.get_by_test_id("location-notes-input").fill("Main filing bench")
    mgr_page.get_by_test_id("location-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/locations")

    # Flash and row both visible.
    expect(mgr_page.get_by_test_id("flash")).to_contain_text("Workshop Bench")
    bench_row = mgr_page.locator('[data-testid="location-row"]', has_text="Workshop Bench")
    expect(bench_row).to_be_visible()

    # Step 8: Archive the location.
    bench_row.get_by_test_id("archive-location").click()
    mgr_page.wait_for_url(f"{app_server}/admin/locations")
    expect(
        mgr_page.locator('[data-testid="location-row"]', has_text="Workshop Bench")
    ).to_have_count(0)

    # Step 9: Switch to archived tab — Workshop Bench is there.
    mgr_page.get_by_test_id("tab-archived").click()
    mgr_page.wait_for_url(f"{app_server}/admin/locations?show=archived")
    archived_row = mgr_page.locator('[data-testid="location-row"]', has_text="Workshop Bench")
    expect(archived_row).to_be_visible()

    # Step 10: Unarchive — moves back to active.
    archived_row.get_by_test_id("unarchive-location").click()
    mgr_page.wait_for_url(f"{app_server}/admin/locations")
    restored_row = mgr_page.locator('[data-testid="location-row"]', has_text="Workshop Bench")
    expect(restored_row).to_be_visible()

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
