"""End-to-end walk for manual stock-in (M2), stock-out (M3), adjust (M4),
detail (M6), and transfer (M5).

Workshop's first positive-write surface: M2 + M3 + M4 + M5 are the only
places a Workshop user can mutate the system today. After I1c, Workshop also
has *read-only* access to the items list and per-item view. The walk:

1. A pending workshop user signs up via dev-login.
2. Admin (bootstrap or pre-promoted) promotes them to Workshop + active.
3. A manager (separately) creates a pair of locations + a category + an item
   pinned to one of those locations via the UI so we exercise the items
   routes' end-to-end shape, not just inserted rows. The second location is
   the M5 transfer target.
4. The workshop user signs in. I1c: Workshop's nav now shows the items
   link; Workshop browses the list (no New CTA, "View" link instead of
   "Edit"), opens the read-only form (disabled inputs, no submit button),
   and confirms the in/out/adjust action links are still reachable. Then
   deep-links to ``/admin/items/{id}/in``.
5. Workshop submits a receipt; the form re-renders with the flash, the bumped
   ``current_qty``, and the new movement in the recent-movements table.
6. Workshop deep-links to ``/admin/items/{id}/out`` and consumes part of the
   layer; the form re-renders with the flash, the decremented ``current_qty``,
   and the new OUT movement showing the FIFO-derived ``total_cost``. Then
   tries to consume more than is open and asserts the in-form error block
   plus preserved inputs.
7. Workshop deep-links to ``/admin/items/{id}/adjust`` and submits two
   adjustments: an increase (creates a new positive_adjustment cost layer +
   bumps qty) and a decrease (consumes FIFO + decrements qty). Both legs
   verify the flash + bumped ``current_qty`` + new movement row.
8. Workshop visits the M6 detail page and verifies the consolidated read.
9. Workshop deep-links to ``/admin/items/{id}/transfer`` and moves the item
   to the second location. The form re-renders with the flash; the recent-
   movements row shows the TRANSFER. Re-visits detail and asserts the
   timeline now has five rows (the new TRANSFER on top), still with two
   layer-breakdown blocks (TRANSFER doesn't add one), and ``current_qty``
   unchanged at 75 (transfers don't change qty or value).
10. Cleanup: manager archives the item + the taxonomy category + both
    locations so downstream e2e walks see empty active lists.
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

    # Create two locations: the from-location (assigned to the item on create)
    # and the to-location (M5 transfer target).
    for loc_name in ("Movements From Bench", "Movements To Storage"):
        mgr_page.goto(f"{app_server}/admin/locations/new")
        mgr_page.get_by_test_id("location-name-input").fill(loc_name)
        mgr_page.get_by_test_id("location-submit").click()
        mgr_page.wait_for_url(f"{app_server}/admin/locations")

    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("MV-E2E-001")
    mgr_page.get_by_test_id("item-name-input").fill("Casting alloy")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Movements E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-location-input").select_option(
        label="Movements From Bench"
    )
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

    # Step 6: Workshop user signs in. After I1c, Workshop has the items
    # link in the primary nav, can see the items list (read-only), and can
    # click into a per-item read-only view. We exercise that path here before
    # the deep-links to the stock-in/out/adjust forms.
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
    expect(ws_page.get_by_test_id("nav-items")).to_be_visible()

    # I1c: Workshop navigates to the items list via the nav link, sees the
    # row created by the manager, and clicks "View" (not "Edit"). The form
    # renders with disabled inputs and no submit button — but the in/out/
    # adjust action links remain visible.
    ws_page.get_by_test_id("nav-items").click()
    ws_page.wait_for_url(f"{app_server}/admin/items")
    expect(ws_page.get_by_test_id("new-item")).not_to_be_visible()
    item_row_ws = ws_page.locator(
        '[data-testid="item-row"]', has_text="MV-E2E-001"
    )
    expect(item_row_ws).to_be_visible()
    expect(item_row_ws.get_by_test_id("view-item")).to_be_visible()
    expect(item_row_ws.get_by_test_id("archive-item")).not_to_be_visible()
    item_row_ws.get_by_test_id("view-item").click()
    ws_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    expect(ws_page.get_by_test_id("item-form-readonly-note")).to_be_visible()
    expect(ws_page.get_by_test_id("item-submit")).not_to_be_visible()
    expect(ws_page.get_by_test_id("stock-in-link")).to_be_visible()

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

    # Step 6b (M3): Workshop deep-links to the stock-out form and consumes 30
    # of the 100-unit layer. FIFO drains the only layer, so total_cost should
    # be 30 * 2.50 = 75. After the redirect, current_qty = 70.
    ws_page.goto(f"{app_server}/admin/items/{item_id}/out")
    # The "open value" line shows 100 * 2.50 = 250.
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("100.0000")
    expect(ws_page.get_by_test_id("item-open-value")).to_contain_text("250")

    ws_page.get_by_test_id("stock-out-qty-input").fill("30")
    ws_page.get_by_test_id("stock-out-reason-input").fill("Production run")
    ws_page.get_by_test_id("stock-out-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/out")

    expect(ws_page.get_by_test_id("flash")).to_contain_text("Casting alloy")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("30")
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("70.0000")
    # The OUT movement is newest, so the first movement-row is it. Total cost
    # = 30 * 2.50 = 75.
    out_row = ws_page.locator('[data-testid="movement-row"]').first
    expect(out_row).to_contain_text("Production run")
    expect(out_row).to_contain_text("30")
    expect(out_row).to_contain_text("75")

    # Insufficient-stock path: try to consume 1000 (only 70 left). The route
    # returns 400 + re-renders the form with the error block + preserved input.
    ws_page.get_by_test_id("stock-out-qty-input").fill("1000")
    ws_page.get_by_test_id("stock-out-reason-input").fill("Way too much")
    ws_page.get_by_test_id("stock-out-submit").click()
    # Stays on the same URL (no 303 redirect because the response is a 400
    # with the form re-rendered).
    expect(ws_page.get_by_test_id("stock-out-error")).to_contain_text(
        "Not enough stock"
    )
    # Inputs preserved.
    expect(ws_page.get_by_test_id("stock-out-qty-input")).to_have_value("1000")
    expect(ws_page.get_by_test_id("stock-out-reason-input")).to_have_value(
        "Way too much"
    )
    # current_qty unchanged (still 70).
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("70.0000")

    # Step 6c (M4): Workshop deep-links to the adjust form. Leg 1 is a positive
    # adjustment (increase) which creates a new cost layer and bumps qty;
    # leg 2 is a negative adjustment (decrease) which consumes FIFO.
    ws_page.goto(f"{app_server}/admin/items/{item_id}/adjust")
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("70.0000")

    # Leg 1: increase by 20 at unit_cost 3.00 → current_qty 90, total_cost 60.
    ws_page.get_by_test_id("stock-adjust-direction-input").select_option(
        "increase"
    )
    ws_page.get_by_test_id("stock-adjust-qty-input").fill("20")
    ws_page.get_by_test_id("stock-adjust-unit-cost-input").fill("3.00")
    ws_page.get_by_test_id("stock-adjust-reason-input").fill("found extra")
    ws_page.get_by_test_id("stock-adjust-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/adjust")

    expect(ws_page.get_by_test_id("flash")).to_contain_text("Casting alloy")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("+20")
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("90.0000")
    inc_row = ws_page.locator('[data-testid="movement-row"]').first
    expect(inc_row).to_contain_text("found extra")
    expect(inc_row).to_contain_text("20")
    expect(inc_row).to_contain_text("60")  # 20 * 3.00

    # Leg 2: decrease by 15 → current_qty 75. FIFO consumes from the oldest
    # layer (the original 100 @ 2.50, which had 70 left after the OUT step), so
    # total_cost = 15 * 2.50 = 37.50.
    ws_page.get_by_test_id("stock-adjust-direction-input").select_option(
        "decrease"
    )
    ws_page.get_by_test_id("stock-adjust-qty-input").fill("15")
    ws_page.get_by_test_id("stock-adjust-unit-cost-input").fill("")
    ws_page.get_by_test_id("stock-adjust-reason-input").fill("damaged batch")
    ws_page.get_by_test_id("stock-adjust-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/adjust")

    expect(ws_page.get_by_test_id("flash")).to_contain_text("Casting alloy")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("-15")
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("75.0000")
    dec_row = ws_page.locator('[data-testid="movement-row"]').first
    expect(dec_row).to_contain_text("damaged batch")
    expect(dec_row).to_contain_text("15")

    # Step 6d (M6): Workshop visits the item detail page and sees the
    # consolidated read view: open layers + paginated full timeline + per-row
    # layer breakdown for the OUT and the negative-adjustment.
    ws_page.goto(f"{app_server}/admin/items/{item_id}/detail")
    expect(ws_page.get_by_test_id("item-detail-heading")).to_contain_text(
        "Casting alloy"
    )
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("75.0000")
    # Two open layers remain: the original IN @ 2.50 (with 55 left after OUT
    # consumed 30 + adjust-decrease consumed 15) + the adjust-increase @ 3.00
    # (with all 20 still open). open_value = 55*2.5 + 20*3 = 197.50.
    expect(ws_page.get_by_test_id("item-open-value")).to_contain_text("197.5")
    expect(
        ws_page.locator('[data-testid="cost-layer-row"]')
    ).to_have_count(2)
    # The timeline shows all four movements (newest first).
    expect(
        ws_page.locator('[data-testid="timeline-row"]')
    ).to_have_count(4)
    # The OUT and the adjust-decrease both produce layer-breakdown rows; the
    # IN and the adjust-increase do not (each "row" wraps a <ul>, so we
    # expect 2 breakdown blocks total).
    expect(
        ws_page.locator('[data-testid="layer-breakdown"]')
    ).to_have_count(2)
    # Single page footer (well under 20 movements).
    expect(
        ws_page.get_by_test_id("pagination-single-page")
    ).to_be_visible()
    # Workshop sees the in/out/adjust action links but not the edit link.
    expect(ws_page.get_by_test_id("stock-in-link")).to_be_visible()
    expect(ws_page.get_by_test_id("edit-item-link")).to_have_count(0)

    # Step 6e (M5): Workshop transfers the item from "Movements From Bench"
    # to "Movements To Storage". The form re-renders with the flash and the
    # recent-movements row shows a TRANSFER. current_qty unchanged at 75
    # (transfers don't change quantity or valuation), and the timeline gains
    # a row but no new layer-breakdown block.
    ws_page.goto(f"{app_server}/admin/items/{item_id}/transfer")
    expect(ws_page.get_by_test_id("stock-transfer-from-location")).to_contain_text(
        "Movements From Bench"
    )
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("75.0000")
    ws_page.get_by_test_id("stock-transfer-to-location-input").select_option(
        label="Movements To Storage"
    )
    ws_page.get_by_test_id("stock-transfer-qty-input").fill("75")
    ws_page.get_by_test_id("stock-transfer-reason-input").fill("end of shift")
    ws_page.get_by_test_id("stock-transfer-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/transfer")

    expect(ws_page.get_by_test_id("flash")).to_contain_text("Transfer recorded")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("Movements From Bench")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("Movements To Storage")
    # The newest movement row is the TRANSFER; it has no total_cost (renders
    # as a dash) and no direction sign.
    transfer_row = ws_page.locator('[data-testid="movement-row"]').first
    expect(transfer_row).to_contain_text("transfer")
    expect(transfer_row).to_contain_text("75")
    # The from-location label has flipped — the form now shows the new
    # current-from when re-rendered (which is the to-location of this leg).
    expect(ws_page.get_by_test_id("stock-transfer-from-location")).to_contain_text(
        "Movements To Storage"
    )

    # Re-visit detail: timeline now has 5 rows; still 2 layer-breakdown blocks
    # (TRANSFER doesn't add one). current_qty unchanged at 75.
    ws_page.goto(f"{app_server}/admin/items/{item_id}/detail")
    expect(ws_page.get_by_test_id("item-current-qty")).to_have_text("75.0000")
    expect(
        ws_page.locator('[data-testid="timeline-row"]')
    ).to_have_count(5)
    expect(
        ws_page.locator('[data-testid="layer-breakdown"]')
    ).to_have_count(2)

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

    for loc_name in ("Movements From Bench", "Movements To Storage"):
        cleanup_page.goto(f"{app_server}/admin/locations")
        loc_row = cleanup_page.locator(
            '[data-testid="location-row"]', has_text=loc_name
        )
        loc_row.get_by_test_id("archive-location").click()
        cleanup_page.wait_for_url(f"{app_server}/admin/locations")

    cleanup_page.close()
    if cleanup_context is not context:
        cleanup_context.close()
