"""End-to-end walk for the check-out flow (C2).

A manager creates a flagged unique-tracked item with two units. A workshop
user navigates via the role-aware nav into the item's read-only view, follows
the new ``checkout-link`` → ``/admin/items/{id}/checkout`` form, picks one of
the two units, fills an expected return date and a condition note, submits,
and asserts:

- The flash includes the item name + the picked unit's serial.
- The status block now lists the just-checked-out unit with the workshop
  user's email as the holder.
- The next form render's unit ``<select>`` no longer offers the picked unit
  (it's now in the open-checkouts set, not the available set).

Cleanup archives the units + item + taxonomy category so downstream walks see
empty active lists.
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


def _admin_promote(
    base_url: str,
    context: BrowserContext,
    *,
    email: str,
    role: str,
) -> None:
    """Sign in as the bootstrap admin and promote ``email`` → ``role`` + active."""
    admin_context = (
        context.browser.new_context() if context.browser else context
    )
    admin_page = admin_context.new_page()
    _dev_login(
        admin_page,
        base_url,
        email="admin@uc.test",
        sub="g-e2e-admin",
        name="Seed Admin",
    )
    admin_page.goto(f"{base_url}/admin/users")
    row = admin_page.locator(
        '[data-testid="user-row"]', has_text=email
    )
    row.locator('[data-testid="role-select"]').select_option(role)
    row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{base_url}/admin/users")
    promoted = admin_page.locator(
        '[data-testid="user-row"]', has_text=email
    )
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
    _admin_promote(
        app_server, context, email="checkouts-ws@uc.test", role="workshop"
    )
    _admin_promote(
        app_server, context, email="checkouts-mgr@uc.test", role="manager"
    )

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
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Create the item: flagged for checkout, unique-tracked.
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("CHK-MOULD-1")
    mgr_page.get_by_test_id("item-name-input").fill("Wax mould A")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Checkouts E2E Cat"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("ea")
    mgr_page.get_by_test_id("item-tracking-mode-input").select_option("unique")
    mgr_page.get_by_test_id("item-requires-checkout-input").check()
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="CHK-MOULD-1"
    )
    item_id = item_row.get_attribute("data-item-id")
    assert item_id is not None

    # Add two units.
    item_row.get_by_test_id("edit-item").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    mgr_page.get_by_test_id("manage-units").click()
    mgr_page.wait_for_url(
        lambda u: u.endswith("/units")
    )
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
    item_row_ws = ws_page.locator(
        '[data-testid="item-row"]', has_text="CHK-MOULD-1"
    )
    expect(item_row_ws).to_be_visible()
    item_row_ws.get_by_test_id("view-item").click()
    ws_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    expect(ws_page.get_by_test_id("checkout-link")).to_be_visible()
    ws_page.get_by_test_id("checkout-link").click()
    ws_page.wait_for_url(f"{app_server}/admin/items/{item_id}/checkout")

    # No open checkouts yet.
    expect(ws_page.get_by_test_id("checkout-status-block")).not_to_be_visible()

    # Step 5: pick a unit, fill the form, submit.
    ws_page.get_by_test_id("checkout-unit-input").select_option(label="CHK-A")
    ws_page.get_by_test_id("checkout-expected-return-input").fill("2026-06-15")
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

    # Step 7: cleanup. Manager signs back in and archives the units, item, cat.
    cleanup_context = (
        context.browser.new_context() if context.browser else context
    )
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
    cleanup_row = cleanup_page.locator(
        '[data-testid="item-row"]', has_text="CHK-MOULD-1"
    )
    cleanup_row.get_by_test_id("archive-item").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/items")

    # Archive the taxonomy.
    cleanup_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = cleanup_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Checkouts E2E Cat"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    cleanup_page.wait_for_url(f"{app_server}/admin/taxonomy")
    cleanup_page.close()
    if cleanup_context is not context:
        cleanup_context.close()
