"""End-to-end walk for manual stock-in (M2).

Workshop's first positive-write surface: the M2 route is the only place a
Workshop user can mutate the system today (Workshop still 403s on items list /
edit per I3a's role table). The walk:

1. A pending workshop user signs up via dev-login.
2. Admin (bootstrap or pre-promoted) promotes them to Workshop + active.
3. A manager (separately) creates a category and an item via the UI so we
   exercise the items routes' end-to-end shape, not just inserted rows.
4. The workshop user signs in and deep-links to ``/admin/items/{id}/in`` —
   no UI link from the items list because Workshop can't see it yet (deferred
   to I1c).
5. Workshop submits a receipt; the form re-renders with the flash, the bumped
   ``current_qty``, and the new movement in the recent-movements table.
6. Cleanup: manager archives the item + the taxonomy category so downstream
   e2e walks see empty active lists. (No attempt to "delete" the movement — the
   audit log is append-only by mission.)
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


def test_workshop_records_a_manual_stock_in(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future workshop user signs up (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="movements-ws@uc.test",
        sub="g-e2e-movements-ws",
        name="Movements Workshop",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: Admin signs in (bootstrap is one-shot but idempotent thereafter).
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

    # Step 3: Admin promotes the pending user → workshop + active.
    admin_page.goto(f"{app_server}/admin/users")
    pending_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="movements-ws@uc.test"
    )
    pending_row.locator('[data-testid="role-select"]').select_option("workshop")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    promoted_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="movements-ws@uc.test"
    )
    promoted_row.locator('[data-testid="status-select"]').select_option("active")
    promoted_row.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()

    # Step 4: A separate manager promotes themselves the same way (admin already
    # exists, so this user is created pending and *also* needs promoting). Sign
    # admin back in to do it.
    pending_mgr_page = context.new_page()
    _dev_login(
        pending_mgr_page,
        app_server,
        email="movements-mgr@uc.test",
        sub="g-e2e-movements-mgr",
        name="Movements Manager",
    )
    expect(pending_mgr_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_mgr_page.close()

    admin_again_context = (
        context.browser.new_context() if context.browser else context
    )
    admin_again = admin_again_context.new_page()
    _dev_login(
        admin_again,
        app_server,
        email="admin@uc.test",
        sub="g-e2e-admin",
        name="Seed Admin",
    )
    admin_again.goto(f"{app_server}/admin/users")
    mgr_pending = admin_again.locator(
        '[data-testid="user-row"]', has_text="movements-mgr@uc.test"
    )
    mgr_pending.locator('[data-testid="role-select"]').select_option("manager")
    mgr_pending.locator('[data-testid="role-submit"]').click()
    admin_again.wait_for_url(f"{app_server}/admin/users")
    mgr_promoted = admin_again.locator(
        '[data-testid="user-row"]', has_text="movements-mgr@uc.test"
    )
    mgr_promoted.locator('[data-testid="status-select"]').select_option("active")
    mgr_promoted.locator('[data-testid="status-submit"]').click()
    admin_again.wait_for_url(f"{app_server}/admin/users")
    admin_again.close()
    if admin_again_context is not context:
        admin_again_context.close()

    # Step 5: Manager creates a taxonomy category + an item we can stock-in to.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="movements-mgr@uc.test",
        sub="g-e2e-movements-mgr",
        name="Movements Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    mgr_page.get_by_test_id("new-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Movements E2E Cat")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("MV-E2E-001")
    mgr_page.get_by_test_id("item-name-input").fill("Casting alloy")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Movements E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    # Capture the item id from the row so we can deep-link the workshop user.
    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="MV-E2E-001"
    )
    item_id = item_row.get_attribute("data-item-id")
    assert item_id is not None

    # Sanity: manager can also see the new "Stock in →" link on the edit form.
    item_row.get_by_test_id("edit-item").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    expect(mgr_page.get_by_test_id("stock-in-link")).to_be_visible()
    mgr_page.close()
    if mgr_context is not context:
        # Keep the manager context alive for cleanup at the end of the test.
        pass

    # Step 6: Workshop user signs in and deep-links to the stock-in form. The
    # items list is still 403 for Workshop (deferred to I1c), so we don't try
    # to navigate there.
    ws_context = context.browser.new_context() if context.browser else context
    ws_page = ws_context.new_page()
    _dev_login(
        ws_page,
        app_server,
        email="movements-ws@uc.test",
        sub="g-e2e-movements-ws",
        name="Movements Workshop",
    )
    expect(ws_page.get_by_test_id("welcome")).to_be_visible()

    ws_page.goto(f"{app_server}/admin/items/{item_id}/in")
    expect(ws_page.get_by_test_id("movements-empty")).to_be_visible()
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("0.0000")

    ws_page.get_by_test_id("stock-in-qty-input").fill("100")
    ws_page.get_by_test_id("stock-in-unit-cost-input").fill("2.50")
    ws_page.get_by_test_id("stock-in-reason-input").fill("First receipt")
    ws_page.get_by_test_id("stock-in-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/in")

    # Flash, bumped qty, recent-movements row.
    expect(ws_page.get_by_test_id("flash")).to_contain_text("Casting alloy")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("100")
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("100.0000")
    movement_row = ws_page.locator('[data-testid="movement-row"]').first
    expect(movement_row).to_contain_text("First receipt")
    expect(movement_row).to_contain_text("100")
    expect(movement_row).to_contain_text("250")  # total_cost = 100 * 2.50

    ws_page.close()
    if ws_context is not context:
        ws_context.close()

    # Step 7: Cleanup — manager archives the item + the category so downstream
    # walks start with empty active lists. Same posture as the items walk.
    cleanup_context = (
        context.browser.new_context() if context.browser else context
    )
    cleanup_page = cleanup_context.new_page()
    _dev_login(
        cleanup_page,
        app_server,
        email="movements-mgr@uc.test",
        sub="g-e2e-movements-mgr",
        name="Movements Manager",
    )
    cleanup_page.goto(f"{app_server}/admin/items")
    item_row_to_archive = cleanup_page.locator(
        '[data-testid="item-row"]', has_text="MV-E2E-001"
    )
    item_row_to_archive.get_by_test_id("archive-item").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/items")

    cleanup_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = cleanup_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Movements E2E Cat"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/taxonomy")
    cleanup_page.close()
    if cleanup_context is not context:
        cleanup_context.close()
