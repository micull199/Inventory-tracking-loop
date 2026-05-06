"""End-to-end walk for the reorder dashboard (PO1).

A manager creates a supplier + a category + two items: one already at zero
stock against a positive threshold (below threshold), one with stock above
threshold. The manager visits the reorder dashboard via the nav link, sees
*only* the below-threshold item in the supplier's group, asserts the
threshold + deficit cells, clicks the stock-in link, records enough stock to
clear the threshold, and returns to the dashboard via the nav link to see
the empty-state.

Cleanup at the end archives both items + the supplier + the category so
downstream walks see clean active lists.
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


def test_manager_reorder_dashboard_walk(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future manager signs in (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="reorder-mgr@uc.test",
        sub="g-e2e-reorder-mgr",
        name="Reorder Manager",
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
        '[data-testid="user-row"]', has_text="reorder-mgr@uc.test"
    )
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    promoted_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="reorder-mgr@uc.test"
    )
    promoted_row.locator('[data-testid="status-select"]').select_option("active")
    promoted_row.locator('[data-testid="status-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")
    admin_page.close()
    if admin_context is not context:
        admin_context.close()

    # Step 3: Manager signs in and creates a supplier + category + two items.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="reorder-mgr@uc.test",
        sub="g-e2e-reorder-mgr",
        name="Reorder Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()
    expect(mgr_page.get_by_test_id("nav-reorder")).to_be_visible()

    # Supplier.
    mgr_page.goto(f"{app_server}/admin/suppliers/new")
    mgr_page.get_by_test_id("supplier-name-input").fill("Reorder Bullion Co")
    mgr_page.get_by_test_id("supplier-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers")

    # Category.
    mgr_page.goto(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Reorder E2E Cat")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Item 1: needs reordering (current_qty=0, threshold=10).
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("RD-LOW")
    mgr_page.get_by_test_id("item-name-input").fill("Low stock alloy")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Reorder E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-supplier-input").select_option(
        label="Reorder Bullion Co"
    )
    mgr_page.get_by_test_id("item-reorder-threshold-input").fill("10")
    mgr_page.get_by_test_id("item-reorder-qty-input").fill("100")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    low_row = mgr_page.locator('[data-testid="item-row"]', has_text="RD-LOW")
    low_id = low_row.get_attribute("data-item-id")
    assert low_id is not None

    # Item 2: another item bound to the same supplier — we'll keep it ABOVE
    # threshold by stocking it in. Threshold=10, after stock-in current=50.
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("RD-OK")
    mgr_page.get_by_test_id("item-name-input").fill("OK alloy")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Reorder E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-supplier-input").select_option(
        label="Reorder Bullion Co"
    )
    mgr_page.get_by_test_id("item-reorder-threshold-input").fill("10")
    mgr_page.get_by_test_id("item-reorder-qty-input").fill("100")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    ok_row = mgr_page.locator('[data-testid="item-row"]', has_text="RD-OK")
    ok_id = ok_row.get_attribute("data-item-id")
    assert ok_id is not None

    # Step 4: Stock in the OK item to push it above threshold.
    mgr_page.goto(f"{app_server}/admin/items/{ok_id}/in")
    mgr_page.get_by_test_id("stock-in-qty-input").fill("50")
    mgr_page.get_by_test_id("stock-in-unit-cost-input").fill("1.00")
    mgr_page.get_by_test_id("stock-in-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items/{ok_id}/in")
    expect(mgr_page.get_by_test_id("item-current-qty")).to_have_text("50.0000")

    # Step 5: Visit the reorder dashboard via the nav link.
    mgr_page.get_by_test_id("nav-reorder").click()
    mgr_page.wait_for_url(f"{app_server}/admin/reorder")

    # Only the below-threshold item should be listed; the above-threshold
    # item should not.
    expect(mgr_page.get_by_test_id("reorder-empty")).not_to_be_visible()
    expect(
        mgr_page.locator('[data-testid="reorder-row"]', has_text="RD-LOW")
    ).to_be_visible()
    expect(
        mgr_page.locator('[data-testid="reorder-row"]', has_text="RD-OK")
    ).to_have_count(0)

    # The supplier group label is the supplier name (active, no suffix).
    expect(
        mgr_page.locator(
            '[data-testid="reorder-supplier-name"]',
            has_text="Reorder Bullion Co",
        )
    ).to_be_visible()
    low_reorder_row = mgr_page.locator(
        '[data-testid="reorder-row"]', has_text="RD-LOW"
    )
    expect(low_reorder_row.get_by_test_id("reorder-current-qty")).to_have_text(
        "0.0000"
    )
    expect(low_reorder_row.get_by_test_id("reorder-threshold")).to_have_text(
        "10.0000"
    )
    expect(low_reorder_row.get_by_test_id("reorder-deficit")).to_have_text(
        "10.0000"
    )

    # Step 6 (PO2): The supplier group renders a Draft PO button. Click it →
    # creates a draft PO with one line for RD-LOW + redirects to the detail page.
    expect(mgr_page.get_by_test_id("reorder-draft-po-button")).to_be_visible()
    mgr_page.get_by_test_id("reorder-draft-po-button").click()
    # The redirect target is /admin/purchase-orders/{po_id}; we don't know the
    # id yet, so wait on the heading.
    expect(mgr_page.get_by_test_id("po-detail-heading")).to_be_visible()
    # Capture the PO id from the URL for later cancel-vs-detail assertions.
    po_url = mgr_page.url
    po_id = po_url.rsplit("/", 1)[-1]
    # One line — the RD-LOW item.
    expect(mgr_page.locator('[data-testid="po-line-row"]')).to_have_count(1)
    expect(mgr_page.get_by_test_id("po-supplier-name")).to_have_text(
        "Reorder Bullion Co"
    )
    expect(mgr_page.get_by_test_id("po-status-badge")).to_have_text("draft")
    expect(mgr_page.get_by_test_id("po-line-sku")).to_have_text("RD-LOW")
    # PO2b: drafts render the edit form. Inputs replace the text cells.
    expect(mgr_page.get_by_test_id("po-edit-form")).to_be_visible()
    expect(mgr_page.get_by_test_id("po-edit-qty-input")).to_have_value(
        "100.0000"
    )
    # RD-LOW had no prior cost layer when the PO was drafted → empty input.
    expect(mgr_page.get_by_test_id("po-edit-cost-input")).to_have_value("")

    # Step 6b (PO2b): Edit the line + add notes + save.
    mgr_page.get_by_test_id("po-edit-qty-input").fill("120")
    mgr_page.get_by_test_id("po-edit-cost-input").fill("1.50")
    mgr_page.get_by_test_id("po-edit-notes-input").fill("rush order")
    mgr_page.get_by_test_id("po-edit-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/purchase-orders/{po_id}")
    # The redirect lands back on the same detail page (still draft) with the
    # new values pre-filled.
    expect(mgr_page.get_by_test_id("po-edit-qty-input")).to_have_value(
        "120.0000"
    )
    expect(mgr_page.get_by_test_id("po-edit-cost-input")).to_have_value(
        "1.5000"
    )
    expect(mgr_page.get_by_test_id("po-edit-notes-input")).to_have_value(
        "rush order"
    )

    # Step 6b-pdf (PO3): Download the PDF for the draft PO via the link on
    # the detail page. Use the page's request context so we ride the same
    # session cookie + CSRF state as a click would, but avoid opening a new
    # tab (Chromium's built-in PDF viewer makes load events non-deterministic).
    pdf_link = mgr_page.get_by_test_id("po-pdf-link")
    expect(pdf_link).to_be_visible()
    pdf_href = pdf_link.get_attribute("href") or ""
    assert pdf_href.endswith(f"/admin/purchase-orders/{po_id}/pdf")
    pdf_resp = mgr_page.request.get(f"{app_server}{pdf_href}")
    assert pdf_resp.status == 200
    assert pdf_resp.headers["content-type"].startswith("application/pdf")
    pdf_body = pdf_resp.body()
    assert pdf_body[:4] == b"%PDF"
    # Sanity-check the supplier + line are in the byte stream.
    assert b"Reorder Bullion Co" in pdf_body
    assert b"RD-LOW" in pdf_body

    # Visit the PO list to confirm the new PO is there.
    mgr_page.get_by_test_id("nav-pos").click()
    mgr_page.wait_for_url(f"{app_server}/admin/purchase-orders")
    expect(mgr_page.locator('[data-testid="po-row"]')).to_have_count(1)
    expect(mgr_page.get_by_test_id("po-row-supplier")).to_have_text(
        "Reorder Bullion Co"
    )
    expect(mgr_page.get_by_test_id("po-row-line-count")).to_have_text("1")

    # Step 6c (PO2b): Click into the PO and cancel it.
    mgr_page.get_by_test_id("po-row-detail-link").click()
    mgr_page.wait_for_url(f"{app_server}/admin/purchase-orders/{po_id}")
    expect(mgr_page.get_by_test_id("po-cancel-submit")).to_be_visible()
    mgr_page.get_by_test_id("po-cancel-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/purchase-orders/{po_id}")
    # Status now reads "cancelled" + the edit form is gone + the read-only
    # banner appears.
    expect(mgr_page.get_by_test_id("po-status-badge")).to_have_text("cancelled")
    expect(mgr_page.get_by_test_id("po-readonly-banner")).to_be_visible()
    expect(mgr_page.get_by_test_id("po-edit-form")).to_have_count(0)
    # PO3: cancelled POs hide the PDF link (the route also 400s for them).
    expect(mgr_page.get_by_test_id("po-pdf-link")).to_have_count(0)

    # Back to reorder dashboard via nav.
    mgr_page.get_by_test_id("nav-reorder").click()
    mgr_page.wait_for_url(f"{app_server}/admin/reorder")
    # The PO doesn't change current_qty, so RD-LOW is still listed.
    low_reorder_row = mgr_page.locator(
        '[data-testid="reorder-row"]', has_text="RD-LOW"
    )
    expect(low_reorder_row).to_be_visible()

    # Step 7: Click the stock-in link on the below-threshold row → land on
    # /admin/items/{id}/in for that item.
    low_reorder_row.get_by_test_id("reorder-stock-in-link").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items/{low_id}/in")

    # Stock in 25 (above the threshold of 10).
    mgr_page.get_by_test_id("stock-in-qty-input").fill("25")
    mgr_page.get_by_test_id("stock-in-unit-cost-input").fill("1.00")
    mgr_page.get_by_test_id("stock-in-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items/{low_id}/in")
    expect(mgr_page.get_by_test_id("item-current-qty")).to_have_text("25.0000")

    # Step 8: Back to the reorder dashboard via the nav link → empty state.
    mgr_page.get_by_test_id("nav-reorder").click()
    mgr_page.wait_for_url(f"{app_server}/admin/reorder")
    expect(mgr_page.get_by_test_id("reorder-empty")).to_be_visible()
    expect(mgr_page.locator('[data-testid="reorder-row"]')).to_have_count(0)

    # Step 9: Cleanup — archive both items + the supplier + the category so
    # downstream walks see clean active lists. (The draft PO created in step 6
    # remains in the test DB; that's fine — each test session uses a fresh
    # schema, and there's no cancel-PO route in PO2 yet.)
    mgr_page.goto(f"{app_server}/admin/items")
    for sku in ("RD-LOW", "RD-OK"):
        row = mgr_page.locator('[data-testid="item-row"]', has_text=sku)
        row.get_by_test_id("archive-item").click()
        mgr_page.wait_for_url(f"{app_server}/admin/items")

    mgr_page.goto(f"{app_server}/admin/suppliers")
    sup_row = mgr_page.locator(
        '[data-testid="supplier-row"]', has_text="Reorder Bullion Co"
    )
    sup_row.get_by_test_id("archive-supplier").click()
    mgr_page.wait_for_url(f"{app_server}/admin/suppliers")

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Reorder E2E Cat"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
