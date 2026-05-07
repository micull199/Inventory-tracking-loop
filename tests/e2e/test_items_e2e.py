"""End-to-end manager workflow for items: promote → create / archive / unarchive an item.

The taxonomy e2e walk has its own coverage of categories + sub-cats + field
defs (see ``test_taxonomy_e2e.py``). This walk is intentionally independent:
it creates its own taxonomy node up-front and exercises *just* the items
flow. Splits per the S5 self-critique that flagged the taxonomy walk getting
too long.
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


def test_manager_creates_views_archives_and_unarchives_an_item(
    context: BrowserContext, app_server: str
) -> None:
    # Step 1: Future items manager signs in for the first time (lands pending).
    pending_page = context.new_page()
    _dev_login(
        pending_page,
        app_server,
        email="items-mgr@uc.test",
        sub="g-e2e-items-mgr",
        name="Items Manager",
    )
    expect(pending_page.get_by_test_id("pending-heading")).to_be_visible()
    pending_page.close()

    # Step 2: Admin signs in. Bootstrap promotion is one-shot per session DB
    # but idempotent thereafter — fine if other e2e tests already created the
    # admin. (See ``test_taxonomy_e2e.py`` for the same pattern.)
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
    pending_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="items-mgr@uc.test"
    )
    pending_row.locator('[data-testid="role-select"]').select_option("manager")
    pending_row.locator('[data-testid="role-submit"]').click()
    admin_page.wait_for_url(f"{app_server}/admin/users")

    promoted_row = admin_page.locator(
        '[data-testid="user-row"]', has_text="items-mgr@uc.test"
    )
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
        email="items-mgr@uc.test",
        sub="g-e2e-items-mgr",
        name="Items Manager",
    )
    expect(mgr_page.get_by_test_id("welcome")).to_be_visible()

    # Step 5: The Items link appears in the role-aware primary nav.
    expect(mgr_page.get_by_test_id("nav-items")).to_be_visible()

    # Step 6: Manager creates a category to put items in. Use a name unique to
    # this e2e so it doesn't clash with other e2e tests sharing the session DB.
    mgr_page.goto(f"{app_server}/admin/taxonomy")
    mgr_page.get_by_test_id("new-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    mgr_page.get_by_test_id("taxonomy-name-input").fill("Items E2E Cat")
    mgr_page.get_by_test_id("taxonomy-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Step 6b (I2): manager defines two field defs on the new category — a
    # required text "Alloy" and an optional select "Karat" with three options.
    # Items created under this category must satisfy "Alloy" or be rejected.
    items_e2e_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Items E2E Cat"
    )
    items_e2e_row.get_by_test_id("open-fields").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/taxonomy/")
        and u.endswith("/fields")
    )
    mgr_page.get_by_test_id("new-field-def").click()
    mgr_page.wait_for_url(
        lambda u: "/fields/new" in u and u.startswith(f"{app_server}/admin/taxonomy/")
    )
    mgr_page.get_by_test_id("field-def-name-input").fill("Alloy")
    mgr_page.get_by_test_id("field-def-required-input").check()
    mgr_page.get_by_test_id("field-def-submit").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/taxonomy/")
        and u.endswith("/fields")
    )

    mgr_page.get_by_test_id("new-field-def").click()
    mgr_page.wait_for_url(
        lambda u: "/fields/new" in u and u.startswith(f"{app_server}/admin/taxonomy/")
    )
    mgr_page.get_by_test_id("field-def-name-input").fill("Karat")
    mgr_page.get_by_test_id("field-def-type-input").select_option("select")
    mgr_page.get_by_test_id("field-def-options-input").fill("9\n14\n18")
    mgr_page.get_by_test_id("field-def-submit").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/taxonomy/")
        and u.endswith("/fields")
    )

    # Step 7: Click into Items.
    mgr_page.get_by_test_id("nav-items").click()
    mgr_page.wait_for_url(lambda u: u.startswith(f"{app_server}/admin/items"))
    expect(mgr_page.get_by_test_id("items-empty")).to_be_visible()

    # Step 8: Open the new-item form. The form starts with no category
    # picked — and no custom-field inputs visible. This is the user-reported
    # fix path: previously the inputs only rendered if the URL carried
    # ``?node_id=…``; now picking the category in the dropdown HTMX-swaps
    # the leaf's custom fields into ``#cf-container``.
    mgr_page.goto(f"{app_server}/admin/items/new")

    # Initial state: no category-fields fieldset because no leaf is picked.
    expect(
        mgr_page.get_by_test_id("item-custom-fields")
    ).not_to_be_visible()

    # SKU input is optional — leaving blank triggers server auto-gen.
    # Notes was removed entirely.
    expect(mgr_page.get_by_test_id("item-sku-input")).to_be_visible()
    expect(mgr_page.get_by_test_id("item-notes-input")).to_have_count(0)

    mgr_page.get_by_test_id("item-name-input").fill("Silver wire (e2e)")
    # Pick the leaf — HTMX fires on change, fetches the partial, swaps in
    # the custom-field inputs.
    mgr_page.get_by_test_id("item-category-input").select_option(
        label="Items E2E Cat"
    )
    # The fieldset and its inputs appear after the swap.
    expect(mgr_page.get_by_test_id("item-custom-fields")).to_be_visible()
    expect(mgr_page.get_by_test_id("item-cf-alloy-input")).to_be_visible()
    expect(mgr_page.get_by_test_id("item-cf-karat-input")).to_be_visible()

    mgr_page.get_by_test_id("item-unit-input").fill("g")
    mgr_page.get_by_test_id("item-reorder-threshold-input").fill("100")
    mgr_page.get_by_test_id("item-reorder-qty-input").fill("500")

    # I2: fill the custom fields the leaf inherits. "Alloy" required, "Karat"
    # optional select.
    mgr_page.get_by_test_id("item-cf-alloy-input").fill("silver")
    mgr_page.get_by_test_id("item-cf-karat-input").select_option("18")

    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    # Flash and row both visible.
    expect(mgr_page.get_by_test_id("flash")).to_contain_text("Silver wire")
    item_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ITE-0001"
    )
    expect(item_row).to_be_visible()
    expect(item_row.get_by_test_id("item-name")).to_have_text(
        "Silver wire (e2e)"
    )
    expect(item_row.get_by_test_id("item-category")).to_have_text(
        "Items E2E Cat"
    )

    # Step 8b (I2): re-open the edit form and verify the custom-field values
    # round-tripped (form pre-fills from item_field_values rows).
    item_row.get_by_test_id("edit-item").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    expect(mgr_page.get_by_test_id("item-cf-alloy-input")).to_have_value(
        "silver"
    )
    expect(mgr_page.get_by_test_id("item-cf-karat-input")).to_have_value("18")
    # Bounce back to the list without changes.
    mgr_page.goto(f"{app_server}/admin/items")

    # Step 9: Archive the item.
    item_row.get_by_test_id("archive-item").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")
    expect(
        mgr_page.locator('[data-testid="item-row"]', has_text="ITE-0001")
    ).to_have_count(0)

    # Step 10: Switch to archived tab — RM-E2E-001 is there.
    mgr_page.get_by_test_id("tab-archived").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items?show=archived")
    archived_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ITE-0001"
    )
    expect(archived_row).to_be_visible()

    # Step 11: Unarchive — back to active.
    archived_row.get_by_test_id("unarchive-item").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")
    restored_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ITE-0001"
    )
    expect(restored_row).to_be_visible()

    # Step 11b (I3): flip the item to unique-tracking, then add two units.
    # Verifies the unique-tracked half of DoD #2.
    restored_row.get_by_test_id("edit-item").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    mgr_page.get_by_test_id("item-tracking-mode-input").select_option("unique")
    mgr_page.get_by_test_id("item-submit").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")

    # Reopen the edit form and follow the "Manage units" link.
    item_row_after = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ITE-0001"
    )
    item_row_after.get_by_test_id("edit-item").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    mgr_page.get_by_test_id("manage-units").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )
    expect(mgr_page.get_by_test_id("item-units-empty")).to_be_visible()

    # Create the first unit.
    mgr_page.get_by_test_id("new-item-unit").click()
    mgr_page.wait_for_url(
        lambda u: "/units/new" in u
        and u.startswith(f"{app_server}/admin/items/")
    )
    mgr_page.get_by_test_id("item-unit-serial-input").fill("SN-001")
    mgr_page.get_by_test_id("item-unit-submit").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )
    expect(
        mgr_page.locator('[data-testid="item-unit-row"]', has_text="SN-001")
    ).to_be_visible()

    # Create the second unit.
    mgr_page.get_by_test_id("new-item-unit").click()
    mgr_page.wait_for_url(
        lambda u: "/units/new" in u
        and u.startswith(f"{app_server}/admin/items/")
    )
    mgr_page.get_by_test_id("item-unit-serial-input").fill("SN-002")
    mgr_page.get_by_test_id("item-unit-submit").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )

    # Archive one of the units, switch to archived tab, confirm it's there.
    sn001_row = mgr_page.locator(
        '[data-testid="item-unit-row"]', has_text="SN-001"
    )
    sn001_row.get_by_test_id("archive-item-unit").click()
    mgr_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )
    expect(
        mgr_page.locator('[data-testid="item-unit-row"]', has_text="SN-001")
    ).to_have_count(0)
    mgr_page.get_by_test_id("tab-archived").click()
    mgr_page.wait_for_url(
        lambda u: "/units?show=archived" in u
        and u.startswith(f"{app_server}/admin/items/")
    )
    expect(
        mgr_page.locator('[data-testid="item-unit-row"]', has_text="SN-001")
    ).to_be_visible()

    # Bounce back to the items list for the cleanup step.
    mgr_page.goto(f"{app_server}/admin/items")
    restored_row = mgr_page.locator(
        '[data-testid="item-row"]', has_text="ITE-0001"
    )

    # Step 12: Cleanup — archive the item *and* the taxonomy category so
    # downstream e2e tests (notably ``test_taxonomy_e2e``) start with an empty
    # *active* taxonomy list. The session-scoped DB means the alphabetical
    # filename order would otherwise let "Items E2E Cat" leak into other
    # tests' active views.
    restored_row.get_by_test_id("archive-item").click()
    mgr_page.wait_for_url(f"{app_server}/admin/items")
    mgr_page.goto(f"{app_server}/admin/taxonomy")
    cat_row = mgr_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Items E2E Cat"
    )
    cat_row.get_by_test_id("archive-taxonomy").click()
    mgr_page.wait_for_url(f"{app_server}/admin/taxonomy")
    expect(mgr_page.get_by_test_id("taxonomy-empty")).to_be_visible()

    mgr_page.close()
    if mgr_context is not context:
        mgr_context.close()


def test_admin_creates_qty_then_unique_item_end_to_end(
    context: BrowserContext, app_server: str
) -> None:
    """DoD #2: Admin creates an item end-to-end (qty → flip to unique → unit).

    Companion to ``test_manager_creates_views_archives_and_unarchives_an_item``.
    The manager walk already covers I2 (custom fields) + I3 (multi-unit). This
    walk's narrow job is to prove an *Admin* (not Manager) can drive one full
    create + manage + archive cycle through the UI — the manual-sanity-check
    that backs DoD #2 alongside the Admin integration coverage in
    ``tests/integration/test_items_routes.py::TestRoleEnforcement`` and
    ``::test_item_units_routes.py::TestUnitsRoleEnforcement``.
    """
    # Step 1: Admin signs in. Bootstrap promotion is one-shot per session DB
    # (safe to invoke even if an earlier e2e file already fired it).
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

    # Step 2: Admin creates a category named distinctly from the manager walk's
    # "Items E2E Cat" so the shared session DB doesn't collide.
    admin_page.goto(f"{app_server}/admin/taxonomy")
    admin_page.get_by_test_id("new-taxonomy").click()
    admin_page.wait_for_url(f"{app_server}/admin/taxonomy/new")
    admin_page.get_by_test_id("taxonomy-name-input").fill("Admin Items E2E Cat")
    admin_page.get_by_test_id("taxonomy-submit").click()
    admin_page.wait_for_url(f"{app_server}/admin/taxonomy")

    # Step 3: Click into Items via the nav. List is empty at this point because
    # the manager walk's cleanup archives its row.
    admin_page.get_by_test_id("nav-items").click()
    admin_page.wait_for_url(lambda u: u.startswith(f"{app_server}/admin/items"))
    expect(admin_page.get_by_test_id("items-empty")).to_be_visible()

    # Step 4: Open the new-item form *without* a ?node_id= deep-link — admin
    # picks the category from the select. This exercises the unfiltered path
    # that the manager walk skips. The category has no field defs, so no
    # custom-field block renders and no required-field 400 fires.
    admin_page.get_by_test_id("new-item").click()
    admin_page.wait_for_url(f"{app_server}/admin/items/new")
    # SKU input is optional — leave blank to trigger server auto-gen
    # ("ADM-0001" from "Admin Items E2E Cat").
    expect(admin_page.get_by_test_id("item-sku-input")).to_be_visible()
    admin_page.get_by_test_id("item-name-input").fill("Admin item (e2e)")
    admin_page.get_by_test_id("item-category-input").select_option(
        label="Admin Items E2E Cat"
    )
    admin_page.get_by_test_id("item-unit-input").fill("ea")
    admin_page.get_by_test_id("item-submit").click()
    admin_page.wait_for_url(f"{app_server}/admin/items")

    expect(admin_page.get_by_test_id("flash")).to_contain_text("Admin item")
    item_row = admin_page.locator(
        '[data-testid="item-row"]', has_text="ADM-0001"
    )
    expect(item_row).to_be_visible()
    expect(item_row.get_by_test_id("item-category")).to_have_text(
        "Admin Items E2E Cat"
    )

    # Step 5: Re-open the item, flip to unique tracking, then add one unit via
    # the Manage units link.
    item_row.get_by_test_id("edit-item").click()
    admin_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    admin_page.get_by_test_id("item-tracking-mode-input").select_option("unique")
    admin_page.get_by_test_id("item-submit").click()
    admin_page.wait_for_url(f"{app_server}/admin/items")

    item_row_after = admin_page.locator(
        '[data-testid="item-row"]', has_text="ADM-0001"
    )
    item_row_after.get_by_test_id("edit-item").click()
    admin_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/edit")
    )
    admin_page.get_by_test_id("manage-units").click()
    admin_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )
    expect(admin_page.get_by_test_id("item-units-empty")).to_be_visible()

    admin_page.get_by_test_id("new-item-unit").click()
    admin_page.wait_for_url(
        lambda u: "/units/new" in u
        and u.startswith(f"{app_server}/admin/items/")
    )
    admin_page.get_by_test_id("item-unit-serial-input").fill("AD-SN-001")
    admin_page.get_by_test_id("item-unit-submit").click()
    admin_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )
    expect(
        admin_page.locator(
            '[data-testid="item-unit-row"]', has_text="AD-SN-001"
        )
    ).to_be_visible()

    # Step 6: Cleanup — archive the unit, archive the item, archive the
    # category. Leaves the active taxonomy + items lists empty for downstream
    # tests, matching the manager walk's cleanup convention.
    sn_row = admin_page.locator(
        '[data-testid="item-unit-row"]', has_text="AD-SN-001"
    )
    sn_row.get_by_test_id("archive-item-unit").click()
    admin_page.wait_for_url(
        lambda u: u.startswith(f"{app_server}/admin/items/")
        and u.endswith("/units")
    )
    admin_page.goto(f"{app_server}/admin/items")
    admin_page.locator(
        '[data-testid="item-row"]', has_text="ADM-0001"
    ).get_by_test_id("archive-item").click()
    admin_page.wait_for_url(f"{app_server}/admin/items")
    admin_page.goto(f"{app_server}/admin/taxonomy")
    admin_page.locator(
        '[data-testid="taxonomy-row"]', has_text="Admin Items E2E Cat"
    ).get_by_test_id("archive-taxonomy").click()
    admin_page.wait_for_url(f"{app_server}/admin/taxonomy")

    admin_page.close()
    if admin_context is not context:
        admin_context.close()
