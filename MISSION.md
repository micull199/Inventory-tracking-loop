# MISSION: UC Inventory Tracking System

This is the single source of truth for the build. Read this every loop iteration. If anything you are about to do is not justified by this document, stop and revise the plan, not the document.

## 1. Purpose

Build a web-based inventory tracking system for UC, a jewellery workshop. The system tracks items across a configurable taxonomy (categories and sub-categories defined by Managers, with custom required fields per category combination). The initial seed taxonomy covers raw materials, consumables, tools, and wax injection moulds, but the system itself is taxonomy-agnostic. It must support both live barcode/QR-code scanning for high-velocity items and periodic stock takes for the rest. It must alert when stock is low, generate purchase orders, track cost using FIFO with variable per-receipt pricing, and keep a full audit trail.

The system replaces ad-hoc spreadsheets and tribal knowledge. It must be reliable enough that workshop and office staff trust it as the source of truth for what UC owns and what UC is running out of.

## 2. Users

Day-to-day users:
- **Workshop staff** (Filers, Setters, Polishers, Casters, etc.): scan items in/out, adjust stock, check out tools and moulds.
- **Office/admin staff**: review stock levels, approve and send purchase orders, run stock takes, pull reports.
- **Managers**: oversee everything, manage suppliers, configure reorder thresholds, see cost reports.
- **Admin**: configure the system, manage users, manage roles.

All users authenticate with Google SSO (UC already uses Google Workspace).

## 3. Scope

### In scope (v1)

**Taxonomy (categories, sub-categories, picked fields)**
- Managers and Admins define a hierarchy of categories and sub-categories from the settings UI. The hierarchy is at most two levels deep (Category → Sub-category) for v1.
- For each node (Category or Sub-category), a Manager picks fields from a fixed in-code catalog (`app/field_catalog.py`). Each catalog entry has a stable key, label, type (text, number, decimal, date, boolean, single-select, multi-select), and a storage target — either a column on the item or a row in `item_field_values`. Fields are not invented per-node; they are chosen from a curated list so the same field cannot be defined twice under different names.
- A field picked on a node is inherited by every descendant. The same catalog entry cannot be picked twice in one ancestor-descendant chain (sibling sub-categories may independently pick the same entry — those keys are scoped to their branch).
- Per-leaf overrides: a picked field can be marked **required**, given a custom **sort order**, and **archived**. Archived picks hide the field from new entry but preserve existing values in history.
- The catalog is hardcoded. Adding a new field type or entry is a code change plus a migration, never a settings-UI action. Deliberate: each new field gets a deliberate review of audit-log shape, CSV column meaning, and storage target rather than letting unstructured drift accumulate.
- A Manager can rename, archive, or reorder taxonomy nodes. Archived nodes are hidden from item creation but still appear in historical reports and on existing items.
- Seed taxonomy on first run: Raw Materials, Consumables, Tools, Wax Injection Moulds, each with no sub-categories and no picked fields. Managers configure from there.

**Item management**
- Each item belongs to exactly one node in the taxonomy.
- SKU is the only structural field — system-allocated from the leaf's prefix, present on every item. Every other field on an item — name, unit, quantity, supplier, location, threshold, reorder qty, tracking mode, checkout requirement, plus jewellery-specific fields like karat or weight — is present on the item *iff* the item's node (or an ancestor) picked that field from the catalog. Required picks must be filled to save.
- Some catalog entries store values directly on the `items` table (column-backed: name, unit, supplier_id, location_id, reorder_threshold, reorder_qty, requires_checkout, tracking_mode, qr_code, notes); others store them in `item_field_values` (row-backed: karat, weight_grams, material, etc.). Item code reads and writes both through a single storage abstraction (`app/field_storage.py`) so route logic is uniform.
- Items page requires the user to pick a category before items are shown. The table columns and CSV export then match exactly the fields that category's tree collects from the catalog.
- Quantity is present on every item. Tracking mode `unique` forces `current_qty = 1` and disables reorder logic (one-of-a-kind items); tracking mode `qty` is the default for stock that is counted in bulk. (Cost is tracked via FIFO layers regardless of tracking mode — see Cost tracking.)
- Items can be archived (soft delete), not hard deleted.

**Stock movements**
- In: receiving stock from a supplier (links to a PO if one exists).
- Out: consumed in production, scrapped, lost.
- Adjustment: stock-take corrections (positive or negative) with a required reason.
- Transfer: between locations (if multi-location is enabled).
- Every movement records: item, quantity, type, user, timestamp, reason/note, optional cost.

**Barcode / QR workflow**
- Each item has a printable QR label.
- A scan-mode page where staff scan a code and pick an action (in / out / adjust).
- Bulk scan mode for stock takes.
- Works on a desktop with a USB scanner and on a phone/tablet camera.

**Stock takes**
- Schedule recurring stock takes per category or per location.
- Stock-take session: start, scan or enter counts, see variance vs. system, commit adjustments.
- Variances logged as adjustment movements with a stock-take reference.

**Tool and mould check-out**
- Some items (configurable per-item flag) require check-out to a user.
- Check-out records: item, user, checked-out-at, expected-return, actual-return, condition note.
- Manager view of currently-out items and overdue items.

**Reorder and purchase orders**
- Items below threshold appear on a reorder dashboard.
- Generate a draft PO grouped by supplier from low-stock items.
- PO has: supplier, line items (item + qty + expected unit cost), status (draft, sent, partially received, received, cancelled), expected date, notes.
- Expected unit cost on a PO line is editable by the user creating the PO (defaults to the most recent received cost for that item, or blank if none). It is **not** authoritative for stock valuation.
- Send PO via email (PDF attachment) to supplier.
- Receive against a PO: full or partial. The receiving user enters the **actual unit cost** per line at the moment of receipt (defaults to the PO's expected cost, can be overridden). Receiving creates an "in" stock movement and a new FIFO cost layer at that actual unit cost.
- Stock-ins outside of a PO (manual receipt) also require a unit cost at the time of entry.

**Cost tracking (FIFO, variable per-receipt pricing)**
- There is no fixed unit cost on an item. Cost is determined per receipt.
- Every "in" movement (whether from a PO receipt or a manual stock-in) records the unit cost entered at the time of receipt. This creates a **cost layer**: a quantity received at a specific unit cost, with a received-at timestamp.
- The **PO line** stores an expected unit cost when the PO is drafted. On receipt, the user can confirm or override the actual unit cost per line. The actual cost is what gets recorded on the cost layer; the expected cost is kept on the PO line for variance reporting.
- "Out" movements consume cost layers in **FIFO order**: oldest layer first. If an out movement spans multiple layers, it is split internally and the cost of goods consumed for that movement is the sum across the layers consumed.
- Adjustment movements:
  - Positive adjustments require a unit cost (defaults to the most recent layer's cost, user can override) and create a new cost layer.
  - Negative adjustments consume layers FIFO, same as outs.
- Item current value is the sum of (remaining qty × unit cost) across its open cost layers.
- The dashboard shows total inventory value, calculated this way.
- The cost-of-goods-consumed report sums the cost of all out and negative-adjustment movements over a date range.
- Cost layer history is part of the audit trail and cannot be edited. Corrections are made via new movements with explanatory reasons.

**Audit and history**
- Every item has a full timeline of movements, edits, check-outs, stock-take entries.
- Every action is attributable to a user with a timestamp.
- Audit log is append-only. No user (including Admin) can edit or delete past entries; corrections are made via new adjustment movements.

**Reporting and analytics dashboard**
- Current stock value (total and by category).
- Low-stock count and overdue check-outs.
- Top-consumed items over a configurable window.
- Stock-take variance trends.
- Cost-of-goods-consumed over a date range.
- Export any list view to CSV.

**Auth and roles**
- Google SSO only.
- Four roles: Admin, Manager, Workshop, Office. Permissions:
  - **Admin**: everything, including user management and system config.
  - **Manager**: everything except user management. Includes managing the taxonomy (categories, sub-categories, custom field schemas), suppliers, locations, and reorder thresholds.
  - **Office**: items, movements, POs (including entering actual unit costs at receipt), stock takes, reports. Cannot manage the taxonomy, cannot delete items, cannot change reorder thresholds.
  - **Workshop**: scan items in/out, check tools/moulds in/out, view items, log adjustments (with reason). Can enter unit costs on stock-ins they perform. Cannot see aggregated cost data or reports, cannot manage POs.
- A user can have only one role.
- New Google sign-ins land in a "pending" state until an Admin assigns a role.

### Out of scope (v1, do not build)

- Native mobile apps (responsive web only).
- Offline mode (assume always-online).
- Multi-tenant (single-org system, UC only).
- Customer-facing anything.
- Integration with accounting software (Xero etc.).
- Supplier API integrations beyond email.
- Integration with UC's job/order management systems (treat consumption as opaque "out" movements for now).
- Automatic photo recognition of items.
- Predictive reorder forecasting (just thresholds in v1).

## 4. Non-functional requirements

- **Performance**: scan-to-recorded round trip under 500ms on a local network. List views with 5,000 items must paginate and load in under 1s.
- **Reliability**: no data loss. All writes go through transactions. The audit log is the recovery mechanism if anything else gets corrupted.
- **Usability**: workshop staff are not technical. Scanning and basic actions must be doable in two taps or one scan + one tap. No jargon in the UI.
- **Accessibility**: keyboard-navigable, sensible contrast, readable on a 10" tablet at arm's length.
- **Security**: Google SSO only, no local passwords. Role checks enforced server-side on every endpoint, never trusted from the client. CSRF protection on all mutating routes. Audit log captures actor for every state change.
- **Maintainability**: a competent Python developer should be able to read the code and ship a change in under a day. No clever metaprogramming. Boring is good.

## 5. Tech stack (fixed)

- **Backend**: Python 3.11+, FastAPI.
- **Database**: SQLite for local/test, Postgres for cloud. The data layer must work with both via SQLAlchemy. Use Alembic for migrations.
- **Frontend**: Server-rendered HTML via Jinja2 templates. Add HTMX for interactivity (scan handling, partial updates, modals). No SPA framework. No build step beyond a tiny bit of vanilla CSS and maybe Tailwind via CDN.
- **Auth**: Authlib for Google OAuth.
- **PDF (for POs)**: WeasyPrint or reportlab. Pick one and stick with it.
- **Email**: SMTP via a configurable provider (start with a fake/console backend in dev, real SMTP in prod).
- **Background jobs**: APScheduler in-process for v1 (recurring stock-take prompts, reorder checks). No Celery, no Redis.
- **Tests**: pytest + httpx for API, Playwright (Python) for end-to-end.
- **Lint/format**: ruff (lint and format), mypy (strict on the `app/` package).
- **Deployment**: cloud target is Fly.io or Render (pick whichever is simpler when you get there). Local dev is `uvicorn` against SQLite.

If something the mission requires cannot be done in this stack, escalate by writing to BLOCKED.md. Do not silently swap stacks.

## 6. Data model (high-level, refine in implementation)

Tables (or equivalent):

- `users` (id, google_sub, email, name, role, status, created_at, updated_at)
- `suppliers` (id, name, email, phone, notes, archived_at)
- `locations` (id, name, notes, archived_at)
- `taxonomy_nodes` (id, parent_id?, name, sort_order, archived_at, created_at, updated_at) — two levels max: top-level rows have `parent_id` null, sub-categories point to a parent.
- `taxonomy_field_defs` (id, node_id, name, key, type {text, number, decimal, date, boolean, select, multiselect}, options_json?, required, sort_order, archived_at, created_at, updated_at) — schema attached to a leaf node.
- `items` (id, sku, name, taxonomy_node_id, unit, tracking_mode {qty, unique}, requires_checkout, current_qty, reorder_threshold, reorder_qty, supplier_id, location_id, qr_code, notes, archived_at, created_at, updated_at)
- `item_field_values` (id, item_id, field_def_id, value_text, value_number, value_decimal, value_date, value_bool, value_json) — sparse, one row per field with a value.
- `item_units` (id, item_id, serial_or_label, status, location_id) — only for unique-tracked items.
- `cost_layers` (id, item_id, qty_received, qty_remaining, unit_cost, received_at, source {po_receipt, manual_in, positive_adjustment}, source_movement_id) — FIFO layers; consumed oldest-first.
- `cost_layer_consumptions` (id, layer_id, movement_id, qty_consumed, unit_cost_at_consumption) — records exactly which layer fed which out movement, for audit and reporting.
- `stock_movements` (id, item_id, item_unit_id?, type {in, out, adjustment, transfer}, qty, user_id, reason, note, po_id?, stock_take_id?, total_cost?, created_at) — `total_cost` is computed from cost_layer_consumptions for outs and from the receipt cost for ins.
- `checkouts` (id, item_id, item_unit_id?, user_id, checked_out_at, expected_return, returned_at, condition_note)
- `purchase_orders` (id, supplier_id, status, expected_date, sent_at, notes, created_by, created_at, updated_at)
- `purchase_order_lines` (id, po_id, item_id, qty_ordered, qty_received, expected_unit_cost) — actual unit cost lives on the cost_layer created at receipt.
- `stock_takes` (id, scope_node_id?, scope_location_id?, scheduled_for, started_at, completed_at, created_by)
- `stock_take_lines` (id, stock_take_id, item_id, system_qty, counted_qty, variance, committed)
- `audit_log` (id, actor_id, action, entity_type, entity_id, before_json, after_json, created_at)

Treat this as a starting sketch. Adjust during implementation, but justify any change in PROGRESS.md.

## 7. Definition of Done

The system is "done" for v1 when ALL of the following are true. Do not declare done until you can tick every box, with evidence in the test suite or a manual-test checklist.

1. A new user can sign in with Google, land in pending state, and an Admin can assign them a role. They can then access the appropriate parts of the app.
2. A Manager can define categories, sub-categories, and per-leaf custom field schemas in settings. An Admin can create items in those nodes, including unique-tracked items and qty-tracked items. Required custom fields are enforced. Items can be archived and unarchived.
3. A Workshop user can scan a QR code (USB scanner on desktop AND camera on a phone) and record an in, out, or adjustment movement in two interactions. Stock-in actions prompt for and record a unit cost.
4. A Workshop user can check out a tool or mould flagged for checkout, and check it back in. A Manager can see who has what and what's overdue.
5. An Office user can run a stock take: start it, enter counts, see variances, commit adjustments. The variance shows up in audit history. Positive adjustments require a unit cost; negative adjustments consume FIFO layers.
6. An item that drops below its reorder threshold appears on the reorder dashboard. An Office or Manager user can generate a draft PO from low-stock items, edit expected unit costs, send it via email as a PDF, and later mark it received (full or partial). Receiving prompts for the actual unit cost per line and creates new FIFO cost layers; stock and valuation update accordingly.
7. The dashboard shows current inventory value (computed from open FIFO layers), low-stock count, overdue checkouts, top consumed items, and a cost-of-goods-consumed figure for a date range.
8. Every state-changing action appears in the audit log with the correct actor and timestamp. The audit log cannot be edited.
9. Role-based access is enforced server-side. A Workshop user hitting a Manager-only URL gets a 403, verified by tests.
10. The full pytest suite passes with zero failures. The Playwright E2E suite passes with zero failures. ruff reports zero issues. mypy reports zero issues on `app/`.
11. The app runs locally with `make dev` (or equivalent single command) against SQLite, and runs in cloud config against Postgres with no code changes (only env vars).
12. README explains: how to run locally, how to run tests, how to deploy, how to configure Google SSO, how to add a supplier and an item, how to do a stock take. Written for someone who has never seen the project.

## 8. How to work

You are operating in an autonomous loop. Each iteration:

1. **Read** MISSION.md (this file) and PROGRESS.md.
2. **Pick** the smallest shippable next slice that moves toward Definition of Done. Prefer slices that are end-to-end (DB → API → UI → test) over horizontal slabs.
3. **Plan** the slice. Write the plan into PROGRESS.md before coding.
4. **Implement** the slice.
5. **Test**: run ruff, mypy, pytest, and the relevant Playwright tests. Add new tests for the new behaviour. A slice is not done until it has tests.
6. **Self-critique** against MISSION.md: does this slice hold up against the non-functional requirements? Is the UX usable? Is the code something a competent dev could maintain? Note weaknesses in PROGRESS.md.
7. **Improve** the weakest item from the critique, or commit if nothing is weak enough to block.
8. **Commit** with a message that names the slice and references the Definition-of-Done item it advances. Update PROGRESS.md with status.
9. **Loop**.

### Rules of engagement

- **Tests are the verification signal.** If tests pass but the feature is wrong, the tests are wrong. Fix the tests, not the truth.
- **Boring beats clever.** If a fancy approach saves five lines but adds a concept the next dev has to learn, don't.
- **No silent scope changes.** If you find a reason to expand or shrink scope, write it to PROGRESS.md under "Proposed scope changes" and continue with the original scope. The user reviews these.
- **Stuck detection.** If you fail the same test three iterations in a row without measurable progress, stop. Write a clear summary to BLOCKED.md and halt.
- **Commit hygiene.** Small, frequent commits. Never leave the repo with failing tests on the main branch.
- **Migrations are forever.** Once a migration is committed and applied, do not edit it. Add a new migration to fix mistakes.
- **Secrets never go in the repo.** Use a `.env` file ignored by git, plus a `.env.example` that is committed.
- **Document as you go.** README and inline docstrings get updated in the same commit as the feature, not later.

## 9. Hard rules (do not violate)

- Do not change the tech stack in section 5 without writing to BLOCKED.md and stopping.
- Do not introduce a JS framework (React, Vue, Svelte, etc.).
- Do not store passwords. Google SSO only.
- Do not implement features in the "out of scope" list.
- Do not delete the audit log. Do not provide a way to edit it.
- Do not skip writing tests for a slice "just this once."
- Do not declare the system done unless every Definition-of-Done item is verified.

## 10. Files this loop maintains

- `MISSION.md` — this file. Read every iteration. The user edits this; the loop does not.
- `PROGRESS.md` — running log of plans, completed slices, self-critiques, proposed scope changes. The loop maintains this.
- `BLOCKED.md` — created only if the loop is stuck. The loop halts when this is written.
- `README.md` — user-facing docs. Updated alongside features.
- `CHANGELOG.md` — high-level "what shipped" log, one line per slice.
