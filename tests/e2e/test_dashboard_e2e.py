"""End-to-end walk for the reporting dashboard (R1).

A manager creates a category + item, stocks in 100 @ 2.50, stocks out 30 to
generate a recorded consumption, then visits ``/admin/dashboard`` via the nav
link. Verifies the four widgets DoD #7 enumerates:

- Total inventory value = (100 - 30) * 2.50 = 175 (Decimal scale 4)
- Low-stock count = 0 (the item has stock above the default 0 threshold)
- Top consumed table has one row showing the item with qty=30
- COGS for the default window = 30 * 2.50 = 75

Cleanup at the end archives the item + category so downstream walks see
clean active lists.
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


def test_manager_dashboard_walk(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future manager signs up (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="dashboard-mgr@uc.test",
        sub="g-e2e-dashboard-mgr",
        name="Dashboard Manager",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: Admin signs in and promotes the manager.
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

    admin_page.goto(f"{app_server}/admin/users")
    pending_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="dashboard-mgr@uc.test"
    )
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    promoted_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="dashboard-mgr@uc.test"
    )
    promoted_row.locator('[data-testid="status-select"]').select_option("active")
    promoted_row.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()

    # Step 3: Manager signs in, creates a category + item, exercises the
    # cost engine via stock-in + stock-out, then visits the dashboard.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="dashboard-mgr@uc.test",
        sub="g-e2e-dashboard-mgr",
        name="Dashboard Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()
    expect(mgr_page.get_by_test_id("nav-dashboard")).to_be_visible()

    # Category.
    mgr_page.goto(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Dashboard E2E Cat")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Item — threshold=0 so it doesn't show in the low-stock count after
    # stock-in (current_qty will be > 0).
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("DASH-1")
    mgr_page.get_by_test_id("item-name-input").fill("Dashboard alloy")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Dashboard E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="DASH-1"
    )
    item_id = item_row.get_attribute("data-item-id")
    assert item_id is not None

    # Step 4: Stock in 100 @ 2.50.
    mgr_page.goto(f"{app_server}/admin/items/{item_id}/in")
    mgr_page.get_by_test_id("stock-in-qty-input").fill("100")
    mgr_page.get_by_test_id("stock-in-unit-cost-input").fill("2.50")
    mgr_page.get_by_test_id("stock-in-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items/{item_id}/in")

    # Step 5: Stock out 30 — this generates a recorded consumption that the
    # COGS aggregation picks up.
    mgr_page.goto(f"{app_server}/admin/items/{item_id}/out")
    mgr_page.get_by_test_id("stock-out-qty-input").fill("30")
    mgr_page.get_by_test_id("stock-out-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items/{item_id}/out")

    # Step 6: Visit the dashboard via the nav link.
    mgr_page.get_by_test_id("nav-dashboard").click()
    mgr_page.wait_for_url(f"{app_server}/admin/dashboard")

    # Total inventory value = (100 - 30) * 2.50 = 175 (scale-4 column round-trip).
    expect(mgr_page.get_by_test_id("dashboard-total-value")).to_have_text(
        "175.0000"
    )

    # No low-stock items (threshold=0, qty=70 > 0).
    expect(mgr_page.get_by_test_id("dashboard-low-stock-count")).to_have_text("0")

    # No open POs.
    expect(mgr_page.get_by_test_id("dashboard-open-pos-count")).to_have_text("0")

    # Overdue checkouts placeholder.
    expect(
        mgr_page.get_by_test_id("dashboard-overdue-checkouts")
    ).to_have_text("0")

    # Top consumed table has one row showing DASH-1 with qty 30.
    expect(
        mgr_page.locator('[data-testid="dashboard-top-consumed-row"]')
    ).to_have_count(1)
    expect(mgr_page.get_by_test_id("dashboard-top-consumed-sku")).to_have_text(
        "DASH-1"
    )
    # Decimal scale 4 from the qty column round-trip.
    expect(mgr_page.get_by_test_id("dashboard-top-consumed-qty")).to_have_text(
        "30.0000"
    )

    # COGS = 30 * 2.50 = 75. Default window covers today's movement.
    expect(mgr_page.get_by_test_id("dashboard-cogs-amount")).to_have_text(
        "75.0000"
    )

    # The forms are present and pre-filled.
    expect(mgr_page.get_by_test_id("dashboard-top-days-input")).to_have_value(
        "30"
    )

    # Step 7: Cleanup — archive the item + category.
    mgr_page.goto(f"{app_server}/admin/items")
    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="DASH-1"
    )
    item_row.get_by_test_id("archive-item").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Dashboard E2E Cat"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
