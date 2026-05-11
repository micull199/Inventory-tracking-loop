"""End-to-end walk for the check-out + check-in flow (C2 + C3 + C4).

A manager creates a flagged unique-tracked item with two units. A workshop
user navigates via the role-aware nav into the item's read-only view, follows
the new ``checkout-link`` → ``/admin/items/{id}/checkout`` form, picks one of
the two units, fills a *backdated* expected return date (yesterday) and a
condition note, submits, and asserts:

- The flash includes the item name + the picked unit's serial.
- The status block now lists the just-checked-out unit with the workshop
  user's email as the holder.
- The next form render's unit ``<select>`` no longer offers the picked unit
  (it's now in the open-checkouts set, not the available set).

C4 leg: the manager signs in, navigates ``nav-checkouts`` → the cross-item
oversight view, and asserts:

- The default ``open`` tab lists the just-checked-out CHK-A row.
- The ``overdue`` tab also lists it (because the expected_return was set to
  yesterday, the row is past-due — overdue badge visible with days_overdue
  ≥ 1).
- The dashboard's ``dashboard-overdue-checkouts`` widget reads "1".

Then the workshop user clicks the inline "Check in" button on the open-row,
optionally adds a return note, and asserts:

- The flash includes "Checked in" + the item name + the unit serial.
- The status block is now hidden (no open checkouts left).
- CHK-A is back in the available ``<select>``.
- The C4 ``open`` tab is now empty; the dashboard widget reads "0".

Cleanup archives the units + item + taxonomy category so downstream walks see
empty active lists.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def test_workshop_checks_out_a_unique_tracked_unit(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: pending workshop + pending manager sign up via dev-login.
    for email, sub in (
        ("checkouts-ws@uc.test", "g-e2e-checkouts-ws"),
        ("checkouts-mgr@uc.test", "g-e2e-checkouts-mgr"),
    ):
        page = context.new_page()
        _dev_login(page, app_server, email=email, sub=sub)
        expect(page.get_by_test_id("pending-heading")).to_be_visible()
        page.close()

    # Step 2: admin promotes both users.
    _admin_promote(app_server, context, email="checkouts-ws@uc.test", role="workshop")
    _admin_promote(app_server, context, email="checkouts-mgr@uc.test", role="manager")

    # Step 3: manager signs in, creates a category + a flagged unique-tracked
    # item, and adds two units.
    mgr_context = context.browser.new_context() if context.browser else context
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="checkouts-mgr@uc.test",
        sub="g-e2e-checkouts-mgr",
        name="Checkouts Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    mgr_page.get_by_test_id("new-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Checkouts E2E Cat")
    mgr_page.get_by_test_id("taxonomy-archetype-input").select_option("bulk")
    mgr_page.get_by_test_id("taxonomy-sku-prefix-input").fill("CHE")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Create the item: flagged for checkout, unique-tracked.
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("CHK-MOULD-1")
    mgr_page.get_by_test_id("item-name-input").fill("Wax mould A")
    pick_item_category(mgr_page, "Checkouts E2E Cat")
    mgr_page.get_by_test_id("item-unit-input").fill("ea")
    mgr_page.get_by_test_id("item-tracking-mode-input").select_option("unique")
    mgr_page.get_by_test_id("item-requires-checkout-input").check()
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    item_row = mgr_page.locator('[data-testid="item-row"]', has_text="CHK-MOULD-1")
    item_id = item_row.get_attribute("data-item-id")
    assert item_id is not None

    # Add two units.
    item_row.get_by_test_id("edit-item").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/") and u.endswith("/edit")
    )
    mgr_page.get_by_test_id("manage-units").click()
    mgr_page.wait_for_url(lambda u: u.endswith("/units"))
    for serial in ("CHK-A", "CHK-B"):
        mgr_page.get_by_test_id("new-item-unit").click()
        mgr_page.wait_for_url(lambda u: "/units/new" in u)
        mgr_page.get_by_test_id("item-unit-serial-input").fill(serial)
        mgr_page.get_by_test_id("item-unit-submit").click()
        mgr_page.wait_for_url(lambda u: u.endswith("/units"))
    mgr_page.close()

    # Step 4: workshop signs in.
    ws_context = context.browser.new_context() if context.browser else context
    ws_page = ws_context.new_page()
    _dev_login(
        ws_page,
        app_server,
        email="checkouts-ws@uc.test",
        sub="g-e2e-checkouts-ws",
        name="Checkouts Workshop",
    )
    expect(ws_page.get_by_test_id("welcome")).to_be_visible()

    # I1c: workshop browses items via the nav, finds the flagged item,
    # opens the read-only view, follows the new "Check out →" link.
    ws_page.get_by_test_id("nav-items").click()
    ws_page.wait_for_url(f"{app_server}/admin/items")
    item_row_ws = ws_page.locator('[data-testid="item-row"]', has_text="CHK-MOULD-1")
    expect(item_row_ws).to_be_visible()
    item_row_ws.get_by_test_id("view-item").click()
    ws_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/") and u.endswith("/edit")
    )
    expect(ws_page.get_by_test_id("checkout-link")).to_be_visible()
    ws_page.get_by_test_id("checkout-link").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/checkout")

    # No open checkouts yet.
    expect(ws_page.get_by_test_id("checkout-status-block")).not_to_be_visible()

    # Step 5: pick a unit, fill the form, submit. expected_return is set to
    # *yesterday* so the row appears as overdue in the C4 oversight view.
    yesterday = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%d")
    ws_page.get_by_test_id("checkout-unit-input").select_option(label="CHK-A")
    ws_page.get_by_test_id("checkout-expected-return-input").fill(yesterday)
    ws_page.get_by_test_id("checkout-note-input").fill("careful with the lip")
    ws_page.get_by_test_id("checkout-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/checkout")

    # Step 6: assert the flash + status block + unit removed from select.
    expect(ws_page.get_by_test_id("flash")).to_contain_text("Wax mould A")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("CHK-A")

    expect(ws_page.get_by_test_id("checkout-status-block")).to_be_visible()
    open_row = ws_page.locator('[data-testid="checkout-open-row"]').first
    expect(open_row).to_contain_text("checkouts-ws@uc.test")
    expect(open_row.get_by_test_id("checkout-open-unit")).to_have_text("CHK-A")

    # The picked unit is no longer in the available <select>; CHK-B still is.
    available = ws_page.get_by_test_id("checkout-unit-input")
    expect(available.locator('option[value=""]')).to_have_count(1)  # placeholder
    options_text = available.inner_text()
    assert "CHK-A" not in options_text
    assert "CHK-B" in options_text
    ws_page.close()

    # Step 6a (C4): manager signs in, visits the cross-item checkouts view +
    # the dashboard widget. CHK-A should appear as overdue.
    c4_context = context.browser.new_context() if context.browser else context
    c4_page = c4_context.new_page()
    _dev_login(
        c4_page,
        app_server,
        email="checkouts-mgr@uc.test",
        sub="g-e2e-checkouts-mgr",
        name="Checkouts Manager",
    )
    expect(c4_page.get_by_test_id("welcome")).to_be_visible()

    # Default `open` tab — CHK-A row is visible.
    c4_page.get_by_test_id("nav-checkouts").click()
    c4_page.wait_for_url(f"{app_server}/admin/checkouts")
    expect(c4_page.get_by_test_id("checkouts-admin-heading")).to_be_visible()
    expect(c4_page.get_by_test_id("checkouts-open-count")).to_have_text("1")
    expect(c4_page.get_by_test_id("checkouts-overdue-count")).to_have_text("1")
    overdue_row = c4_page.locator('[data-testid="checkouts-row"]').first
    expect(overdue_row).to_contain_text("CHK-MOULD-1")
    expect(overdue_row).to_contain_text("CHK-A")
    expect(overdue_row).to_contain_text("checkouts-ws@uc.test")
    expect(overdue_row.get_by_test_id("checkouts-row-overdue-badge")).to_be_visible()
    assert overdue_row.get_attribute("data-overdue") == "true"

    # `overdue` tab — same row.
    c4_page.get_by_test_id("filter-overdue").click()
    c4_page.wait_for_url(f"{app_server}/admin/checkouts?show=overdue")
    expect(c4_page.locator('[data-testid="checkouts-row"]')).to_have_count(1)

    # Dashboard widget now reads 1.
    c4_page.get_by_test_id("nav-dashboard").click()
    c4_page.wait_for_url(f"{app_server}/admin/dashboard")
    expect(c4_page.get_by_test_id("dashboard-overdue-checkouts")).to_have_text("1")
    c4_page.close()
    if c4_context is not context:
        c4_context.close()

    # Step 6b (C3): workshop signs back in and checks the unit back in via
    # the inline form on the open-row, with an optional return note. The note
    # input lives inside a collapsed <details>; expand it before filling.
    ws_page = ws_context.new_page()
    _dev_login(
        ws_page,
        app_server,
        email="checkouts-ws@uc.test",
        sub="g-e2e-checkouts-ws",
        name="Checkouts Workshop",
    )
    ws_page.goto(f"{app_server}/admin/items/{item_id}/checkout")
    open_row = ws_page.locator('[data-testid="checkout-open-row"]').first
    open_row.locator("details summary").click()
    open_row.get_by_test_id("checkout-return-note-input").fill(
        "returned clean, ready for next pour"
    )
    open_row.get_by_test_id("checkout-return-submit").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/checkout")

    expect(ws_page.get_by_test_id("flash")).to_contain_text("Checked in")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("Wax mould A")
    expect(ws_page.get_by_test_id("flash")).to_contain_text("CHK-A")

    # Status block is gone — no more open checkouts on this item.
    expect(ws_page.get_by_test_id("checkout-status-block")).not_to_be_visible()

    # CHK-A is back in the available <select>; CHK-B is still there too.
    available_after = ws_page.get_by_test_id("checkout-unit-input")
    options_after = available_after.inner_text()
    assert "CHK-A" in options_after
    assert "CHK-B" in options_after
    ws_page.close()

    # Step 6c (C4 follow-up): re-visit the cross-item view + the dashboard
    # widget — both should now be empty / zero.
    c4_after_context = context.browser.new_context() if context.browser else context
    c4_after_page = c4_after_context.new_page()
    _dev_login(
        c4_after_page,
        app_server,
        email="checkouts-mgr@uc.test",
        sub="g-e2e-checkouts-mgr",
        name="Checkouts Manager",
    )
    c4_after_page.goto(f"{app_server}/admin/checkouts")
    expect(c4_after_page.get_by_test_id("checkouts-open-count")).to_have_text("0")
    expect(c4_after_page.get_by_test_id("checkouts-overdue-count")).to_have_text("0")
    expect(c4_after_page.get_by_test_id("checkouts-admin-empty")).to_be_visible()
    c4_after_page.goto(f"{app_server}/admin/dashboard")
    expect(c4_after_page.get_by_test_id("dashboard-overdue-checkouts")).to_have_text("0")
    c4_after_page.close()
    if c4_after_context is not context:
        c4_after_context.close()

    # Step 7: cleanup. Manager signs back in and archives the units, item, cat.
    cleanup_context = context.browser.new_context() if context.browser else context
    cleanup_page = cleanup_context.new_page()
    _dev_login(
        cleanup_page,
        app_server,
        email="checkouts-mgr@uc.test",
        sub="g-e2e-checkouts-mgr",
        name="Checkouts Manager",
    )

    # Archive the item directly — units stay on the archived item.
    cleanup_page.goto(f"{app_server}/admin/items")
    cleanup_row = cleanup_page.locator('[data-testid="item-row"]', has_text="CHK-MOULD-1")
    cleanup_row.get_by_test_id("archive-item").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/items")

    # Archive the taxonomy.
    cleanup_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = cleanup_page.locator('[data-testid="taxonomy-row"]', has_text="Checkouts E2E Cat")
    cat_row.get_by_test_id("archive-taxonomy").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/taxonomy")
    cleanup_page.close()
    if cleanup_context is not context:
        cleanup_context.close()
