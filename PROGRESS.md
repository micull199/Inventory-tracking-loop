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

**Iteration:** 2 (complete)
**Last commit:** b46ee57 — slice: F2 — Google SSO + users + roles
**Branch:** main
**Tests:** `make check` green: ruff ✓, mypy ✓, pytest 27 unit+integration ✓, Playwright 3 e2e ✓.
**Definition-of-Done items ticked:** 0 / 12 (DoD #1 and #9 advanced — sign-in + pending state + server-side role enforcement all work; not ticked because the admin "assign role" UI doesn't exist yet, so the full DoD #1 happy path is not end-to-end demoable yet).

**Repo health:**
- ruff: clean (`app tests`)
- mypy: clean (strict on `app/`, 6 source files)
- pytest unit + integration: 27 passing
- Playwright e2e: 3 passing (chromium)

**What's running:** FastAPI app with sessions + Google SSO (Authlib) + dev-login backdoor (test/dev only). Index page that branches anonymous / pending / welcome. Admin-only `/admin/users` JSON listing. SQLAlchemy `User` + `Role`/`UserStatus` enums backed by an Alembic-managed `users` table.

**Known broken:** none.

---

## Current slice

*(none — slice F2 just shipped, see Completed log)*

---

## Next slice

**Slice name:** F2.1 — Admin user-management UI: assign role, activate/disable users
**Targets DoD item(s):** 1 (will tick once this lands and the round-trip is e2e demoable)
**Why this next:** F2 created pending users but there's no way for an admin to approve them, so DoD #1 isn't fully closed. This slice is the smallest follow-on that closes it.
**Sketch:**
- HTML page at `/admin/users` (replace JSON-only response, or add an HTML variant gated on `Accept`).
- POST endpoints: assign-role + toggle-status. Server-side role check stays.
- Audit log isn't built yet (F3) — for now we trust the existing `request.state` actor; F3 will hook this in retroactively without changing the route shape.
- Tests: integration (admin can promote pending → active manager; non-admin POST gets 403; admin can't disable themselves — guardrail). Playwright: admin signs in via dev-login → opens admin users → assigns Workshop to a pending user → that user reloads and sees the welcome page.

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
| 2 | F2 — Google SSO login + pending-state user model + role enum | `b46ee57` | `users` table (Alembic mig), `Role`/`UserStatus`, Authlib OAuth, signed sessions, `require_role`, role-gated `/admin/users`, anon/pending/welcome index, dev/test-only login backdoor for Playwright. Prod-config validator now requires non-default `SECRET_KEY` + Google creds. 27 unit/integration + 3 e2e passing. |
| 1 | F1 — Project skeleton and verification harness | `884cd46` | FastAPI app + `/health`, SQLAlchemy + Alembic wired, pytest + Playwright harness, `make check` green. |

---

## Self-critique notes (rolling)

*(Carry forward weaknesses noted during iteration self-critiques but not yet addressed. Clear an entry when it gets fixed in a later slice. Don't let this get longer than ~10 items — if it does, something has gone off the rails.)*

- **F2 / no CSRF on POSTs.** `POST /auth/logout` and (in dev/test) `POST /auth/_dev-login` are not CSRF-protected. The session cookie uses `SameSite=Lax`, which mitigates most cross-origin POSTs, but is not a substitute. Must land a CSRF middleware (likely a per-session token rendered into forms via Jinja) before the first user-facing form-post slice ships (S1/S2/F4). Reason this isn't blocking now: logout is a no-op for an attacker, and `/_dev-login` is gated on non-prod environments.
- **F2 / dev-login backdoor exists in `dev` too, not just `test`.** Useful for local hacking, but it is a real backdoor: anyone who can reach the dev server can sign in as anyone. Document this in the README (done) and consider tightening to `test`-only (or requiring an env-var token) once we have a real dev workflow.
- **F2 / migration's CHECK constraint duplicates the enum members.** `migrations/versions/0001_create_users.py` hard-codes `('admin','manager','office','workshop')` and `('pending','active','disabled')`. Adding a new `Role` member without a migration update would silently make the model accept values the DB rejects. Tests would catch it (any insert with the new value would fail), but consider dropping the CHECK in favour of just trusting the SAEnum, or generating the constraint from the enum at migration time.
- **F2 / no audit on user creation/promotion.** Slice F3 (audit log infrastructure) will retroactively wire this in. Acceptable — DoD #8 says audit works for "every state change", which is verified by F3's tests, not F2's.
- **F2 / templates lack accessibility polish.** No skip-link, no focus styles, no aria-current on nav. Slice F4 (base layout pass) is the home for this. The pages are keyboard-navigable as-is (one link per page) so usability is acceptable.
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
