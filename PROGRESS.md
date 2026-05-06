# PROGRESS

This file is maintained by the build loop, not the user. Read it at the start of every iteration. Update it at the end of every iteration.

The loop's job is to keep this file honest. If something here is out of date, fix it before doing anything else.

---

## How to use this file

Every iteration:

1. **Read** the "Current state" section to know where things stand.
2. **Read** the "Next slice" section to see what was queued last time.
3. If "Next slice" is empty, **pick** the next one based on the priorities in MISSION.md §7 (Definition of Done) and the Backlog below. Smallest end-to-end slice that moves a Definition-of-Done item closer to ticked.
4. Move the chosen slice from Backlog into "Current slice" with a plan.
5. Implement, test, self-critique.
6. On commit, move the entry from "Current slice" into the "Completed slices" log with the commit hash and a one-line summary.
7. Refresh the "Current state" section.
8. Queue the next slice in "Next slice" if obvious, otherwise leave empty.
9. Update "Definition of Done tracker" if a box just got ticked.
10. Note anything weird in "Open questions" or "Proposed scope changes" — never silently fold scope changes into the work.

If the same test or the same problem fails three iterations in a row without measurable progress, stop and write to BLOCKED.md per MISSION.md §8.

---

## Current state

**Iteration:** 9 (complete)
**Last commit:** 981a8aa — slice: S3 — Taxonomy: top-level categories CRUD (Manager-owned)
**Branch:** main
**Tests:** `make check` green: ruff ✓, mypy ✓ (12 files), pytest 262 unit+integration ✓, Playwright 7 e2e ✓.
**Definition-of-Done items ticked:** 0 / 12. S3 advances DoD #2, #8, #9 but does not complete any of them — #2 still needs sub-categories + custom fields + items + required-field enforcement; #8 is a system-wide guarantee that won't be ticked until every entity state change is audited; #9 is the same — system-wide, ticked when the full feature set has role gating verified.

**Repo health:**
- ruff: clean (`app tests`)
- mypy: clean (strict on `app/`, 12 source files — `app/taxonomy.py` added)
- pytest unit + integration: 262 passing (was 204, +58: 10 new in `test_taxonomy.py` + 43 new in `test_taxonomy_routes.py` + 5 new layout tests for nav-taxonomy)
- Playwright e2e: 7 passing (was 6, +1 — `test_taxonomy_e2e.py`)

**What's running:** Suppliers, Locations, and Taxonomy CRUD all live under `/admin/...`, all Manager-owned, all with the same shape (active/archived tabs, audit on every change, idempotent archive/unarchive, name-unique-across-archive, CSRF-protected POSTs, flash on success). Taxonomy adds a `sort_order` column and a `parent_id` self-FK that S3 routes never accept from form input — schema is sub-category-ready but the routes are top-level only. Two partial unique indexes (`uq_taxonomy_top_name` for `parent_id IS NULL`, `uq_taxonomy_child_name` for `parent_id IS NOT NULL`) handle sibling-scoped uniqueness across both shapes; both span archived rows. The audit vocabulary now includes `taxonomy_node.{created,updated,archived,unarchived}` with `entity_type="taxonomy_node"`. Nav adds a "Taxonomy" link for Manager + Admin.

**Known broken:** none.

---

## Current slice

*(none — slice S3 just shipped, see Completed log)*

---

## Next slice

**Slice name:** S4 — Taxonomy: sub-categories under a category (Manager-owned)
**Targets DoD item(s):** #2 (Manager defines taxonomy), #8 (audit on sub-category mutations), #9 (continued role gating).
**Why this next:** S3 schema is sub-category-ready (the `(parent_id, name)` partial unique index is in place; the unit tests already exercise sibling-scoped uniqueness). S4 surfaces sub-cats through the routes: a sub-cat list nested under each top-level node, a "new sub-category" form bound to a parent, and the same audit/archive/unarchive vocabulary scoped to non-null parent_id. The depth limit (max two levels) needs to be enforced at the application layer when creating a sub-cat. This is also when we re-evaluate whether suppliers/locations/taxonomy helper duplication is worth collapsing — S4 will tell us if sub-cats fit the same shape or branch off.

---

## Backlog (rough order of attack)

These are sliced small. Each should be one to three iterations of work. Reorder as needed; the only hard rule is that earlier slices should unblock later ones.

### Foundations
- **F1** Project skeleton and verification harness *(see Next slice)*
- **F2** Google SSO login + pending-state user model + role enum
- **F3** Audit log infrastructure (append-only, hooks for state changes)
- **F4** Base layout, navigation, role-aware menu, HTMX wired in

### Settings (Manager-owned)
- **S1** Suppliers CRUD
- **S2** Locations CRUD
- **S3** Taxonomy: top-level categories CRUD
- **S4** Taxonomy: sub-categories under a category
- **S5** Taxonomy: custom field defs per leaf node (text, number, decimal, date, boolean, select, multiselect)
- **S6** Field schema versioning behaviour (existing items keep values; new edits enforce current schema; deletes hide-not-purge)

### Items
- **I1** Item core fields + create/edit/archive against a chosen leaf node
- **I2** Item custom fields rendered + validated from the leaf node's schema
- **I3** Unique-tracked items: `item_units` table, per-unit serial labels
- **I4** QR code generation + printable label view

### Stock movements & cost
- **M1** `cost_layers` + `cost_layer_consumptions` tables and the FIFO consumption engine (pure logic, well-tested before any UI)
- **M2** Manual stock-in form (qty + unit cost → creates layer + "in" movement)
- **M3** Manual stock-out form (qty → consumes layers FIFO → records consumptions + "out" movement)
- **M4** Adjustment movements (positive creates layer, negative consumes FIFO; reason required)
- **M5** Transfer between locations (no cost change)
- **M6** Item detail page: current qty, open layers, full movement history

### Scanning
- **SC1** Scan-mode page (USB scanner: focused input, action picker, qty entry)
- **SC2** Camera-based scanning on phone/tablet (use a maintained JS QR lib via CDN)
- **SC3** Bulk scan mode for stock takes

### Check-out / check-in
- **C1** Per-item `requires_checkout` flag
- **C2** Check-out flow (assign to user, expected return)
- **C3** Check-in flow + condition note
- **C4** Manager view: currently-out + overdue list

### Stock takes
- **ST1** Stock take scheduling and scope (by node, by location, custom set)
- **ST2** Stock take session UI (start, count, see variance, commit adjustments)
- **ST3** Variance reports + linkage from adjustment movements back to the stock take

### Reorder & POs
- **PO1** Reorder dashboard (items below threshold, grouped by supplier)
- **PO2** Draft PO from low-stock selection (lines with expected unit cost editable)
- **PO3** PO PDF generation
- **PO4** Email PO to supplier (SMTP, console backend in dev)
- **PO5** Receive against PO (full / partial), enter actual unit cost per line, create cost layers and "in" movements

### Reporting
- **R1** Dashboard: total inventory value (from open layers), low-stock count, overdue checkouts
- **R2** Top consumed items over a window
- **R3** Cost-of-goods-consumed for a date range
- **R4** Stock-take variance trend
- **R5** CSV export on every list view

### Polish & deploy
- **P1** Mobile responsiveness pass (workshop tablets, 10" target)
- **P2** Accessibility pass (keyboard nav, contrast, focus states)
- **P3** Postgres parity: run full test suite against Postgres in CI
- **P4** Deployment config (Fly.io or Render) + production env handling
- **P5** README finalisation: setup, deploy, configure SSO, common workflows

---

## Definition of Done tracker

From MISSION.md §7. Tick only when verified by tests AND a manual sanity-check.

- [ ] 1. New user signs in with Google → pending → Admin assigns role → access works.
- [ ] 2. Manager defines taxonomy + custom fields. Admin creates items in those nodes (qty + unique). Required fields enforced. Archive/unarchive works.
- [ ] 3. Workshop user scans QR (USB on desktop AND camera on mobile), records in/out/adjust in two interactions. Stock-ins record unit cost.
- [ ] 4. Workshop user checks out / in tools and moulds. Manager sees who has what + overdue.
- [ ] 5. Office user runs a stock take end-to-end. Variances hit audit. Positive adj. requires cost; negative consumes FIFO.
- [ ] 6. Reorder dashboard → draft PO → edit expected costs → send PDF email → mark received with actual costs → cost layers created, valuation updates.
- [ ] 7. Dashboard shows value (FIFO), low stock, overdue, top consumed, COGS over date range.
- [ ] 8. Every state change in audit log with actor + timestamp. Audit log not editable.
- [ ] 9. Role-based access enforced server-side. Workshop hitting Manager URL = 403 (test-verified).
- [ ] 10. Full pytest, Playwright, ruff, mypy: zero failures / zero issues.
- [ ] 11. Runs locally on SQLite via single `make dev`. Runs in cloud config on Postgres with no code changes (env vars only).
- [ ] 12. README covers: local run, tests, deploy, SSO config, add supplier + item, run stock take. Written for a stranger.

---

## Completed slices (log)

*(Append-only. One line per shipped slice. Newest at the top.)*

| Iter | Slice | Commit | Notes |
|------|-------|--------|-------|
| 9 | S3 — Taxonomy: top-level categories CRUD (Manager-owned) | `981a8aa` | New Manager-only `/admin/taxonomy` server-rendered CRUD: list (active/archived tabs, ordered by `sort_order` then name), new, create, edit, update (with field-level audit diff), archive, unarchive. Migration `0005_create_taxonomy_nodes.py` adds the `taxonomy_nodes` table with self-referencing nullable `parent_id` (ON DELETE RESTRICT — soft-delete only), `sort_order` (int default 0), and two partial unique indexes: `uq_taxonomy_top_name` on `(name)` where `parent_id IS NULL` and `uq_taxonomy_child_name` on `(parent_id, name)` where `parent_id IS NOT NULL`. Both indexes span archived rows (matches Supplier/Location convention: archiving doesn't free the name). The same partial indexes are declared in `TaxonomyNode.__table_args__` so `Base.metadata.create_all` (used by unit tests) creates them too. New `app/taxonomy.py` router with `_FIELDS=("name","sort_order")`, a `_check_top_name_unique` helper that filters by `parent_id IS NULL`, a `_next_top_sort_order` that returns `max(existing) + 10` (or 0 if empty), and a `_normalise` that parses `sort_order` to int (400 on non-integer). Edit-form blank `sort_order` means "leave alone" — never silently resets to 0. Every route guards `node.parent_id is not None` and 404s — sub-cat ids cannot be edited/archived/unarchived through the top-level routes, leaving room for S4 to introduce sub-cat-aware variants. New "Taxonomy" nav link (data-testid `nav-taxonomy`) for Manager + Admin with `aria-current="page"` on `/admin/taxonomy*` paths. 58 new tests (10 unit `_taxonomy` covering top-level uniqueness, sub-cat sibling-scoped uniqueness across archived rows, name required, sort_order default; 38 integration `taxonomy_routes` covering role enforcement, list filters incl. excludes-sub-cats and orders-by-sort-order, create happy + whitespace strip + blank-sort-order steps-by-10 + duplicate name + invalid sort_order + parent_id-form-field-ignored + audit + no-audit-on-failure, edit happy + same-name + clash + empty + blank-sort-keeps-existing + diff-only-changed + no-op-no-audit + 404-on-sub-cat, archive/unarchive idempotency + audit + 404 + 404-on-sub-cat; 5 layout tests for nav-taxonomy visibility per role + aria-current) + 1 new e2e: pending → admin promotes → manager creates "Raw Materials" → archives → tab to archived → unarchive. 262 unit+integration + 7 e2e passing. mypy strict-clean (12 source files). |
| 8 | S2.5 — Extract shared `app.template_env` (drop `init_templates` plumbing) | `139d4f8` | Refactor only — no behaviour change. New `app/template_env.py` exposes a single module-level `templates: Jinja2Templates` pre-loaded with `csrf_context_processor` and a moved-and-renamed `flash_context_processor` (was `_flash_context_processor` in `app/main.py`). `app/main.py` now imports `templates` from there and no longer constructs a `Jinja2Templates` itself; the `suppliers_module.init_templates(templates)` and `locations_module.init_templates(templates)` shim calls are deleted. `app/suppliers.py` and `app/locations.py` drop their per-router `_templates` global, `init_templates()`, and `_t()` helper, and now import `templates` directly. Six rendering call-sites change from `_t().TemplateResponse(...)` to `templates.TemplateResponse(...)`. Net diff: −51 lines across the three routers and `main.py`, +25 lines in the new module. New unit suite `tests/unit/test_template_env.py` (6 tests) pins: `templates` is a `Jinja2Templates`, the search-path includes `app/templates/`, both context processors are registered on the instance, and `flash_context_processor` (a) pops the session entry, (b) returns `{"flash": None}` when there's no flash, (c) returns `{"flash": None}` when there's no session in scope (anonymous paths). All existing tests pass unchanged: 204 unit+integration (was 198, +6), 6 e2e (unchanged). mypy still strict-clean (now 11 source files including `app/template_env.py`). |
| 6 | S1 — Suppliers CRUD (Manager-owned) | `bac73b0` | New Manager-only `/admin/suppliers` server-rendered CRUD: list (active/archived tabs), new, create, edit, update (with field-level audit diff), archive, unarchive. Migration `0003_create_suppliers.py` adds the `suppliers` table (`name` unique). `Supplier` ORM model in `app/models.py`. New `app/suppliers.py` router mounted under the shared `Jinja2Templates` (with CSRF + new flash context processor) via a small `init_templates()` shim. `base.html` gains a **flash region** (`role="status"`, one-shot; `_flash_context_processor` pops `request.session["flash"]`) and a **Suppliers nav link** visible to Manager + Admin (with `aria-current="page"`). 48 new tests (10 unit `_suppliers` + 32 integration `suppliers_routes` + 6 layout for nav-suppliers/flash) + 1 new e2e: pending → admin promotes → manager creates "Acme Wax Co" → archives → tab to archived → unarchive. Idempotent archive/unarchive (no audit row on no-op). Validation: empty/whitespace name = 400, duplicate name = 400 (active and archived names share the namespace by design). 152 unit+integration + 5 e2e passing. |
| 5 | F4 — Base layout, role-aware nav, CSRF, `require_active_user` | `03f51a1` | New `app/csrf.py` with `CSRFMiddleware` (raw ASGI, double-submit cookie, header **or** form-field token, exempt set = `{/auth/google/callback, /auth/_dev-login}`) + `csrf_context_processor` registered on `Jinja2Templates`. New `require_active_user` dependency in `app/auth.py` applied to `/auth/me` (so a disabled user with a still-valid cookie immediately gets 403). Rebuilt `app/templates/base.html`: skip link, focus styles, header with email/status/role + CSRF-protected sign-out form, role-aware primary nav with `aria-current="page"`, HTMX 1.9.12 via CDN with SRI hash. `admin_users.html` forms render the hidden CSRF input. 25 new tests (8 unit `_csrf` + 11 integration `csrf` + 11 integration `layout` + 4 `require_active_user` unit + 2 disabled/pending `/auth/me` integration). Existing admin/audit/auth POST tests threaded with `_csrf(client)` helper. 104 unit/integration + 4 e2e passing. |
| 3 | F2.1 — Admin user-management UI: assign role, activate/disable users | `e80feb6` | `/admin/users` is now an HTML page with per-user role + status forms (POST → 303 redirect, no HTMX yet). New routes `POST /admin/users/{id}/role` + `/status` with server-side guards: admin can't demote themselves, can't disable themselves, can't activate a user with no role. Pending users sort to the top of the list. e2e covers the full round-trip: pending user signs in → admin promotes them → user signs back in and sees the welcome page. 43 unit/integration + 4 e2e passing. |
| 2 | F2 — Google SSO login + pending-state user model + role enum | `b46ee57` | `users` table (Alembic mig), `Role`/`UserStatus`, Authlib OAuth, signed sessions, `require_role`, role-gated `/admin/users`, anon/pending/welcome index, dev/test-only login backdoor for Playwright. Prod-config validator now requires non-default `SECRET_KEY` + Google creds. 27 unit/integration + 3 e2e passing. |
| 1 | F1 — Project skeleton and verification harness | `884cd46` | FastAPI app + `/health`, SQLAlchemy + Alembic wired, pytest + Playwright harness, `make check` green. |

---

## Self-critique notes (rolling)

*(Carry forward weaknesses noted during iteration self-critiques but not yet addressed. Clear an entry when it gets fixed in a later slice. Don't let this get longer than ~10 items — if it does, something has gone off the rails.)*

- **S3 / three concrete settings-CRUD shapes now live side by side (suppliers, locations, taxonomy).** Helper duplication is real but not uniform: taxonomy adds `sort_order` (int parse, blank-on-edit-keeps-existing, default-step-by-10) and a `parent_id is not None → 404` guard on every endpoint. Suppliers and locations don't have either. Collapsing all three into a generic helper now would mean either (a) hard-coding the union of all features (sort_order + email/phone + parent_id guard) into the helper, or (b) building a config-driven registry whose configs are barely simpler than the routes. Wait until S4 (sub-categories) — that slice will reuse `taxonomy_nodes` and either fit the existing shape (one more vote for extraction) or introduce a fourth variant (vote against). Re-evaluate at the end of S4.
- **S3 / sort_order UX is a raw integer input, not a reorder gesture.** MISSION §3 says "Manager can rename, archive, or **reorder** taxonomy nodes". The input technically lets the manager reorder by editing the number, but it's not the natural gesture (drag-drop, up/down arrows). Defer to a UI polish slice once HTMX is actually being used somewhere — the same control will likely apply to suppliers/locations if we add ordering there too. Not blocking S3's DoD progress.
- **S3 / the partial unique indexes rely on SQLite ≥ 3.8 partial-index support.** Pyproject pins Python 3.11+, which on every modern OS ships SQLite ≥ 3.39 (released 2022). `create_engine` doesn't sanity-check the SQLite version, so a pathologically old runtime would silently create non-unique indexes. The risk is theoretical — every macOS / Linux distro from the last 8+ years ships a sufficient SQLite — but worth flagging in case CI ever runs against a stripped container. Cheap fix: a startup-time `PRAGMA compile_options` check, or assert via SQLAlchemy's `dialect.server_version_info`.
- **S3 / sub-category schema is in place but unexercised through routes.** `(parent_id, name)` unique constraints are tested at the unit level (`tests/unit/test_taxonomy.py::TestTaxonomyConstraints`). The S3 routes 404 on any sub-cat id and ignore unsolicited `parent_id` form fields. When S4 lands, the new routes need to enforce the depth limit (parent's `parent_id` must be NULL — max two levels) at the application layer; the DB doesn't enforce that. Don't forget when implementing S4.
- ~~**S2 / route + template duplication ... has hit the extraction threshold.**~~ *Partially resolved in S2.5: the `init_templates`/`_t()` plumbing is gone — both routers now import the shared `templates` from `app.template_env` directly. The route-handler helper duplication (`_normalise`/`_validate_name`/`_check_name_unique`/`_diff`/`_flash`) is **deliberately left in place**: collapsing it now would mean inventing a `_FIELDS` registry to drive a generic helper before the third concrete shape (S3 taxonomy) is on the page. Re-evaluate once S3 lands. — **Update (S3):** Now that the third shape is on the page, see the new top-of-list note. The duplication grew non-uniform features (`sort_order`, `parent_id` guard); extraction is now harder, not easier. Wait until S4.*
- **S2.5 / `templates` is now a true module-level singleton.** Tests can't easily swap it for one with different processors. No test today needs that — TestClient uses the real app — but if a future test wants to render a template against a fake processor (e.g. to assert what the processor receives without booting Starlette), it'd have to monkey-patch the module attribute. Acceptable. Flag if/when a test needs it.
- **S2.5 / `app.template_env` test reads `templates.context_processors` directly.** That's a starlette public attribute today (set in `Jinja2Templates.__init__`) but isn't documented as stable surface. The test has a fallback to `templates.env.globals.get("__context_processors__", [])` which is hopeful — if starlette renames the attribute the test will fall through and silently say "no processors". Strictly the test should round-trip: render a tiny `<{{ csrf_token }}|{{ flash }}>` template through `TemplateResponse` and parse the output. Defer until/unless a starlette upgrade actually breaks the current attribute access.
- **S2 / no audit-action coverage for `location.*` in the existing audit-immutability tests.** `tests/integration/test_audit_immutability.py` exercises the trigger by inserting and trying to UPDATE arbitrary rows, which is fine. But it doesn't enumerate the action vocabulary. Adding `location.*` extends the implicit vocabulary; if a future trigger added a CHECK on `action` (it doesn't currently) we'd silently break it. Today this is fine — flagged in case the audit-viewer UI (M6 territory) adds a fixed action allowlist.
- **S2 / inherits S1's UX gaps verbatim.** Bare 400 page on validation failure, no pagination, no e2e on the negative-role path, archived-name conflict message doesn't say *which* row it clashed with. All four are already in this list under the S1 entries; S2 doesn't make any of them worse, but it doubles the cost of fixing each one (now two routes have to be updated, soon three). When the "settings UX polish" slice lands, do all the routers in one pass.
- **S1 / validation failures show a bare 400 page, not the form re-rendered with errors.** A user submits an empty name and gets "name is required" as a stark FastAPI HTTPException page instead of the form with the field highlighted. Mirrors the existing `admin_users` route convention but isn't great UX. The `suppliers_form.html` template already takes a `form` dict and renders submitted values, so the upgrade is small: swap `HTTPException(400, ...)` for `templates.TemplateResponse(..., status_code=400, ...)` with an `errors` dict. Defer to a "settings UX polish" slice (after S2/S3 land so the change covers all settings forms at once).
- **S1 / no email format validation.** Browser `type="email"` validates client-side but not server-side. Storing garbage emails will bite at PO send time (PO5). MISSION says "boring is good" — defer to a single validation point at PO5 (cheaper than re-validating everywhere).
- **S1 / DoD #9 has integration coverage but not e2e coverage on the negative path.** Workshop=403, Office=403, anonymous=401 are exhaustively integration-tested for `/admin/suppliers`. The e2e only covers the positive path (manager succeeds). Strictly the DoD says "verified by tests" (plural, not specifically e2e). I'm leaving #9 unticked for now — being conservative; consider in the next iteration whether to (a) tick as-is, or (b) add a one-liner e2e that has a workshop user navigate to `/admin/suppliers` and assert a 403 page renders. Cheap addition.
- **S1 / no pagination on the suppliers list.** The list view renders every active or archived row inline. UC has tens of suppliers, not thousands, so this is fine for v1. Worth flagging because the same template will be cloned for items (I1+), where 5000 rows IS realistic. Don't copy the bare-list pattern there.
- **S1 / archived names share the namespace with active names.** Tested + intentional: archiving doesn't free the name. Operator must rename the old row or unarchive it. The 400 message ("a supplier with that name already exists") doesn't tell the user *which* one — confusing if the conflict is with an archived supplier they don't see by default. Defer to a UX polish slice.
- **F4 / CSRF doesn't cover multipart bodies.** `_extract_submitted_token` reads only `application/x-www-form-urlencoded`. Multipart form-posts (file uploads — not yet a feature) must send the token in the `X-CSRF-Token` header instead, or they'll fail CSRF. When the first upload route lands (likely with item photos or PO PDFs), either extend the middleware to parse multipart or document the header requirement explicitly on those routes. Captured in the docstring of `app/csrf.py` so the next dev sees it.
- ~~**F4 / no flash-message region in the layout.**~~ *Resolved in S1: `_flash_context_processor` registered on `Jinja2Templates`, base layout renders `<div class="flash" role="status" data-testid="flash">` when `request.session["flash"]` is set; supplier mutations populate it. Tested in `tests/integration/test_layout.py::TestFlashRegion`.*
- ~~**F4 / nav for active non-admin users renders just "Home".**~~ *Partially resolved in S1: Manager + Admin now also see "Suppliers". Workshop and Office still see only "Home" until M2 (stock-in form) and SC1 (scan-mode page) land.*
- **F4 / CSRF token doesn't rotate on login or logout.** Same token persists across sign-in/sign-out cycles for as long as the cookie lives. Threat model says fine: SameSite=Lax + non-HttpOnly-but-same-origin means a cross-site attacker can't read or set the cookie. The window where this matters is a session-fixation-style attack where attacker plants a known csrftoken cookie in the victim's browser before login — they can't, because they can't write our domain's cookies cross-site. Document and move on.
- **F4 / HTMX is loaded but unused.** No route emits HTML fragments yet. The `<script>` tag is there for the next slice to start using; the integrity hash pins us to 1.9.12 — bumping requires updating both the URL and the SRI.
- **F3 / no audit-viewer UI.** Today the audit log is observable only via direct DB access. That's enough for tests and for an eyeball on a dev DB, but for "Manager can see who has what" / "Admin can investigate an incident" we'll need a real `/admin/audit` (or per-entity timeline) view. Belongs alongside the item-detail timeline (M6 territory) — building it earlier means rendering rows for actions that don't exist yet. Not blocking DoD #8.
- **F3 / returning-user name/email refresh is silent.** `upsert_user_from_userinfo` updates `email`/`name` from Google on every sign-in but writes no audit row, so a Google-side rename is invisible. Strict reading of "every state-changing action" suggests a `user.profile_synced` row should be written when Google's payload differs from what's stored. Cheap fix (adds 5 lines + a test), can land in F4 or later.
- **F3 / migration → trigger linkage isn't tested end-to-end.** `apply_immutability_triggers` is exercised by unit tests that install it themselves; the migration does call it on `op.get_bind()`, but no test asserts that running `alembic upgrade head` against a clean DB produces tamper-proof tables. The e2e suite runs the migration but never tries to UPDATE/DELETE audit rows. Risk: a future refactor drops the trigger call, tests stay green, prod is silently unprotected. Mitigate with a small "post-migration tamper test" — run `alembic upgrade head` against a temp SQLite, attempt UPDATE, expect failure.
- **F3 / Postgres path is implemented but untested.** The `plpgsql` block-function + triggers are written but no CI run exercises them. Acceptable for v1 — covered when P3 (Postgres parity) lands. Worth a sanity-read before that slice.
- ~~**F2.1 / still no CSRF on POSTs, and now there are real ones.**~~ *Resolved in F4: `CSRFMiddleware` with double-submit cookie now gates every state-changing route except the two intentional exemptions (`/auth/google/callback`, `/auth/_dev-login`). Tested in `tests/integration/test_csrf.py`.*
- **F2.1 / no last-admin guard.** Server prevents an admin from demoting/disabling *themselves*, but not from demoting/disabling another admin — including the only other admin. Sequence to lock everyone out: A and B are admins; A demotes B to manager (allowed), then disables themselves... no, that's blocked. But: A demotes B *and* disables themselves... still blocked on self-disable. Genuine lock-out path: A demotes themselves (blocked), so really just: A disables B → A leaves → no one can manage. Recovery requires direct DB access or wiping admins so the bootstrap path re-fires. Not blocking, but worth a 30-line slice ("cannot remove the last active admin") soon.
- ~~**F2.1 / `/auth/me` doesn't enforce status.**~~ *Resolved in F4: `/auth/me` now depends on `require_active_user`, which 403s any non-active user (pending or disabled) even with a valid cookie. Tested in `tests/integration/test_auth_routes.py::TestAuthMe::test_disabled_user_with_valid_session_is_403` and `::test_pending_user_with_valid_session_is_403`.*
- ~~**F2 / no CSRF on POSTs.**~~ *Resolved in F4. `/auth/logout` now requires CSRF; `/auth/_dev-login` remains intentionally exempt as documented in `app/csrf.py` (it's a non-prod-only backdoor and CSRF on a backdoor protects nothing).*
- **F2 / dev-login backdoor exists in `dev` too, not just `test`.** Useful for local hacking, but it is a real backdoor: anyone who can reach the dev server can sign in as anyone. Document this in the README (done) and consider tightening to `test`-only (or requiring an env-var token) once we have a real dev workflow.
- **F2 / migration's CHECK constraint duplicates the enum members.** `migrations/versions/0001_create_users.py` hard-codes `('admin','manager','office','workshop')` and `('pending','active','disabled')`. Adding a new `Role` member without a migration update would silently make the model accept values the DB rejects. Tests would catch it (any insert with the new value would fail), but consider dropping the CHECK in favour of just trusting the SAEnum, or generating the constraint from the enum at migration time.
- ~~**F2 / no audit on user creation/promotion.**~~ *Resolved in F3: `upsert_user_from_userinfo` now writes `user.created` (+ `user.bootstrap_admin_granted` for the seed admin path), tested in `tests/integration/test_audit_routes.py::TestUpsertUserAudit`.*
- ~~**F2 / templates lack accessibility polish.**~~ *Resolved in F4: skip-link, visible focus ring, `aria-current="page"` on the active nav item, larger tap targets. Tested in `tests/integration/test_layout.py::TestBaseLayoutAccessibility`. Future passes (P2 in the backlog) can audit deeper (contrast ratios, screen-reader walk-through, etc.) once there are more pages.*
- **F1 / e2e fixture readiness probe (carried).** `tests/e2e/conftest.py` still uses TCP-port-open as readiness. Hasn't flaked in F2; revisit only if it does.

---

## Open questions

*(Things the loop noticed that need a human decision. Continue working on unblocked slices while these sit here. Do not invent answers.)*

- *(empty)*

---

## Proposed scope changes

*(If the loop finds a reason to expand or shrink scope vs MISSION.md, log the proposal here and continue with the original scope. The user reviews and either edits MISSION.md or rejects.)*

- *(empty)*

---

## Decisions log

*(Non-trivial implementation decisions worth remembering. One line each. Helps future iterations not re-litigate settled choices.)*

- **Alembic env.py reads `DATABASE_URL` from `app.config.settings`**, not from `alembic.ini`. Single source of truth across dev/prod, and means `alembic` invocations honour `.env` automatically.
- **`render_as_batch=True` on SQLite** in `migrations/env.py` so future ALTER TABLE migrations work on SQLite (which doesn't support most ALTER ops natively). Postgres ignores the flag.
- **e2e fixture spawns a real uvicorn subprocess** rather than using ASGI in-process. Trades startup cost (~2s) for fidelity to the production transport. Acceptable while the e2e suite is small.
- **e2e suite uses an isolated temp SQLite file + runs `alembic upgrade head` before booting uvicorn** (rather than `Base.metadata.create_all`), so e2e exercises the actual migration path. This is what caught the enum-name-vs-value mismatch in F2.
- **Enum columns store enum values, not names** — `SAEnum(..., values_callable=lambda cls: [e.value for e in cls])`. Without this, SAEnum stores the Python member name (e.g. `"PENDING"`) which doesn't match our migration's lowercase CHECK constraints. Apply the same `values_callable` to every future enum column.
- **Users use a separate `status` enum (pending/active/disabled) and a *nullable* `role`**, rather than overloading role with a `pending` value. Reason: status is orthogonal to role — a Manager can be temporarily disabled without losing their assigned role. This costs one column and keeps the role enum honest.
- **Bootstrap admin promotion is one-shot.** Once any admin exists, `BOOTSTRAP_ADMIN_EMAIL` matches no longer auto-promote. Without this, leaving the env var set in prod would silently grant admin to anyone matching it on first sign-in.
- **`require_role` blocks pending and disabled users even if their role would otherwise match.** Status overrides role for access decisions. Tested explicitly so future refactors don't quietly weaken this.
- **Dev-login backdoor `POST /auth/_dev-login`** is mounted in `dev` and `test` only, gated by `settings.app_env`. Used by the Playwright suite (and local dev) to sign in without a real Google round-trip. Hard-disabled in prod by the env check at module import + a redundant runtime check inside the handler.
- **`record_audit` flushes inside the caller's transaction; the caller commits.** The audit row and the change it records succeed-or-fail together. Helper never commits — that's a deliberate constraint so a future bug can't half-commit a state change without its log entry.
- **`actor=user` for `user.created`** (not `actor=None`). The user is treated as the actor of their own first-time creation; the bootstrap admin promotion is logged as a separate `actor=None` system event. Rationale: keeps every "user-initiated" event attributable to a human, even when the human is the subject.
- **Audit log immutability is enforced at the DB layer.** SQLite uses `BEFORE UPDATE`/`BEFORE DELETE` triggers with `RAISE(ABORT, 'audit_log is append-only: …')`; Postgres uses an equivalent `plpgsql` function. The trigger SQL lives in `app/audit.py` so the migration and any test fixture share one source of truth (`apply_immutability_triggers`).
- **CSRF uses double-submit cookie, not session-stored token.** Token lives in a non-HttpOnly `csrftoken` cookie + a matching `csrf_token` form field or `X-CSRF-Token` header. Reasoning: the session cookie is encrypted/signed and not readable by tests/JS, which would force every caller to first parse the rendered HTML to discover the token. Double-submit makes the token discoverable to legitimate callers (HTMX, fetch, Playwright) while `SameSite=Lax` keeps the cookie out of cross-site requests.
- **CSRF middleware runs outside SessionMiddleware.** `CSRFMiddleware` is added second in `app/main.py` so it's the *outermost* middleware. Forged requests get rejected before SessionMiddleware ever decodes the session cookie. Cost: anonymous forged POSTs return 403 before authentication runs, which is why `test_unauthenticated_post_is_401` has to bootstrap a CSRF cookie first.
- **`/auth/_dev-login` is CSRF-exempt by design.** The dev-login backdoor is mounted only in `dev`/`test`. Adding CSRF to a backdoor that bypasses real auth would only add ceremony, not security — anyone who can reach the dev server already has full access. This is documented in `app/csrf.py::DEFAULT_EXEMPT_PATHS` so the next dev doesn't quietly "fix" it.
- **`require_active_user` is a separate dependency from `require_role`.** Pre-F4, `require_user` only checked "is there a user?" and `require_role` checked status as a side-effect. Routes that don't have role gating but should still reflect current account state (today: `/auth/me`) need their own status check — added as `require_active_user`. Without it, a user disabled mid-session continues to read their own profile via a still-valid cookie until they sign out.
- **Suppliers (and other Manager-owned settings) live under `/admin/...`** even though "admin" in the URL implies the admin role. Reasoning: the existing `/admin/users` set the `admin` prefix as "admin/settings area," not strictly "admin role." The role gate is in the dependency (`require_role(Role.MANAGER)` for suppliers, `require_role(Role.ADMIN)` for users), not the URL. Don't read the URL prefix as a role signal.
- **Suppliers `name` is unique across active *and* archived rows.** Archiving does not free the name. To re-use a name the operator must rename the existing row or unarchive it. Reason: PO history and FIFO layers may reference a supplier by id, but humans reference by name; allowing two "Acme Wax Co" rows (one archived, one active) would silently let the wrong one feed reorder math. Tested at the DB layer (`uq_suppliers_name`) and at the route layer (`_check_name_unique`).
- **No-op POSTs (update with no field changes, archive an already-archived supplier, unarchive an active one) write no audit row but still 303 to the list.** Reason: tests assert this explicitly so a future refactor doesn't accidentally start logging spurious "X updated" rows that drown the real signal. The 303 still happens so the browser's POST-redirect-GET cycle terminates cleanly.
- **`record_audit` for `supplier.updated` records *only* the changed fields, not the full row.** `_diff()` returns a sparse `(before, after)` of fields whose value changed. Reason: full-row before/after gets noisy fast (especially with `notes`), and the DoD requirement is that "every state change is attributable" — knowing exactly what changed beats a lossy snapshot of everything.
- **Flash messages live in `request.session["flash"]` and are popped (read+clear) by `flash_context_processor` on render.** One-shot semantics: appears on the next page render, never on the one after. Tested in `test_layout.py::TestFlashRegion::test_flash_appears_after_set_then_cleared_on_next_load` and `tests/unit/test_template_env.py::TestFlashContextProcessor`. Don't try to "tag" flashes with a type (success/warning/etc.) until there's a real second type to render — current uses are all success messages.
- **The shared `Jinja2Templates` instance lives in `app.template_env` as a module global.** Every router (today: `app.main`, `app.suppliers`, `app.locations`; tomorrow: taxonomy + items + everything else) imports `templates` from there. Reason: the app needs a single Jinja env so the CSRF + flash context processors are guaranteed to be present on every render — a router building its own `Jinja2Templates` would silently drop both. Module-level is fine because the configuration is pure (template directory + two pure-function processors). *(Supersedes the earlier "`app.suppliers.init_templates()` shim" decision from S1.)*
