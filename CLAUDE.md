# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

UC Inventory: a FastAPI + Jinja2 + HTMX inventory tracker for a jewellery workshop. Server-rendered HTML, no SPA, no build step. SQLite in dev, Postgres in prod via SQLAlchemy 2.0 + Alembic. Google SSO via Authlib. PDFs via reportlab. APScheduler in-process for scheduled jobs.

The codebase is being built by an autonomous Claude Code loop (`loop.sh`) driven by `MISSION.md` (the immutable spec) and `PROGRESS.md` (the loop's running state). See "Loop posture" below before editing either.

## Common commands

The Makefile is the single source of truth for "how to run things". Targets:

```
make dev          # uvicorn on :8000 with reload
make test         # pytest tests/unit tests/integration  (-x, fast-fail)
make e2e          # pytest tests/e2e (Playwright)
make e2e-install  # one-time: install Playwright chromium
make lint         # ruff check app tests
make typecheck    # mypy on app/  (strict)
make format       # ruff format + ruff check --fix
make migrate      # alembic upgrade head
make migration m="..."  # alembic revision --autogenerate -m "..."
make check        # lint + typecheck + test + e2e — verification gate
```

`make check` is what `loop.sh` runs between iterations as the independent verification signal. A slice is not done unless `make check` is green.

## Git workflow

Standing authorization for any agent (loop iteration or interactive session) working in this repo:

- **Pull before working.** At the start of every session or iteration, run `git checkout main && git pull --rebase origin main` so you start from the latest remote state. Resolve any conflicts before touching anything else.
- **Work on `main`.** All changes land directly on the `main` branch. If you find yourself on a detached HEAD or another branch, switch to `main` before committing. No feature branches unless the user explicitly asks.
- **Commit and push every change.** Once a logical unit of work is complete and `make check` is green, commit it (using the `slice: <slice-name> (DoD #<n>)` format from Loop posture for loop work, or a conventional message otherwise) and `git push origin main`. Don't leave work uncommitted between iterations — the next pull must see it.
- **If `make check` fails, don't commit.** Fix it or write `BLOCKED.md` and stop. A red commit on `main` poisons the next pull.

Run a single test: `uv run pytest tests/integration/test_items_routes.py::TestItemCreate::test_creates_item -x`. Append `-k <expr>` for substring filters. The suite runs with `filterwarnings = ["error", ...]` so a stray DeprecationWarning fails the test — fix it, don't ignore it.

Run the suite against Postgres for a parity smoke test: `TEST_DATABASE_URL=postgresql+psycopg:///test_uc make test`. The `db_session` fixture in `tests/conftest.py` dispatches on URL prefix — SQLite gets a fresh per-test engine, anything else gets the SAVEPOINT-rollback pattern.

## Architecture

### Module layout (flat, not the layered tree the README's "Project layout" sketches)

`app/` is flat: one file per domain (`items.py`, `movements.py`, `purchase_orders.py`, `stock_takes.py`, `taxonomy.py`, `field_defs.py`, `checkouts.py`, `checkouts_admin.py`, `dashboard.py`, `reports.py`, `reorder.py`, `scan.py`, `audit_routes.py`, `auth.py`, `suppliers.py`, `locations.py`, `item_units.py`). Each defines its own `APIRouter` and is mounted in `app/main.py`. The README's `routes/`, `services/`, `schemas/` directories don't exist — keep new code flat in `app/<domain>.py` unless you have a specific reason to refactor.

Cross-cutting infrastructure: `app/audit.py` (audit-log writer + DB triggers), `app/cost_engine.py` (FIFO arithmetic), `app/csrf.py` (CSRF middleware), `app/csv_export.py` (CSV branch helper), `app/auth.py` (Google OAuth + role dependency), `app/template_env.py` (shared Jinja2 instance), `app/pdf.py` (reportlab PO renderer), `app/email_backend.py` (console + SMTP backends).

All ORM models live in a single `app/models.py` file, not split per domain.

### FIFO cost engine (`app/cost_engine.py`)

Every stock-mutating route delegates cost arithmetic here — manual in/out, adjustments, PO receipts, stock-take commits. Three entry points:

- `record_receipt(...)` — appends a `CostLayer`, bumps `item.current_qty`, sets `movement.total_cost`.
- `consume_fifo(...)` — walks layers `ORDER BY received_at ASC, id ASC`, writes `CostLayerConsumption` rows, decrements `qty_remaining`. Raises `InsufficientStockError` *before* writing anything if the available qty is short — route handlers map this to a 400.
- `open_value(...)` — `sum(qty_remaining * unit_cost)` across open layers; used by the dashboard.

Hard invariants: layer columns (`qty_received`, `unit_cost`, `received_at`, `source`) are immutable post-insert; only `qty_remaining` decrements. Nothing in the engine ever DELETEs. Callers must `db.add(movement); db.flush()` before calling — the engine needs `movement.id` for the FK on layers and consumptions.

### Audit log (`app/audit.py`)

Every state-changing route calls `record_audit(db, actor=..., action=..., entity_type=..., entity_id=..., before=..., after=...)`. The function flushes a row in the caller's transaction but does *not* commit — audit + change succeed/fail together. `actor=None` is reserved for system events (bootstrap admin promotion, scheduled jobs).

DB-level immutability: `apply_immutability_triggers()` installs UPDATE/DELETE-blocking triggers on `audit_log` for both SQLite and Postgres. The same SQL is applied in the migration (`0002_create_audit_log.py`) and in test fixtures so behaviour stays consistent.

`tests/integration/test_audit_coverage.py` is a **forcing-function source-text sweep**: it parametrizes over every POST/PUT/PATCH/DELETE route in the app and asserts the endpoint function's source contains `record_audit(`. A small `_EXEMPT_FROM_AUDIT_WRITE` set holds documented exceptions (e.g. `/auth/logout` is a no-op session pop). New mutating routes either call `record_audit` or join the exempt set with a one-line justification.

### CSRF middleware (`app/csrf.py`)

Double-submit cookie pattern, raw ASGI middleware (not `BaseHTTPMiddleware`). Mutating requests must carry `csrf_token` in form body OR `X-CSRF-Token` header AND the matching `csrftoken` cookie. Templates get the active token via the `csrf_context_processor` registered on the shared Jinja2 instance.

Exempt paths (small, hard-coded in `DEFAULT_EXEMPT_PATHS`): `/auth/google/callback` (provider-initiated) and `/auth/_dev-login` (dev backdoor). New exemptions require editing `csrf.py` directly — there is no per-route opt-out decorator, on purpose.

### Auth + roles (`app/auth.py`)

Four roles: `admin > manager > office > workshop`. The `require_role(*allowed)` dependency factory builds a FastAPI dependency that checks role + active status. **Admin always passes** any role gate; pending/disabled users are blocked even if their stored role would otherwise match.

`upsert_user_from_userinfo()` is split out as a pure function so user-creation logic can be unit-tested without the OAuth surface. First sign-in matching `BOOTSTRAP_ADMIN_EMAIL` (when no admin exists yet) auto-promotes to `admin` + `active`; every other first sign-in lands in `pending`.

`POST /auth/_dev-login` is a form-encoded login backdoor mounted **only** when `APP_ENV in {"dev", "test"}` (and re-checked at request time as belt-and-braces). Playwright uses this to skip Google entirely.

### Movements are append-only

`stock_movements` has no edit/delete route. Corrections are new compensating movements with reasons that name the original. The audit log assumes this; the FIFO engine assumes this. Don't add an edit route without re-reading MISSION §3 and §9.

### Soft delete via `archived_at`

Suppliers, locations, taxonomy nodes, field defs, items, and item units all use a nullable `archived_at` timestamp instead of a hard delete. Archived rows stay readable on history (movements, POs, stock takes) but are hidden from new entry. The audit log assumes their IDs persist. Don't add hard deletes.

### Taxonomy is a 2-level tree with per-leaf custom field schemas

`taxonomy_nodes` (top-level + sub-category, max two levels) and `taxonomy_field_defs` (typed schema attached to a leaf node). Items live on a *leaf*: a Category with no active sub-categories, or any sub-category. Adding a sub-category to a node with active field defs is rejected — manager must archive the defs first. Field types: `text`, `number`, `decimal`, `date`, `boolean`, `select`, `multiselect`. Item field values are stored sparse in `item_field_values` with type-specific columns (`value_text`, `value_number`, `value_decimal`, `value_date`, `value_bool`, `value_json`).

### CSV export

Every list view supports `?format=csv`. The pattern (in any list route):

```python
if (resp := csv_branch(format, filename="…", headers=…, rows=…)) is not None:
    return resp
return templates.TemplateResponse(…)
```

`csv_branch` is in `app/csv_export.py`. Cell coercion is uniform — `Decimal/int/float/bool` → `str()`, `datetime/date` → `isoformat()`, `None` → `""`, enums get pre-coerced to `.value` by the caller. CSV branch ignores HTML pagination — exports are full snapshots.

### Templates

All HTML rendering goes through `templates` from `app/template_env.py` — a single shared `Jinja2Templates` instance with `csrf_context_processor` and `flash_context_processor` registered. Don't construct a second `Jinja2Templates` in a router; you'll silently drop the context processors and forms will start 403'ing.

Flash messages: a route sets `request.session["flash"] = "…"` after a successful POST; the next render pops and displays it once.

## Loop posture

This repo is built by an autonomous loop (`./loop.sh`). The loop reads MISSION.md + PROGRESS.md, picks the smallest end-to-end slice that moves a Definition-of-Done item closer, implements + tests + self-critiques + commits, then loops.

Implications for any iteration (Claude or human):

- **Do not edit `MISSION.md`.** It is the immutable spec; only the user edits it. If scope genuinely needs to change, log it under "Proposed scope changes" in `PROGRESS.md` and continue with the original scope.
- **`PROGRESS.md` is the loop's running state.** Read it at the start of every iteration. Update "Current state", "Completed slices (log)", "Definition of Done tracker" at the end. Don't edit past Completed-slices entries.
- **Commit message format:** `slice: <slice-name> (DoD #<n>)` — names the slice and references the Definition-of-Done item it advances.
- **`CHANGELOG.md`** is a one-line-per-slice "what shipped" log, newest at top.
- **`BLOCKED.md`** is the halt signal. If the same problem fails three iterations running with no measurable progress, or you can't make progress this iteration, write `BLOCKED.md` and stop — don't produce a no-op commit.
- **Migrations are forever.** Once an Alembic migration is committed and applied, don't edit it. Add a new one to fix mistakes.
- **No tech-stack swaps.** Stack is fixed by MISSION §5. If you have a strong reason to change it, write to `BLOCKED.md` and stop.

## Configuration

`pydantic-settings` in `app/config.py` loads from `.env`. Key vars: `DATABASE_URL`, `SECRET_KEY`, `APP_BASE_URL`, `APP_ENV` (`dev`/`test`/`prod`), `GOOGLE_CLIENT_ID/SECRET`, `EMAIL_BACKEND` (`console`/`smtp`), `BOOTSTRAP_ADMIN_EMAIL`. Prod refuses to start without a non-default `SECRET_KEY` and Google credentials (validator in `Settings._validate_prod_secrets`).

Test bootstrap (`tests/conftest.py`) forces `APP_ENV=test`, `SECRET_KEY=test-secret-key-fixed-for-tests`, `DATABASE_URL=sqlite:///:memory:` before any app imports.
