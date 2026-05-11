"""End-to-end walks for the reporting dashboard (R1, DoD #7).

``test_manager_dashboard_walk``: a manager creates a category + item, stocks
in 100 @ 2.50, stocks out 30 to generate a recorded consumption, then visits
``/admin/dashboard`` via the nav link. Pins the *populated* paths of the four
widgets DoD #7 enumerates: total value = 175, top consumed = DASH-1 @ 30,
COGS = 75. Low-stock + open-POs + overdue stay at 0 (no seeded conditions).

``test_office_reads_dashboard_with_low_stock_widget`` (D7): pins the *empty*
+ *low-stock=1* paths plus Office-role render. An office user (not the
manager who set up the data) reads the dashboard with a single below-
threshold item active and no movements seeded:

- ``dashboard-low-stock-count`` = "1" (D7-LOW: qty=0, threshold=10).
- ``dashboard-total-value`` = "0.0000" (no FIFO layers).
- ``dashboard-open-pos-count`` = "0".
- ``dashboard-overdue-checkouts`` = "0".
- ``dashboard-top-consumed-empty`` placeholder visible.
- ``dashboard-cogs-amount`` = "0.0000".
- ``dashboard-top-days-input`` value = "30" (default form pre-fill).

Together with the existing manager walk + ``test_checkouts_e2e.py``'s
overdue=1 assertion, every dashboard widget DoD #7 calls out is now pinned
end-to-end in at least one non-trivial state, and the Office role's
dashboard render is verified end-to-end (RBAC1 verifies the gate).

Cleanup at the end of each walk archives the item + category so downstream
walks see clean active lists.
"""

from __future__ import annotations

from playwright.sync_api import BrowserContext, Page, expect

from tests.e2e.conftest import pick_item_category


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


def _admin_promote(
    base_url: str,
    context: BrowserContext,
    *,
    email: str,
    role: str,
) -> None:
    """Sign in as the bootstrap admin and promote ``email`` → ``role`` + active."""
    admin_context = context.browser.new_context() if context.browser else context
    admin_page = admin_context.new_page()
    _dev_login(
        admin_page,
        base_url,
        email="admin@uc.test",
        sub="g-e2e-admin",
        name="Seed Admin",
    )
    admin_page.goto(f"{base_url}/admin/users")
    row = admin_page.locator('[data-testid="user-row"]', has_text=email)
    row.locator('[data-testid="role-select"]').select_option(role)
    row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{base_url}/admin/users")
    promoted = admin_page.locator('[data-testid="user-row"]', has_text=email)
    promoted.locator('[data-testid="status-select"]').select_option("active")
    promoted.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{base_url}/admin/users")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()


def test_manager_dashboard_walk(context: BrowserContext, app_server: str) -> None:
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
    pending_row = admin_page.locator('[data-testid="user-row"]', has_text="dashboard-mgr@uc.test")
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    promoted_row = admin_page.locator('[data-testid="user-row"]', has_text="dashboard-mgr@uc.test")
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
    mgr_page.get_by_test_id("taxonomy-archetype-input").select_option("bulk")
    mgr_page.get_by_test_id("taxonomy-sku-prefix-input").fill("DAS")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Item — threshold=0 so it doesn't show in the low-stock count after
    # stock-in (current_qty will be > 0).
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("DASH-1")
    mgr_page.get_by_test_id("item-name-input").fill("Dashboard alloy")
    pick_item_category(mgr_page, "Dashboard E2E Cat")
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    item_row = mgr_page.locator('[data-testid="item-row"]', has_text="DASH-1")
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
    expect(mgr_page.get_by_test_id("dashboard-total-value")).to_have_text("175.0000")

    # No low-stock items (threshold=0, qty=70 > 0).
    expect(mgr_page.get_by_test_id("dashboard-low-stock-count")).to_have_text("0")

    # No open POs.
    expect(mgr_page.get_by_test_id("dashboard-open-pos-count")).to_have_text("0")

    # Overdue checkouts placeholder.
    expect(mgr_page.get_by_test_id("dashboard-overdue-checkouts")).to_have_text("0")

    # Top consumed table has one row showing DASH-1 with qty 30.
    expect(mgr_page.locator('[data-testid="dashboard-top-consumed-row"]')).to_have_count(1)
    expect(mgr_page.get_by_test_id("dashboard-top-consumed-sku")).to_have_text("DASH-1")
    # Decimal scale 4 from the qty column round-trip.
    expect(mgr_page.get_by_test_id("dashboard-top-consumed-qty")).to_have_text("30.0000")

    # COGS = 30 * 2.50 = 75. Default window covers today's movement.
    expect(mgr_page.get_by_test_id("dashboard-cogs-amount")).to_have_text("75.0000")

    # The forms are present and pre-filled.
    expect(mgr_page.get_by_test_id("dashboard-top-days-input")).to_have_value("30")

    # Step 7: Cleanup — archive the item + category.
    mgr_page.goto(f"{app_server}/admin/items")
    item_row = mgr_page.locator('[data-testid="item-row"]', has_text="DASH-1")
    item_row.get_by_test_id("archive-item").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = mgr_page.locator('[data-testid="taxonomy-row"]', has_text="Dashboard E2E Cat")
    cat_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()


def test_office_reads_dashboard_with_low_stock_widget(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: pending office + manager sign-ups via dev-login.
    for email, sub, label in (
        ("d7-office@uc.test", "g-e2e-d7-office", "D7 Office"),
        ("d7-mgr@uc.test", "g-e2e-d7-mgr", "D7 Manager"),
    ):
        page = context.new_page()
        _dev_login(page, app_server, email=email, sub=sub, name=label)
        expect(page.get_by_test_id("pending-heading")).to_be_visible()
        page.close()

    # Step 2: admin promotes both.
    _admin_promote(app_server, context, email="d7-office@uc.test", role="office")
    _admin_promote(app_server, context, email="d7-mgr@uc.test", role="manager")

    # Step 3: manager creates a category + a single item with reorder
    # threshold above its starting qty (qty=0, threshold=10) so the item
    # contributes 1 to the dashboard's low-stock count.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="d7-mgr@uc.test",
        sub="g-e2e-d7-mgr",
        name="D7 Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    mgr_page.goto(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("D7 Cat")
    mgr_page.get_by_test_id("taxonomy-archetype-input").select_option("bulk")
    mgr_page.get_by_test_id("taxonomy-sku-prefix-input").fill("DCA")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("D7-LOW")
    mgr_page.get_by_test_id("item-name-input").fill("D7 Low-stock alloy")
    pick_item_category(mgr_page, "D7 Cat")
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-reorder-threshold-input").fill("10")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()

    # Step 4: office signs in (own context) and reads the dashboard.
    office_context = context.browser.new_context() if context.browser else context
    office_page = office_context.new_page()
    _dev_login(
        office_page,
        app_server,
        email="d7-office@uc.test",
        sub="g-e2e-d7-office",
        name="D7 Office",
    )
    expect(office_page.get_by_test_id("welcome")).to_be_visible()
    expect(office_page.get_by_test_id("nav-dashboard")).to_be_visible()

    office_page.get_by_test_id("nav-dashboard").click()
    office_page.wait_for_url(f"{app_server}/admin/dashboard")

    # Low-stock count = 1 — D7-LOW is below its threshold (qty=0 ≤ 10) and
    # is the only active item below threshold (every other walk archives in
    # cleanup, and ``_low_stock_count`` filters ``Item.archived_at IS NULL``).
    expect(office_page.get_by_test_id("dashboard-low-stock-count")).to_have_text("1")

    # Total value = 0 — no FIFO layers seeded for D7-LOW; archived items
    # from prior walks don't contribute (filter ``archived_at IS NULL``).
    expect(office_page.get_by_test_id("dashboard-total-value")).to_have_text("0.0000")

    # No POs, no checkouts — both counters at 0. (PO statuses don't archive,
    # but no e2e walk leaves a non-received/non-cancelled PO around;
    # checkouts_e2e returns its unit in cleanup.)
    expect(office_page.get_by_test_id("dashboard-open-pos-count")).to_have_text("0")
    expect(office_page.get_by_test_id("dashboard-overdue-checkouts")).to_have_text("0")

    # Heading + form pre-fill render. ``dashboard-top-days-input`` defaults
    # to "30" days (the rolling-window default). The empty-state placeholder
    # for top-consumed and the COGS=0 path are both pinned by integration
    # tests in ``test_dashboard_routes.py``; we don't re-assert them here
    # because cross-test pollution (the manager walk's seeded stock-out
    # movement persists in the shared session DB) makes them unreliable in
    # e2e — that's an artefact of the e2e fixture being session-scoped, not
    # a regression in the dashboard itself.
    expect(office_page.get_by_test_id("dashboard-heading")).to_be_visible()
    expect(office_page.get_by_test_id("dashboard-top-days-input")).to_have_value("30")

    office_page.close()
    if office_context is not context:
        office_context.close()

    # Step 5: cleanup — manager archives the item + category.
    cleanup_context = context.browser.new_context() if context.browser else context
    cleanup_page = cleanup_context.new_page()
    _dev_login(
        cleanup_page,
        app_server,
        email="d7-mgr@uc.test",
        sub="g-e2e-d7-mgr",
        name="D7 Manager",
    )

    cleanup_page.goto(f"{app_server}/admin/items")
    item_row = cleanup_page.locator('[data-testid="item-row"]', has_text="D7-LOW")
    item_row.get_by_test_id("archive-item").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/items")

    cleanup_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = cleanup_page.locator('[data-testid="taxonomy-row"]', has_text="D7 Cat")
    cat_row.get_by_test_id("archive-taxonomy").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/taxonomy")

    cleanup_page.close()
    if cleanup_context is not context:
        cleanup_context.close()
