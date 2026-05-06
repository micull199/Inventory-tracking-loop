"""End-to-end walk for ST1 + ST2 + ST3: Office user schedules, counts, and
commits a stock take.

ST1 leg: a pending office user signs in, gets promoted by admin, navigates the
``nav-stock-takes`` link to the empty list, clicks "New stock take", picks
a category scope + a date + a note, submits, and asserts the list now shows
the row with the right scope label, scheduled date, and "scheduled" status
badge.

ST2 leg: the manager creates an item under the scoped category and seeds 50
units via the stock-in form. Office clicks the row's detail link, lands on
the scheduled detail page, sees the scope-preview row with the seeded item +
its current_qty, clicks "Start counting", lands on the in-progress detail
page with one count row pre-filled with system_qty=50.0000, fills counted=48,
submits, and asserts the variance row renders ``-2.0000``. The item's
``current_qty`` is unchanged after the count save (engine isolation).

ST3 leg: with the negative-variance row visible, office clicks the
``stock-take-commit-submit`` button (no unit_cost needed for a decrease).
The detail page redirects to the completed branch — the commit form is gone,
the line carries ``data-committed="true"`` + a ``Yes`` marker, and the
manager re-checks the stock-in form to verify ``current_qty`` has dropped
from 50.0000 to 48.0000 — the cost engine consumed 2 units FIFO from the
seeded layer.

Cleanup archives the item and the category so subsequent walks see a clean
active list.
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

    # ST2 prep: manager creates an item under the new category and seeds 50
    # units via the stock-in form so the scope=node count has something to
    # count.
    mgr_page.goto(f"{app_server}/admin/items/new")
    mgr_page.get_by_test_id("item-sku-input").fill("ST2-E2E-001")
    mgr_page.get_by_test_id("item-name-input").fill("Casting wax")
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="ST1 Materials"
    )
    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")
    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ST2-E2E-001"
    )
    item_id = item_row.get_attribute("data-item-id")
    assert item_id is not None
    mgr_page.goto(f"{app_server}/admin/items/{item_id}/in")
    mgr_page.get_by_test_id("stock-in-qty-input").fill("50")
    mgr_page.get_by_test_id("stock-in-unit-cost-input").fill("2.50")
    mgr_page.get_by_test_id("stock-in-reason-input").fill("Initial")
    mgr_page.get_by_test_id("stock-in-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items/{item_id}/in")
    expect(mgr_page.get_by_test_id("item-current-qty")).to_have_text(
        "50.0000"
    )

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

    # Step 7b (ST2): office clicks the detail link, lands on the scheduled
    # detail page, sees the scope preview with the seeded item, and starts
    # the count.
    row.get_by_test_id("stock-takes-row-detail-link").click()
    office_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/stock-takes/")
        and not u.endswith("/new")
    )
    expect(
        office_page.get_by_test_id("stock-take-detail-status-badge")
    ).to_have_attribute("data-status", "scheduled")
    scope_row = office_page.locator(
        '[data-testid="stock-take-scope-row"]', has_text="ST2-E2E-001"
    )
    expect(scope_row).to_be_visible()
    expect(
        scope_row.get_by_test_id("stock-take-scope-row-current-qty")
    ).to_contain_text("50.0000")

    office_page.get_by_test_id("stock-take-start-submit").click()
    office_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/stock-takes/")
    )
    expect(
        office_page.get_by_test_id("stock-take-detail-status-badge")
    ).to_have_attribute("data-status", "in_progress")
    count_row = office_page.locator(
        '[data-testid="stock-take-count-row"]', has_text="ST2-E2E-001"
    )
    expect(count_row).to_be_visible()
    expect(
        count_row.get_by_test_id("stock-take-count-system-qty")
    ).to_contain_text("50.0000")

    # Step 7c (ST2): office fills counted=48 and saves; the variance is -2.
    count_row.get_by_test_id("stock-take-count-counted-input").fill("48")
    office_page.get_by_test_id("stock-take-count-submit").click()
    office_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/stock-takes/")
    )
    count_row = office_page.locator(
        '[data-testid="stock-take-count-row"]', has_text="ST2-E2E-001"
    )
    expect(
        count_row.get_by_test_id("stock-take-count-variance")
    ).to_contain_text("-2")
    expect(
        office_page.get_by_test_id("stock-take-progress-counted")
    ).to_contain_text("1")
    expect(
        office_page.get_by_test_id("stock-take-progress-with-variance")
    ).to_contain_text("1")

    # Step 7d: engine isolation sanity — manager re-visits the items list and
    # confirms the seeded item's current_qty is unchanged at 50.0000 (ST2
    # records the count + variance but never touches the cost engine; ST3
    # commits below).
    mgr_page.goto(f"{app_server}/admin/items/{item_id}/in")
    expect(mgr_page.get_by_test_id("item-current-qty")).to_have_text(
        "50.0000"
    )

    # Step 7e (ST3): office sees the commit form with the negative-variance
    # row, clicks Commit count. The decrease consumes FIFO automatically;
    # no unit_cost input needed.
    expect(office_page.get_by_test_id("stock-take-commit-form")).to_be_visible()
    commit_row = office_page.locator(
        '[data-testid="stock-take-commit-row"]', has_text="ST2-E2E-001"
    )
    expect(commit_row).to_have_attribute("data-direction", "decrease")
    expect(
        commit_row.get_by_test_id("stock-take-commit-unit-cost-na")
    ).to_be_visible()
    office_page.get_by_test_id("stock-take-commit-submit").click()
    office_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/stock-takes/")
    )
    # Status flipped to completed; commit form gone; line marked committed.
    expect(
        office_page.get_by_test_id("stock-take-detail-status-badge")
    ).to_have_attribute("data-status", "completed")
    expect(office_page.get_by_test_id("stock-take-commit-form")).to_have_count(0)
    completed_row = office_page.locator(
        '[data-testid="stock-take-count-row"]', has_text="ST2-E2E-001"
    )
    expect(completed_row).to_have_attribute("data-committed", "true")

    # Step 7g (R4): office navigates to the dashboard and clicks the variance
    # trend link, asserts the just-committed stock take appears with the
    # expected per-side splits and the totals card shows one stock take.
    office_page.get_by_test_id("nav-dashboard").click()
    office_page.wait_for_url(f"{app_server}/admin/dashboard")
    office_page.get_by_test_id("dashboard-variance-trend-link").click()
    office_page.wait_for_url(
        f"{app_server}/admin/reports/variance-trend"
    )
    expect(
        office_page.get_by_test_id("variance-trend-stock-take-count")
    ).to_have_text("1")
    expect(
        office_page.get_by_test_id("variance-trend-total-negative-abs")
    ).to_contain_text("2")
    trend_row = office_page.locator('[data-testid="variance-trend-row"]')
    expect(trend_row).to_have_count(1)
    expect(
        trend_row.get_by_test_id("variance-trend-row-net")
    ).to_contain_text("-2")
    expect(
        trend_row.get_by_test_id("variance-trend-row-lines-with-variance")
    ).to_have_text("1")

    office_page.close()
    if office_context is not context:
        office_context.close()

    # Step 7f (ST3): manager re-checks the stock-in form — ``current_qty``
    # has dropped from 50.0000 to 48.0000 (engine consumed 2 units FIFO).
    mgr_page.goto(f"{app_server}/admin/items/{item_id}/in")
    expect(mgr_page.get_by_test_id("item-current-qty")).to_have_text(
        "48.0000"
    )

    # Step 8: cleanup — manager archives the seeded item then the category so
    # downstream walks see a clean active list.
    mgr_page.goto(f"{app_server}/admin/items")
    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ST2-E2E-001"
    )
    item_row.get_by_test_id("archive-item").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    mgr_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="ST1 Materials"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")
    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()
