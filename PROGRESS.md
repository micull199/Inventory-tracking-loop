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

**Iteration:** 1 (complete; commit pending)
**Last commit:** f51cc78 — Initial scaffolding (slice F1 commit appended below)
**Branch:** main
**Tests:** harness established. `make check` green: ruff ✓, mypy ✓, pytest (2 unit + 1 e2e) ✓.
**Definition-of-Done items ticked:** 0 / 12 (#10 and #11 partially advanced; not ticked — no auth, no real features yet)

**Repo health:**
- ruff: clean
- mypy: clean (strict on `app/`)
- pytest unit + integration: 2 passing (1 real, 1 placeholder)
- Playwright e2e: 1 passing (chromium)

**What's running:** FastAPI app with `/health` endpoint. SQLAlchemy + Alembic wired. SQLite dev DB.

**Known broken:** none.

---

## Current slice

*(none — slice F1 just shipped, see Completed log)*

---

## Next slice

**Slice name:** F2 — Google SSO login + pending-state user model + role enum
**Targets DoD item(s):** 1, 9
**Why this next:** Every other Manager/Office/Workshop-gated slice depends on roles existing and being enforceable. SSO + the `users` table + the `Role` enum + a `require_role` dependency unlock all of them.
**Sketch:**
- `users` model: id, google_sub, email, name, role (enum: admin/manager/office/workshop), status (pending/active/disabled), timestamps.
- First Alembic migration creates `users`.
- Authlib OAuth flow: `/auth/google/login` → redirect → `/auth/google/callback` → upsert user (pending unless email matches `BOOTSTRAP_ADMIN_EMAIL`).
- Session middleware (itsdangerous, signed cookies).
- `current_user` dependency + `require_role(*roles)` factory. Pending users get a holding page, not 403.
- Tests: unit for the role-check helper; integration for OAuth callback (mock the Google response); Playwright for the login → pending-page flow.

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
| 1 | F1 — Project skeleton and verification harness | _(pending)_ | FastAPI app + `/health`, SQLAlchemy + Alembic wired, pytest + Playwright harness, `make check` green. |

---

## Self-critique notes (rolling)

*(Carry forward weaknesses noted during iteration self-critiques but not yet addressed. Clear an entry when it gets fixed in a later slice. Don't let this get longer than ~10 items — if it does, something has gone off the rails.)*

- **F1 / config**: `secret_key` has a dev default `"change-me"` with a `noqa: S105`. Acceptable for local-only F1 but must be required (no default → pydantic raises) before the auth slice (F2) ships. Loosely enforced in prod by env override; tighten when SECRET_KEY actually does something.
- **F1 / e2e fixture**: `tests/e2e/conftest.py` boots uvicorn with `sys.executable -m uvicorn`. Works because pytest itself runs in the venv, but the fixture has no readiness probe beyond TCP-port-open — an early-startup crash that still binds the port would make us hang. Revisit if e2e flakes appear.
- **F1 / no CSRF middleware yet**: MISSION §4 mandates CSRF on mutating routes. Health is GET-only so this slice is fine, but the dependency must land in F2/F3 alongside session middleware before any POST route exists.
- **F1 / no template/static infra**: Jinja2/HTMX scaffolding deferred to slice F4 ("base layout"). Health is JSON-only, so deferral is intentional, not an oversight.

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
- **`tests/integration/test_placeholder.py`** exists only to keep `pytest tests/unit tests/integration` from exiting non-zero (no-tests-collected). Delete once the first real integration test lands in F2.
