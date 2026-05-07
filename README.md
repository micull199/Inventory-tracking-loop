# UC Inventory

Inventory tracking for UC's workshop and office. Tracks raw materials, consumables, tools, and wax injection moulds across a Manager-defined taxonomy with custom fields per category. Supports QR/barcode scanning, periodic stock takes, FIFO cost tracking with variable per-receipt pricing, low-stock alerts, and PO generation with email-to-supplier.

> **Status: under active build.** Sections marked _TODO_ get filled in as the corresponding feature ships. If you find a TODO that should have been done, the build loop missed a step. Open an issue.

---

## Quick links

- [Mission and scope](./MISSION.md) — single source of truth for what this app does and does not do.
- [Build progress](./PROGRESS.md) — what's been built, what's next, what's stuck.
- [Changelog](./CHANGELOG.md) — high-level "what shipped" log, one line per slice.
- _TODO: deployed URL_

---

## What this is

A web app for UC staff to:

- Scan items in and out from a phone, tablet, or desktop with a USB scanner.
- Run periodic stock takes and reconcile variances with a full audit trail.
- Track which staff member has which tool or mould checked out.
- Get alerted when stock drops below a threshold and generate purchase orders straight to suppliers.
- See real inventory value computed FIFO from per-receipt costs, not estimates.

Built deliberately boring: server-rendered HTML, HTMX for interactivity, no SPA, no build step. Anyone competent in Python should be able to read the code and ship a fix.

## What this isn't

Not a job/order management system. Not an accounting integration. Not customer-facing. See MISSION.md §3 for the full out-of-scope list.

---

## Tech stack

- **Backend:** Python 3.11+, FastAPI
- **Templating:** Jinja2 + HTMX
- **Database:** SQLite (dev), Postgres (prod), via SQLAlchemy + Alembic migrations
- **Auth:** Google SSO via Authlib
- **PDF:** reportlab (chosen during PO3; see `pyproject.toml` for the dep and `app/pdf.py` for the renderer).
- **Email:** SMTP (console backend in dev)
- **Background jobs:** APScheduler in-process
- **Tests:** pytest + httpx, Playwright (Python) for end-to-end
- **Lint/type:** ruff, mypy (strict on `app/`)
- **Deploy target:** _TODO (Fly.io or Render)_

The stack is fixed by MISSION.md §5. Don't swap it without going through that document first.

---

## Local development

### Prerequisites

- Python 3.11 or later
- `uv` (recommended) or `pip`
- Node (only for Playwright browser binaries)

### First-time setup

```bash
git clone <repo-url> uc-inventory
cd uc-inventory

# install Python deps
uv sync           # or: pip install -e ".[dev]"

# copy env template, fill in values
cp .env.example .env

# create dev DB and run migrations
make migrate

# install Playwright browsers
make e2e-install
```

### Running

```bash
make dev          # starts uvicorn on http://localhost:8000 with auto-reload
```

### Common commands

```bash
make dev          # run the app
make test         # pytest
make e2e          # Playwright end-to-end
make lint         # ruff
make typecheck    # mypy
make migrate      # alembic upgrade head
make migration m="add cost layers table"   # generate a new migration
make seed         # load dev fixtures (sample taxonomy, users, items)
make check        # lint + typecheck + test + e2e (run before commit)
```

If `make check` is green, the build loop considers the slice shippable. If it's red, the loop fixes it before doing anything else.

---

## Configuration

All config via environment variables. See `.env.example` for the full list. Key vars:

| Variable | Purpose | Required | Example |
|---|---|---|---|
| `DATABASE_URL` | SQLAlchemy URL | yes | `sqlite:///./dev.db` or `postgresql://...` |
| `SECRET_KEY` | session signing | yes | (random 32+ bytes) |
| `GOOGLE_CLIENT_ID` | OAuth client ID | yes | `...apps.googleusercontent.com` |
| `GOOGLE_CLIENT_SECRET` | OAuth client secret | yes | (from Google Cloud console) |
| `GOOGLE_HOSTED_DOMAIN` | optional domain restriction | no | `cullen.example` |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | outbound email | yes (prod) | — |
| `EMAIL_BACKEND` | `console` (dev) or `smtp` (prod) | yes | `console` |
| `APP_BASE_URL` | absolute URL for OAuth + email links | yes | `http://localhost:8000` |

Never commit `.env`. Only `.env.example` is in version control.

### Configuring Google SSO

1. **Create an OAuth client.** Google Cloud Console → APIs & Services → Credentials → "Create Credentials" → "OAuth client ID" → Web application. Add an authorised redirect URI of `${APP_BASE_URL}/auth/google/callback` (e.g. `http://localhost:8000/auth/google/callback` for dev).
2. **Save the client ID + secret** into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Restart the app.
3. **Optional: lock to your Workspace domain.** Set `GOOGLE_HOSTED_DOMAIN=your-domain.example` to refuse sign-ins from outside that domain. (Enforced by Google during the OAuth flow.)
4. **Seed the first admin.** Set `BOOTSTRAP_ADMIN_EMAIL=you@your-domain.example` in `.env`. The first user to sign in matching that email is auto-promoted to Admin (active). Every subsequent user lands in `pending` and must be assigned a role by an existing Admin via the user-management UI. Once you've signed in once, you can clear `BOOTSTRAP_ADMIN_EMAIL`.

Until that first Admin signs in, the app will accept Google sign-ins but every user (including you) will see the "account pending approval" page. After the seed, manage roles from `/admin/users` (admin-only).

> **Dev/test backdoor:** when `APP_ENV=dev` or `APP_ENV=test`, the app exposes `POST /auth/_dev-login` (form-encoded `email`, `name`, optional `sub`) which signs the given user in without going through Google. This is how the Playwright suite logs in. It is hard-disabled when `APP_ENV=prod`.

---

## How it works (mental model)

A few concepts you need to hold in your head to read this codebase.

**Taxonomy.** Items don't have a hard-coded category. They belong to a leaf node in a Manager-defined two-level taxonomy (Category → Sub-category). Each leaf node has a schema of custom fields. Items inherit that schema. Editing the schema doesn't break old items; their values are preserved even if a field is later removed.

**FIFO cost layers.** Stock has no single "unit cost." Every receipt creates a cost layer: `qty_received` at `unit_cost`. Out movements consume layers oldest-first, splitting across layers if needed. Each consumption is recorded in `cost_layer_consumptions` so you can answer "what did this specific out movement cost?" forever after. Item value at any moment is the sum of `qty_remaining × unit_cost` across open layers.

**Movements are append-only.** In, out, adjustment, transfer. There is no edit, no delete. Corrections are new movements. The audit log assumes this.

**Roles.** Admin > Manager > Office > Workshop. Server-side role checks on every endpoint, never trusted from the client. New Google sign-ins land in `pending` until an Admin assigns a role.

For the long version, see MISSION.md.

---

## Common workflows

### Adding a new supplier

Suppliers are vendors UC buys stock from. They are Manager-owned (Admins always pass) — Office and Workshop see them only indirectly via purchase orders.

1. Sign in as a Manager (or Admin).
2. Click **Suppliers** in the top nav (or visit `/admin/suppliers`).
3. Click **New supplier**.
4. Fill in **Name** (required, must be unique across active suppliers). Email, phone, and notes are optional.
5. Click **Create supplier**.
6. The list page reappears with the new row.

To edit a supplier, click **Edit** on its row. To stop using a supplier without losing history, click **Archive** — archived suppliers move to the Archived tab and remain readable on existing purchase orders, but are hidden from new PO drafts. Click **Unarchive** to re-activate. Suppliers cannot be hard-deleted; the audit log assumes their IDs persist.

### Defining a new category and its custom fields

The taxonomy is a Manager-owned two-level tree (Category → Sub-category). Items live on a **leaf node** — a Category with no sub-categories, or any Sub-category. Each leaf has its own **field schema** (extra columns per item: text, number, decimal, date, boolean, single-select, multi-select). Items inherit the schema of their leaf.

**Create a top-level category.**

1. Sign in as a Manager (or Admin).
2. Click **Taxonomy** in the top nav (or visit `/admin/taxonomy`).
3. Click **New category**.
4. Fill in **Name** (required, must be unique among active siblings). Sort order is optional — leave blank to append.
5. Click **Create category**.

**Optionally add sub-categories.** From the Taxonomy list, click **Manage** on a category, then **New sub-category**. Adding a sub-category turns the parent into a non-leaf — the parent's field schema is no longer editable, and items must live on a sub-category instead.

**Define the field schema on a leaf node.** From the Taxonomy list (or sub-category list), click **Fields** on the leaf row, then **New field**.

1. **Name** (required). Shown to users when creating items.
2. **Type**: one of `text`, `number`, `decimal`, `date`, `boolean`, `select`, `multiselect`.
3. **Options** (for `select` / `multiselect` only): one option per line in the textarea.
4. **Required**: tick to force the field on item create/edit.
5. **Sort order**: optional — leave blank to append.
6. Click **Create field**.

**Schema versioning posture.** Editing a field schema does not retroactively break existing items: stored values stay readable. New edits to those items must satisfy the current schema. To stop a field from appearing on new entry without losing history, click **Archive** on its row (toggle to the Archived tab to **Unarchive**). The same Archive/Unarchive posture applies to categories and sub-categories — archived nodes are hidden from new items but stay readable on historical items, audit log entries, and reports. Nodes and fields cannot be hard-deleted; the audit log assumes their IDs persist.

### Creating an item

Items are the foundational unit of stock — every movement, stock take, purchase order, and audit row references an item. Each item lives on exactly one **leaf node** of the taxonomy (see _Defining a new category…_ above) and inherits that node's custom fields. Creating items is Manager-owned (Admins always pass); Office can edit existing items but cannot create or archive them.

1. Sign in as a Manager (or Admin).
2. Click **Items** in the top nav (or visit `/admin/items`).
3. Click **New item**.
4. Fill in the core fields:
   - **SKU** (required, unique across active items, max 64 chars).
   - **Name** (required, max 255 chars).
   - **Category** (required) — pick a leaf node. Non-leaf categories are listed but disabled (the dropdown shows _"── Top / (pick a sub-category) ──"_); items must live on a leaf.
   - **Unit** (required) — short unit-of-measure label, e.g. `g`, `ml`, `ea`.
   - **Tracking mode** — `qty` (counted in bulk, e.g. a box of polishing compound) or `unique` (one record per physical item, e.g. a specific tool or mould). For `unique`, after saving, use the **Manage units** link on the item form to add per-unit serial labels.
   - **Requires check-out** — tick for tools, moulds, or anything that gets handed to a named worker. Surfaces a per-item check-out form to Workshop staff (see _Reading the audit trail_ below for how that history is captured).
5. Fill in the reorder fields (Manager-only — Office sees these read-only):
   - **Reorder threshold** — when `current_qty` drops to or below this value, the item appears on the reorder dashboard.
   - **Reorder quantity** — the default qty pre-filled on draft purchase orders.
6. Fill in the optional fields: **Supplier** (preferred vendor for reorders), **Location** (physical home), **QR code** (printable label payload — type any unique string, or leave blank for items that don't carry a label), **Notes** (free text, max 2000 chars).
7. Fill in any **Category fields** the leaf node defines (DOC2's custom-field schema). Required custom fields are enforced on save; the form rejects bad types (e.g. non-numeric in a `decimal` field) with a 400.
8. Click **Create item**.
9. The list page reappears with the new row. New items start at `current_qty = 0` — only stock movements (Stock in / Stock out / Adjust / Transfer / a PO receipt) move that number.

To edit an item, click **Edit** on its row. To stop using an item without losing history, click **Archive** — archived items move to the Archived tab and remain readable on existing movements, purchase orders, and stock takes, but are hidden from new entry. Click **Unarchive** to re-activate. Items cannot be hard-deleted; the audit log assumes their IDs persist.

### Printing a QR label and scanning it

Scanning is the high-velocity workshop path — a Workshop user holds a labelled item, scans its code, and records a movement in two interactions (one scan + one tap). It works on a desktop with a USB barcode scanner (which emulates a keyboard) and on a phone or tablet via the back-facing camera. Manager and Office can scan as well; only the *recording* surface differs by role (e.g. Office cannot check items out).

**Labelling an item.** Each item carries an optional **QR code** string set on the item form (see _Creating an item_ above). Type any unique string — typically the SKU or a short slug — and print a matching label by hand for v1. (A built-in printable-label view lands in a future slice; until then the QR-code field is the source of truth for what the scanner will see.) Items without a QR code are still scannable by SKU.

**Scanning the code.**

1. Sign in as a Workshop user (Manager / Office / Admin can also scan).
2. Click **Scan** in the top nav (or visit `/scan`). The page loads with the **Code** input autofocused.
3. **USB scanner (desktop):** point the scanner at the label. The scanner sends the decoded characters as keystrokes followed by Enter; the form auto-submits.
4. **Camera (phone / tablet):** click **Use camera** to start the back camera (`facingMode: "environment"`). On a successful decode, the value is written into the input and the form submits automatically. Permission denied or no-camera errors fall back to the keyboard input with a plain-English status message.
5. **Keyboard fallback:** any user can type a code into the input directly and click **Find item** if a label is missing or unreadable.

**How a code resolves.** The `/scan/resolve` route looks up the typed value as a **QR code first, then SKU**. If two items happen to share a string across columns, the QR-coded item wins (that's what the scanner physically points at). Unknown codes redirect back to `/scan` with a flash message.

**The action picker on `/scan/item/{id}`.** A successful resolve 303-redirects to `/scan/item/{id}`, which keeps the scan input focused (so the next item drives a fresh resolve without re-navigating) and adds an action picker for the resolved item: **Stock out** (qty), **Stock in** (qty + unit cost — creates a new FIFO cost layer), **Adjust** (direction, qty, optional unit cost for increases, required reason). For items flagged `requires_checkout`, a **Check out →** link also appears.

**Archived items still resolve.** Scanning the QR code or SKU of an archived item resolves to its scan page — but the action forms are hidden and a note directs the user to the items list to record movements. This keeps physical labels working without re-opening the archive on the audit trail.

### Running a stock take

A stock take is a count session that reconciles what's physically on the shelf against the system's `current_qty` per item. Variances are committed as **adjustment movements** through the same FIFO cost engine the rest of the app uses — positive variances need a unit cost and create a new cost layer, negative variances consume the oldest layers first. Stock takes are owned by **Office** staff per MISSION §3 (Manager and Admin also pass; Workshop cannot run stock takes).

Stock takes move through three derived statuses: **scheduled** (created but not started — scope frozen) → **in_progress** (started; per-line counts being entered) → **completed** (committed; lines marked `Yes` in the Committed column). Each transition writes one audit row (`stock_take.created`, `stock_take.started`, `stock_take.counted`, `stock_take.committed`).

**Schedule a stock take.**

1. Sign in as an Office user (Manager or Admin also work).
2. Click **Stock takes** in the top nav (or visit `/admin/stock-takes`).
3. Click **New stock take**.
4. Pick a **Scope**: **All items**, a **Category** (a leaf or non-leaf taxonomy node — sub-categories are listed prefixed with `↳`), or a **Location**. Limiting the scope keeps a count session short; running an "all items" take only makes sense for a full quarterly count.
5. Pick a **Scheduled for** date (required) — the day you intend to actually run it. Notes are optional (max 2000 chars).
6. Click **Schedule stock take**. The list page reappears with the new row in the **Open** tab. Use the **Completed** tab to browse history.

**Start counting.** From the stock takes list, click **View →** on the row, then **Start counting**. The system snapshots the current `current_qty` for every active in-scope item into `StockTakeLine` rows — that snapshot is the baseline for variance, so a count that lags real movements stays internally consistent.

**Enter counts.** The detail page now renders a count table. For every line, type the **Counted qty** you actually counted on the shelf. As you save, the **Variance** column derives `counted − system` (negative = stock missing; positive = stock found). Counts are sparse — re-saving with the same value is a no-op (no audit row written for unchanged lines). The progress summary at the top shows **Counted / Uncounted / With variance** so you can pause and resume without losing your place.

**Commit the variances.** Once you have at least one line with non-zero variance, the **Commit** form appears below the count table.

- **Positive variance** lines (the shelf had more than the system): a **Unit cost** input is required (defaults to the most recent receipt price for the item). Each commit creates one `ADJUSTMENT` movement plus a new FIFO cost layer at the entered cost — same posture as a manual stock-in.
- **Negative variance** lines (the shelf had less than the system): the unit cost cell shows `—`; the cost engine consumes the oldest open FIFO layers automatically and records each `cost_layer_consumption` for the audit trail.

Click **Commit count**. If any negative-variance line would underflow available stock (e.g. the snapshot lagged a fast-moving item), the whole commit is rolled back atomically — no movements are written, no layers are touched, the stock take stays `in_progress`, and the page re-renders with the typed unit costs preserved and an error block naming the offending SKU. Fix the count (or unarchive missing units), then re-commit.

**Audit trail.** Every committed adjustment carries the stock take's id (`stock_take_id` FK on `StockMovement`) so the audit view at `/admin/audit` lets a Manager trace any adjustment back to the count session that produced it. The `stock_take.committed` audit row also records the per-movement snapshot for later review.

### Generating and sending a purchase order

A purchase order (PO) tells a supplier what UC wants to buy. POs move through five statuses: **draft** (editable, never sent) → **sent** (emailed to the supplier; lines locked except for receipt) → **partially_received** / **received** (some or all qty has arrived; see _Receiving stock against a PO_ below). A draft can also be **cancelled** instead of sent. POs are owned by **Office** staff per MISSION §3 (Manager and Admin also pass; Workshop cannot manage POs at all). Each transition writes one audit row — `purchase_order.created`, `purchase_order.updated`, `purchase_order.sent`, `purchase_order.cancelled`, `purchase_order.received` — and the audit view at `/admin/audit` lets a Manager trace any PO state change to the actor and timestamp.

**Drafting a PO from the reorder dashboard.** The reorder dashboard at `/admin/reorder` lists every active item whose `current_qty` is at or below its **Reorder threshold** (set on the item form — see _Creating an item_ above), grouped by **preferred supplier**. This is the normal entry point for restocking: low-stock items already know which supplier they belong to.

1. Sign in as an Office user (Manager or Admin also work).
2. Click **Reorder** in the top nav (or visit `/admin/reorder`).
3. Find the supplier group you want to order from. Each group renders its own table of low-stock items with **SKU**, **Current qty**, **Threshold**, **Suggested reorder** (the item's **Reorder quantity**), and **Deficit**.
4. Click **Draft PO from this supplier**. The button posts to `/admin/reorder/draft-po` and redirects to the new draft's detail page (`/admin/purchase-orders/{id}`) with status **draft** and one PO line per low-stock item, each pre-filled with its **Reorder quantity** and **Expected unit cost** seeded from the item's most recent received cost (blank if the item has never been stocked-in).
5. **Blocker prose.** A supplier group whose preferred supplier is missing renders _"Assign an active supplier on the item before drafting a PO."_ A supplier group whose supplier is archived renders _"Supplier is archived — unarchive it or move items to an active supplier first."_ Either case hides the **Draft PO** button.

**Editing the draft.** On the detail page, while `status == draft`, an inline edit form lets you change the **Expected date** and **Notes**, and per-line **Qty ordered** and **Expected unit cost**. Click **Save changes** to commit edits. **Expected unit cost** is what gets emailed to the supplier and printed on the PDF; it is **not** authoritative for stock valuation. The **actual unit cost** is entered at receipt time (see _Receiving stock against a PO_ below) and is what creates the FIFO cost layer per MISSION §3.

**Cancelling a draft.** Click **Cancel this draft** to flip the PO to **cancelled** without sending. Cancelled POs are read-only and do not appear in receivable filters; their lines are preserved for the audit trail.

**Downloading the PDF.** Click **Download PDF** on the detail page (visible on every status except cancelled) to render the PO as a PDF — UC letterhead, supplier block, line items with SKU + qty + unit + expected unit cost, and a grand total.

**Sending the PO.**

1. Confirm the supplier has an **email** address on its record (see _Adding a new supplier_ above). If not, the detail page renders _"This supplier has no email address — add one on the supplier record before sending."_ instead of the **Send** button.
2. Click **Send to supplier**. The app builds an email with the PO PDF attached, sends it via the configured `EMAIL_BACKEND` (`console` in dev — prints the message to stdout — or `smtp` in prod), flips `status` to **sent**, records `sent_at`, and writes the `purchase_order.sent` audit row. Re-clicking **Send** is rejected (a sent PO can't be re-sent — corrections go via Cancel + new draft, or wait for receipt).

**Browsing past POs.** Click **Purchase orders** in the top nav (or visit `/admin/purchase-orders`) for a newest-first list filterable by status (`all` / `draft` / `sent` / `partially_received` / `received` / `cancelled`). The list view also exposes a **Download CSV** link that respects the active filter (`purchase_orders_{status_filter}.csv`).

### Receiving stock against a PO

Receiving is the **FIFO-cost-layer-creating** leg of the PO lifecycle. Each line you receive becomes one **in** stock movement on the item plus a new cost layer (`source = po_receipt`) at the **actual unit cost** you enter at the moment of receipt — and that actual cost is what stock valuation is calculated against per MISSION §3 (the **expected unit cost** on the PO line is the price emailed to the supplier; it is **not** authoritative for valuation). Receiving is owned by **Office** staff (Manager and Admin also pass; Workshop cannot manage POs at all). Each receipt writes one `purchase_order.received` audit row that records the before / after status plus a per-line list of `(line_id, received_qty, actual_unit_cost, movement_id)` so the audit view at `/admin/audit` can trace any new cost layer back to the PO line that produced it.

**Only `sent` and `partially_received` POs can be received against.** A draft PO must be sent first (or cancelled if no longer wanted); a fully `received` PO is closed; a `cancelled` PO cannot be received against. The detail page hides the **Receive against this PO** link unless the PO is in a receivable status — if the link is missing, check the status badge at the top of the detail page.

1. Sign in as an Office user (Manager or Admin also work).
2. Open the PO detail page from **Purchase orders** in the nav (or visit `/admin/purchase-orders/{id}`).
3. Click **Receive against this PO** (or visit `/admin/purchase-orders/{id}/receive` directly). The receive form lists every line with its **SKU**, **Qty ordered**, **Already received** (cumulative across prior partial receipts), **Outstanding** (`qty_ordered − qty_received`), plus two per-line inputs:
    - **Receiving now** — the qty you took in *this* delivery. Leave blank or `0` for lines that didn't arrive yet (perfectly valid for a partial receipt).
    - **Actual unit cost** — the price you actually paid for *this* delivery. Defaults to the line's expected unit cost; override if the supplier invoice differs. Required when **Receiving now** is non-zero, and is what creates the FIFO cost layer.
4. Click **Record receipt**. For each line with `Receiving now > 0`, the app records an **in** stock movement on the item, creates a new FIFO cost layer at the actual unit cost (subsequent stock-outs consume layers oldest-first), and increments the line's cumulative received qty. The new movement and layer appear on the item detail page's history.
5. The PO's status flips automatically based on the *cumulative* received state across every line:
    - **received** — every line has `qty_received >= qty_ordered`.
    - **partially_received** — at least one line still has outstanding qty. The receive form stays available so you can record the rest later.

**Over-receipt is rejected.** If you accidentally receive more than was ordered on a line (cumulative `qty_received + Receiving now > qty_ordered`), the form returns a 400 naming the line and the offending qty — no movement, layer, or audit row is written. If a supplier genuinely sent more than ordered, record the overage as a manual stock-in on the item (which also creates a FIFO cost layer) rather than over-receiving against the PO.

**An all-zero submit is a no-op.** Submitting the form with every line blank or `0` writes no movement, no cost layer, no audit row, and the PO status doesn't change — the page flashes _"PO #{id} — no receipts entered."_ and redirects back to the detail page.

### Reading the audit trail for an item

The audit trail is the system's append-only memory of every state-changing action — every item create, stock movement, purchase-order transition, stock-take commit, checkout, role assignment, supplier or taxonomy edit. Per MISSION §9 the log cannot be edited or deleted, even by an Admin; corrections are made by recording new compensating movements with explanatory reasons rather than rewriting history. Reading the audit trail is **Manager** and **Admin** only — the **Audit** nav link is hidden from Office and Workshop, and the route returns a 403 if either role tries to navigate to `/admin/audit` directly.

**Every state change writes one row.** Whenever a route changes the database — creating an item, recording a stock-out, sending a PO, committing a stock take, returning a tool — the same code path that writes the change also writes one audit row in the same transaction (so a successful change without an audit row is impossible). The cross-cutting `tests/integration/test_audit_coverage.py` sweep enforces this: every POST / PUT / PATCH / DELETE route in `app.routes` either calls `record_audit(...)` directly or appears in a small reviewer-audited exempt list with a one-line justification. Each row carries the **actor** (the signed-in user, or `(system)` for background events like the bootstrap-admin promotion), an ISO **timestamp**, the **action** wire-name (e.g. `item.created`, `stock_movement.out`, `purchase_order.received`), the **entity_type:entity_id** the action targeted, and **before** + **after** JSON dicts capturing what changed.

**Browsing the log.**

1. Sign in as a Manager (or Admin).
2. Click **Audit** in the top nav (or visit `/admin/audit`).
3. The page renders a newest-first paginated table of 50 rows per page with six columns: **Time** (ISO timestamp), **Actor** (the user's email, or `(system)`), **Action** (the wire-name like `item.created` or `stock_movement.in`), **Entity** (`<type>:<id>` like `item:42`, or just `<type>` for entity-less rows), **Before**, and **After**. The Before / After cells render an em-dash (—) for create rows (no prior state) and for entries with no resulting state; other rows show the JSON dict captured at write time.
4. Use **Previous** / **Next** at the bottom to page through history. Going past the last page renders an empty table — page back to recover.

**Finding entries for a specific item.** There is no per-item filter form in the v1 read view. Two paths in the meantime:

- **Browser search.** Open `/admin/audit`, hit Cmd+F (Ctrl+F on Windows / Linux), and search for the item's SKU, name, or `item:<id>` token. Page through the table if the entry isn't on the current page.
- **CSV export.** Click **Download CSV** at the top of the page (or visit `/admin/audit?format=csv` directly) to download `audit_log.csv` with **every** row — the CSV branch ignores pagination so the snapshot is complete. The CSV has eight columns: `id`, `created_at`, `actor_email`, `action`, `entity_type`, `entity_id`, `before_json`, `after_json`. Open it in a spreadsheet and filter on `entity_type=item` plus `entity_id=<n>`, or on `entity_type=stock_movement` and grep the JSON cells for `"item_id":<n>` if you want every movement against the item.

A filter form for actor / entity / date range is queued as a future slice (`A1b` in the backlog).

**Action vocabulary.** A few canonical action wire-names you can grep or filter on:

- **Items:** `item.created`, `item.updated`, `item.archived`, `item.unarchived`
- **Stock movements:** `stock_movement.in`, `stock_movement.out`, `stock_movement.adjustment`, `stock_movement.transfer`
- **Stock takes:** `stock_take.created`, `stock_take.started`, `stock_take.counted`, `stock_take.committed`
- **Purchase orders:** `purchase_order.created`, `purchase_order.updated`, `purchase_order.sent`, `purchase_order.cancelled`, `purchase_order.received`
- **Checkouts:** `checkout.created`, `checkout.returned`
- Plus actions on `supplier`, `location`, `taxonomy_node`, `taxonomy_field_def`, `user`, and `item_unit`.

**Immutability.** The `audit_log` table has DB-level UPDATE and DELETE triggers (SQLite + Postgres) that reject any modification — even if a buggy code path or a direct DB session tries to rewrite a row, the database refuses. The log is **append-only** and **cannot be edited**. If a recorded row turns out to be wrong (say a stock-out qty was a typo), record a compensating adjustment movement with a reason that names the original movement; the original row stays as-is and the new row sits next to it in the timeline.

---

## Project layout

```
.
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app
│   ├── config.py            # env-driven settings
│   ├── db.py                # SQLAlchemy engine + session
│   ├── models/              # SQLAlchemy models
│   ├── schemas/             # Pydantic request/response models
│   ├── routes/              # FastAPI routers, one per area
│   ├── services/            # business logic (FIFO engine, audit hooks, etc.)
│   ├── templates/           # Jinja2 templates
│   ├── static/              # CSS, JS shims, vendored libs
│   └── auth.py              # Google SSO + role checks
├── migrations/              # Alembic
├── tests/
│   ├── unit/
│   └── e2e/                 # Playwright
├── scripts/                 # one-off utilities (seed, etc.)
├── MISSION.md
├── PROGRESS.md
├── README.md
├── pyproject.toml
├── alembic.ini
├── Makefile
└── .env.example
```

Layout will evolve as the build progresses; this section gets updated when it does.

---

## Testing

Three layers, all mandatory before a slice is considered done:

- **Unit (pytest):** business logic, especially FIFO consumption, audit-log generation, role enforcement helpers.
- **Integration (pytest + httpx):** route-level tests with a test DB, covering happy paths and 403 cases per role.
- **End-to-end (Playwright):** the user-visible flows from MISSION.md §7 (Definition of Done). One Playwright test per DoD item, minimum.

Run all three with `make check`.

The build loop's exit condition is "all twelve DoD items ticked AND `make check` is green." There is no other way for the loop to declare done.

---

## Deployment

_TODO: filled in during slice P4. Will cover Fly.io or Render config, secrets management, Postgres connection, running migrations on deploy, and how to assign the first Admin user in production._

---

## How this gets built

This project is being built by Claude Code running an autonomous build loop. The loop:

1. Reads `MISSION.md` and `PROGRESS.md`.
2. Picks the smallest end-to-end slice that moves a Definition-of-Done item closer.
3. Plans, implements, tests, self-critiques.
4. Commits.
5. Loops.

If something in this codebase looks wrong, check `PROGRESS.md` first — it'll tell you whether it's intentional, in-progress, or noted as a weakness pending a future slice. If it's none of those, it's a bug.

Human-in-the-loop touch points:
- Reviewing and ticking Definition-of-Done items in `PROGRESS.md` after spot-check.
- Reviewing entries under "Proposed scope changes" and "Open questions" in `PROGRESS.md`.
- Editing `MISSION.md` if scope genuinely needs to change.

---

## Contributing

_TODO_

## License

_TODO_
