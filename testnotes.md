# Test notes — manual feature audit

Manual end-to-end audit of every feature area, run against a local `make dev`
instance with Playwright + curl while signed in as admin (and as a
freshly-promoted Workshop user for the role-enforcement legs). Date of run:
2026-05-07. All 12 Definition-of-Done items reported ticked in `PROGRESS.md`;
every DoD item tested works end-to-end on the happy path.

## Bugs found

### High — UX regressions (raw JSON shown instead of an HTML error page)

Four error paths bail out with a `{"detail":"..."}` JSON body instead of
re-rendering the form, leaving the user staring at raw JSON and forcing them to
hit Back to recover. Suppliers / locations / stock-out do this correctly
(re-render the form with `role=alert`); these don't:

1. `POST /admin/items` — invalid custom-field type. Submitting a non-numeric
   value into a `decimal` custom field returns
   `{"detail":"Purity must be a number"}`. All other typed fields are lost.
2. `POST /admin/items` — missing required custom field. Returns
   `{"detail":"Purity is required"}` as raw JSON.
3. `GET /admin/items/{id}/transfer` when the item has no `location_id` set:
   page body is
   `{"detail":"this item has no location yet — set one via the edit form before transferring"}`.
   Should redirect (or render an HTML page) with a flash + link to the edit
   form.
4. `POST /admin/purchase-orders/{id}/receive` — over-receipt. Returns
   `{"detail":"line 1: cannot receive more than ordered (ordered 500.0000, received 0.0000, requested 9999)"}`.
   The receive form has typed costs that get lost.

### Medium — labelling / dropdown bugs

5. Item-edit category dropdown mis-labels an item's existing parent that has
   gained sub-categories as `Raw Materials (archived)`. Reproduce: create a
   category, add an item to it, then add a sub-category to the parent. The
   parent is now a non-leaf — but it's not archived. The label is misleading
   and could trick a manager into thinking they archived something they
   didn't. Bug is in the items-edit form's category-options renderer.
6. The `select` / `multiselect` field-def form depends entirely on HTMX + JS
   to swap in the options textarea on type change. With JS off (or HTMX
   failing to load), users cannot create a select field — the textarea isn't
   in the static HTML and the server rejects the post with
   "select / multiselect fields need at least one option". A `<noscript>`
   fallback or always-rendered (display:none) textarea would harden this.

### Medium — empty / useless surface

7. Reorder dashboard surfaces items with `reorder_threshold = 0`. A new item
   with `current_qty = 0` and `threshold = 0` appears in the reorder list
   (because `0 ≤ 0`) showing "Suggested reorder 0.0000, Deficit 0.0000".
   Useless rows clutter the dashboard. Filter to `threshold > 0` or
   `deficit > 0`.
8. Decimal custom-field renders as `<input type="text">` rather than
   `type="number" inputmode="decimal" step="any"`. On mobile/tablet (which
   MISSION §4 calls out), the keyboard doesn't show a decimal point.

### Low — observations

9. No historical / "all" view for checkouts. Admin tabs are Open / Overdue
   only; `?show=all` silently maps to `?show=open`. Once a checkout is
   returned, it's only retrievable via the audit log. Workable, but
   unexpected — the table stores the row.
10. Inventory values rendered to 4 decimal places ("Total inventory value
    166.0000", "Current quantity: 525.0000 g", etc.). Spec doesn't pin
    precision but 4 dp on UI tiles reads as noise; consider 2 dp for
    display, keep 4 dp for storage.
11. Movement timestamps have no timezone marker on the UI ("2026-05-07
    10:09"). The CSV export does include UTC ISO timestamps. UI could
    mirror that.
12. `favicon.ico` 404 on every page (cosmetic, console noise only).

## Things that genuinely work

- **Auth**: Google SSO scaffolding (OAuth stub e2e tests), dev-login backdoor
  (`POST /auth/_dev-login` mounted only when `APP_ENV in {dev, test}`,
  double-checked at request time), sign-out, role matrix (anon → 401,
  wrong-role → 403, admin-always-passes).
- **CSRF middleware**: anon / no-token POSTs return 403 across all mutating
  routes tried.
- **CRUD + archive + CSV** for suppliers, locations, taxonomy categories,
  sub-categories, field defs, items, item-units. Dup-name validation,
  blank-name validation, archive → unarchive round-trip all pass.
- **Stock movements** (in / out / adjust / transfer) — FIFO math verified
  end-to-end. Inventory value ended at 166.00 and COGS at 126.50, both
  matching hand-calculated expected values across 9 mixed movements + a
  stock-take commit. Insufficient stock rejected with
  "Not enough stock: requested 99999, only 525.0000 available".
- **Scan**: QR/SKU resolve precedence, unknown-code flash ("No item found for
  code: NONEXISTENT."), action-picker on `/scan/item/{id}`, archived-item
  read-only handling.
- **Reorder → PO → receive** full chain: draft created with reorder_qty +
  last-receipt cost pre-filled, edit, PDF download (2.7 KB valid PDF v1.3),
  send via console email, partial receipt → `partially_received`, full
  receipt → `received`, all-zero submit no-ops with the documented flash.
- **Stock take**: schedule → start → snapshot → count → commit. Positive
  variance required unit cost; negative variance consumed FIFO; both wrote
  `stock_movement.adjustment` audit rows.
- **Checkouts**: `requires_checkout` flag, item-unit selection,
  expected-return date, overdue surface (`Overdue (6d)` with backdated due
  date), check-in flow.
- **Audit log**: 50 rows recorded for the test session, CSV branch
  (`?format=csv`) returned full snapshot ignoring pagination, DB-level
  UPDATE/DELETE triggers reject direct sqlite mutations (`audit_log is
  append-only: UPDATE forbidden`).
- **Reports**: Dashboard tiles (value / low-stock / open-POs / overdue),
  top-consumed window, COGS date range, variance-trend page.

## Net assessment

The core engine is solid and the test suite has clearly held it together. The
bugs cluster in two places — a handful of routes that throw
`HTTPException(detail=...)` instead of re-rendering forms, and a few small
UX papercuts (decimal input, threshold-0 reorder, non-leaf parent label).
