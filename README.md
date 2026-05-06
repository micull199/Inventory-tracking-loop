# UC Inventory

Inventory tracking for UC's workshop and office. Tracks raw materials, consumables, tools, and wax injection moulds across a Manager-defined taxonomy with custom fields per category. Supports QR/barcode scanning, periodic stock takes, FIFO cost tracking with variable per-receipt pricing, low-stock alerts, and PO generation with email-to-supplier.

> **Status: under active build.** Sections marked _TODO_ get filled in as the corresponding feature ships. If you find a TODO that should have been done, the build loop missed a step. Open an issue.

---

## Quick links

- [Mission and scope](./MISSION.md) — single source of truth for what this app does and does not do.
- [Build progress](./PROGRESS.md) — what's been built, what's next, what's stuck.
- _TODO: changelog_
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
- **PDF:** _TODO (WeasyPrint or reportlab, decided during PO slice)_
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

_TODO_

### Defining a new category and its custom fields

_TODO_

### Creating an item

_TODO_

### Printing a QR label and scanning it

_TODO_

### Running a stock take

_TODO_

### Generating and sending a purchase order

_TODO_

### Receiving stock against a PO

_TODO_

### Reading the audit trail for an item

_TODO_

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
