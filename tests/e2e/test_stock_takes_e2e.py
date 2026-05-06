"""End-to-end walk for ST1: Office user schedules a stock take.

Pending office user signs in, gets promoted by admin, navigates the
``nav-stock-takes`` link to the empty list, clicks "New stock take", picks
a category scope + a date + a note, submits, and asserts the list now shows
the row with the right scope label, scheduled date, and "scheduled" status
badge.

Cleanup archives the category created during the walk so subsequent walks
see a clean active list.
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


def test_office_schedules_a_stock_take(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: pending office + pending manager sign up via dev-login.
    for email, sub in (
        ("st-office@uc.test", "g-e2e-st-office"),
        ("st-mgr@uc.test", "g-e2e-st-mgr"),
    ):
        page = context.new_page()
        _dev_login(page, app_server, email=email, sub=sub)
        expect(page.get_by_test_id("pending-heading")).to_be_visible()
        page.close()

    # Step 2: admin promotes both. Manager creates a category that the office
    # user can pick as a scope.
    _admin_promote(app_server, context, email="st-mgr@uc.test", role="manager")
    _admin_promote(
        app_server, context, email="st-office@uc.test", role="office"
    )

    # Step 3: manager signs in and creates a category for the stock take to
    # scope to.
    mgr_context = (
        context.browser.new_context() if context.browser else context
    )
    mgr_page = mgr_context.new_page()
    _dev_login(
        mgr_page,
        app_server,
        email="st-mgr@uc.test",
        sub="g-e2e-st-mgr",
    )
    mgr_page.goto(f"{app_server}/admin/taxonomy")
    mgr_page.get_by_test_id("new-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("ST1 Materials")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Step 4: office user signs in and navigates to the empty stock-takes list
    # via the role-aware nav link.
    office_context = (
        context.browser.new_context() if context.browser else context
    )
    office_page = office_context.new_page()
    _dev_login(
        office_page,
        app_server,
        email="st-office@uc.test",
        sub="g-e2e-st-office",
    )
    expect(office_page.get_by_test_id("nav-stock-takes")).to_be_visible()
    office_page.get_by_test_id("nav-stock-takes").click()
    office_page.wait_for_url(f"{app_server}/admin/stock-takes")
    expect(office_page.get_by_test_id("stock-takes-empty")).to_be_visible()

    # Step 5: click "New stock take" → form.
    office_page.get_by_test_id("stock-takes-new-link").click()
    office_page.wait_for_url(f"{app_server}/admin/stock-takes/new")
    expect(office_page.get_by_test_id("stock-take-form")).to_be_visible()

    # Step 6: pick scope=node + the manager's category, set a date + a note,
    # submit. The radio inputs share a name so we click the "node" one
    # specifically.
    office_page.locator(
        'input[name="scope_type"][value="node"]'
    ).check()
    office_page.get_by_test_id("stock-take-scope-node-input").select_option(
        label="ST1 Materials"
    )
    office_page.get_by_test_id("stock-take-scheduled-for-input").fill(
        "2026-08-15"
    )
    office_page.get_by_test_id("stock-take-notes-input").fill(
        "End-of-quarter count"
    )
    office_page.get_by_test_id("stock-take-submit").click()
    office_page.wait_for_url(f"{app_server}/admin/stock-takes")

    # Step 7: assertions.
    expect(office_page.get_by_test_id("flash")).to_contain_text(
        "2026-08-15"
    )
    row = office_page.locator(
        '[data-testid="stock-takes-row"]', has_text="ST1 Materials"
    )
    expect(row).to_be_visible()
    expect(row.get_by_test_id("stock-takes-row-scope")).to_contain_text(
        "Category: ST1 Materials"
    )
    expect(
        row.get_by_test_id("stock-takes-row-scheduled-for")
    ).to_contain_text("2026-08-15")
    expect(
        row.get_by_test_id("stock-takes-row-status-badge")
    ).to_contain_text("scheduled")
    expect(row.get_by_test_id("stock-takes-row-created-by")).to_contain_text(
        "st-office@uc.test"
    )

    office_page.close()
    if office_context is not context:
        office_context.close()

    # Step 8: cleanup — manager archives the category so downstream walks see
    # a clean active list.
    mgr_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="ST1 Materials"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")
    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
