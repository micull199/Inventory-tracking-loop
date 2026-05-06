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

**Iteration:** 0
**Last commit:** none
**Branch:** main
**Tests:** not yet established
**Definition-of-Done items ticked:** 0 / 12

**Repo health:**
- ruff: not yet configured
- mypy: not yet configured
- pytest: not yet configured
- Playwright: not yet configured

**What's running:** nothing yet. No app, no DB, no migrations.

**Known broken:** N/A (nothing exists yet).

---

## Current slice

*(Populated at the start of each iteration with the slice being worked on. Cleared on commit.)*

**Slice name:**
**Targets DoD item(s):**
**Plan:**
1.
2.
3.

**Tests added:**
-

**Self-critique:**
-

---

## Next slice

*(Populated at the end of an iteration if the next move is obvious. Otherwise empty, and the next iteration picks from Backlog.)*

**Slice name:** Project skeleton and verification harness
**Targets DoD item(s):** 10, 11, 12
**Why this first:** Nothing else can be verified until the test/lint/type pipeline exists. Without a verification signal the loop is flying blind.
**Sketch:**
- Initialise project: `app/` package, `pyproject.toml`, ruff + mypy config, `pytest.ini`, `Makefile` with `dev`, `test`, `lint`, `typecheck`, `e2e` targets.
- Minimal FastAPI app with one health endpoint.
- SQLAlchemy + Alembic wired up, SQLite for dev.
- One smoke test (pytest) and one Playwright test hitting the health endpoint.
- README skeleton with the local-run instructions.
- `.env.example`, `.gitignore`, initial commit.

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

---

## Self-critique notes (rolling)

*(Carry forward weaknesses noted during iteration self-critiques but not yet addressed. Clear an entry when it gets fixed in a later slice. Don't let this get longer than ~10 items — if it does, something has gone off the rails.)*

- *(empty)*

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

- *(empty)*
