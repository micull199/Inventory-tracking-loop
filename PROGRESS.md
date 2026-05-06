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

**Iteration:** 5 (complete)
**Last commit:** *(pending — F4 about to land)*
**Branch:** main
**Tests:** `make check` green: ruff ✓, mypy ✓, pytest 104 unit+integration ✓, Playwright 4 e2e ✓.
**Definition-of-Done items ticked:** 0 / 12. F4 lands the CSRF middleware + `require_active_user` + base layout + role-aware nav, which the rest of the app depends on. None of the 12 DoD bullets become tickable from this slice alone — DoD #9 needs a non-admin role-gated route in place to be properly verified end-to-end (S1 will land that). DoD #1 / #8 status unchanged.

**Repo health:**
- ruff: clean (`app tests`)
- mypy: clean (strict on `app/`, 8 source files)
- pytest unit + integration: 104 passing
- Playwright e2e: 4 passing (chromium)

**What's running:** FastAPI app with sessions + Google SSO (Authlib) + dev-login backdoor (test/dev only) + **CSRF middleware (double-submit cookie)** + **`require_active_user` dependency on `/auth/me`**. New base layout with role-aware nav (Home + Users for admins; aria-current on active page), header with sign-out form (CSRF-protected), skip-link, focus styles, HTMX loaded once via CDN. Admin-only `/admin/users` HTML page with per-user role + status assignment forms (POST + 303 redirect, self-mutation guarded, CSRF token rendered into every form). SQLAlchemy `User` + `Role`/`UserStatus` enums backed by an Alembic-managed `users` table. Append-only `audit_log` table with `record_audit()` helper wired into user creation, bootstrap admin promotion, role assignment, and status change. DB-level UPDATE/DELETE triggers (SQLite + Postgres) reject any post-hoc edit.

**Known broken:** none.

---

## Current slice

*(none — slice F4 just shipped, see Completed log)*

---

## Next slice

**Slice name:** S1 — Suppliers CRUD (Manager-owned, server-rendered, CSRF + nav now in place)
**Targets DoD item(s):** First-half of #6 (suppliers feed PO drafts) and #2 (Manager surface for taxonomy/settings — start with suppliers because they have no schema versioning to think about).
**Why this next:** F4 just shipped CSRF, role-aware nav, and a base layout. Suppliers are the simplest CRUD entity in §6 — no taxonomy, no FIFO, no children — so they're the cheapest way to (a) prove the new CSRF + nav stack on a non-admin route, (b) tick the first piece of #2, (c) verify DoD #9 with a real Manager-only route that 403s a Workshop user. Adds a Manager nav link and a `Manager`-only `/admin/suppliers` route.
**Sketch (refine in iteration):**
- `suppliers` table + Alembic migration. Columns: id, name (unique), email, phone, notes, archived_at, timestamps.
- ORM `Supplier` model.
- Routes (Manager + Admin):
  - `GET /admin/suppliers` — list (active + archived tabs).
  - `GET /admin/suppliers/new`, `POST /admin/suppliers` — create.
  - `GET /admin/suppliers/{id}/edit`, `POST /admin/suppliers/{id}` — edit.
  - `POST /admin/suppliers/{id}/archive`, `POST /admin/suppliers/{id}/unarchive` — soft delete / restore.
- Audit: every mutation hits `record_audit` with `entity_type="supplier"`.
- Nav link in `base.html` for Manager + Admin.
- Tests: model unit tests; integration for each route incl. role enforcement (Workshop = 403; Office = 403 since suppliers are Manager-owned per §3); e2e: Manager creates a supplier → sees it in the list → archives it.

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
| 5 | F4 — Base layout, role-aware nav, CSRF, `require_active_user` | *(pending)* | New `app/csrf.py` with `CSRFMiddleware` (raw ASGI, double-submit cookie, header **or** form-field token, exempt set = `{/auth/google/callback, /auth/_dev-login}`) + `csrf_context_processor` registered on `Jinja2Templates`. New `require_active_user` dependency in `app/auth.py` applied to `/auth/me` (so a disabled user with a still-valid cookie immediately gets 403). Rebuilt `app/templates/base.html`: skip link, focus styles, header with email/status/role + CSRF-protected sign-out form, role-aware primary nav with `aria-current="page"`, HTMX 1.9.12 via CDN with SRI hash. `admin_users.html` forms render the hidden CSRF input. 25 new tests (8 unit `_csrf` + 11 integration `csrf` + 11 integration `layout` + 4 `require_active_user` unit + 2 disabled/pending `/auth/me` integration). Existing admin/audit/auth POST tests threaded with `_csrf(client)` helper. 104 unit/integration + 4 e2e passing. |
| 3 | F2.1 — Admin user-management UI: assign role, activate/disable users | `e80feb6` | `/admin/users` is now an HTML page with per-user role + status forms (POST → 303 redirect, no HTMX yet). New routes `POST /admin/users/{id}/role` + `/status` with server-side guards: admin can't demote themselves, can't disable themselves, can't activate a user with no role. Pending users sort to the top of the list. e2e covers the full round-trip: pending user signs in → admin promotes them → user signs back in and sees the welcome page. 43 unit/integration + 4 e2e passing. |
| 2 | F2 — Google SSO login + pending-state user model + role enum | `b46ee57` | `users` table (Alembic mig), `Role`/`UserStatus`, Authlib OAuth, signed sessions, `require_role`, role-gated `/admin/users`, anon/pending/welcome index, dev/test-only login backdoor for Playwright. Prod-config validator now requires non-default `SECRET_KEY` + Google creds. 27 unit/integration + 3 e2e passing. |
| 1 | F1 — Project skeleton and verification harness | `884cd46` | FastAPI app + `/health`, SQLAlchemy + Alembic wired, pytest + Playwright harness, `make check` green. |

---

## Self-critique notes (rolling)

*(Carry forward weaknesses noted during iteration self-critiques but not yet addressed. Clear an entry when it gets fixed in a later slice. Don't let this get longer than ~10 items — if it does, something has gone off the rails.)*

- **F4 / CSRF doesn't cover multipart bodies.** `_extract_submitted_token` reads only `application/x-www-form-urlencoded`. Multipart form-posts (file uploads — not yet a feature) must send the token in the `X-CSRF-Token` header instead, or they'll fail CSRF. When the first upload route lands (likely with item photos or PO PDFs), either extend the middleware to parse multipart or document the header requirement explicitly on those routes. Captured in the docstring of `app/csrf.py` so the next dev sees it.
- **F4 / no flash-message region in the layout.** The plan called for a flash region but I dropped it because no slice is writing flashes today. Suppliers (S1) is the natural first user — add the `request.session.pop('flash', None)` render block there alongside the first redirect-after-success that needs to surface a message.
- **F4 / nav for active non-admin users renders just "Home".** Workshop / Manager / Office users currently see a primary nav with only the Home link until S1+ adds role-relevant items. Cosmetic clutter rather than a UX trap; will resolve naturally as Suppliers / Items / Movements ship.
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
