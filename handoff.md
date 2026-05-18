# UC Inventory — Architectural Handoff

Single-document snapshot of the current state of `/Users/michaelcullen/Desktop/First Inventory app (Loop)` as of 2026-05-15, on branch `main` (commit `237e66f`). Per the brief: descriptive only, no recommendations.

---

## 1. Stack and architecture

### 1.1 Languages, frameworks, libraries

**Backend (Python ≥ 3.11):**

| Concern | Package | Pin |
|---|---|---|
| Web framework | `fastapi` | ≥ 0.115 |
| ASGI server | `uvicorn[standard]` | ≥ 0.32 |
| Form parsing | `python-multipart` | ≥ 0.0.20 |
| Templating | `jinja2` | ≥ 3.1 |
| ORM / migrations | `sqlalchemy` | ≥ 2.0 |
| Migrations | `alembic` | ≥ 1.14 |
| Postgres driver | `psycopg[binary]` | ≥ 3.2 |
| OAuth | `authlib` | ≥ 1.3 |
| Session signing | `itsdangerous` | ≥ 2.2 |
| Settings | `pydantic` ≥ 2.10, `pydantic-settings` ≥ 2.7 |
| In-process scheduler | `apscheduler` | ≥ 3.11 (**dependency present but no scheduler is registered; see §5**) |
| QR codes | `qrcode[pil]` | ≥ 8.0 |
| Email | stdlib `smtplib` + `premailer` ≥ 3.10 |
| PDF | `reportlab` | ≥ 4.5 |
| HTTP client (test stub only) | `httpx` | ≥ 0.28 |
| Apple `.numbers` rejection | `numbers-parser` | ≥ 4.18 |

**Dev/test:** `pytest` ≥ 8.3, `pytest-asyncio`, `playwright` ≥ 1.49, `pytest-playwright`, `ruff`, `mypy` (strict), `freezegun`.

**Frontend:** server-rendered Jinja2 + HTMX. No SPA, no Node/npm, no build step. Some inline `<script>` blocks for HTMX interactions; otherwise no client-side framework.

**Mobile:** No native mobile. Responsive HTML targeted at desktop + 10" tablet (per MISSION §4 NFR).

### 1.2 Database

- **Engine:** SQLite in dev (`dev.db` at repo root), Postgres in prod via `psycopg[binary]`.
- **Version pins:** SQLite ≥ 3.35 required (cost engine and SKU allocator both use `UPDATE ... RETURNING`). Postgres version not pinned in deps; expectation is whatever Fly.io provides.
- **Hosting:** Fly.io for app + Fly Postgres for DB (Dockerfile + `fly.toml` + `scripts/fly-entrypoint.sh` present; per CHANGELOG, P4 slice shipped this).
- **Test config:** `tests/conftest.py` forces `DATABASE_URL=sqlite:///:memory:`; can be overridden with `TEST_DATABASE_URL=postgresql+psycopg:///test_uc` for Postgres parity smoke test.

### 1.3 ORM / query layer

- **SQLAlchemy 2.0** with the new `Mapped[T]` / `mapped_column` typed-declarative API.
- All ORM models live in **a single flat file**: `app/models.py`. No per-domain model split.
- Migrations under `migrations/versions/` (Alembic), 24 in total (`0001`..`0024`).
- Sessions: `app/db.py` exposes `get_session()` (FastAPI dependency). The integration-test fixture (`tests/conftest.py`) dispatches on URL prefix — SQLite gets a fresh per-test engine, Postgres gets a SAVEPOINT rollback pattern.

### 1.4 App structure

**Monolith, flat layout.** `app/` is one file per domain — no `routes/`, `services/`, `schemas/` split. Per CLAUDE.md the README's "Project layout" is aspirational and should be ignored.

```
app/
├── main.py                  # app factory, router mounts, CSRF middleware, exception handlers
├── config.py                # pydantic-settings; loads .env
├── db.py                    # SQLAlchemy session factory + get_session() FastAPI dep
├── models.py                # ALL ORM models (single file)
├── auth.py                  # Google OAuth + role dependencies + dev-login backdoor
├── oauth_test_stub.py       # mounted only when APP_ENV=test AND OAUTH_STUB_MODE=1
├── csrf.py                  # raw-ASGI CSRF middleware (double-submit cookie)
├── audit.py                 # record_audit + DB-trigger immutability installer
├── audit_routes.py          # /admin/audit (list + CSV)
├── cost_engine.py           # record_receipt, consume_fifo, open_value (FIFO)
├── csv_export.py            # csv_branch helper
├── csv_import.py            # shared upload machinery (size cap, .numbers rejection, RowResult)
├── email_backend.py         # ConsoleEmailBackend + SmtpEmailBackend
├── pdf.py                   # PO PDF render via reportlab
├── template_env.py          # shared Jinja2Templates (with csrf + flash context processors)
├── field_catalog.py         # frozen FIELD_CATALOG tuple
├── field_storage.py         # read/format helpers for catalog values
├── field_visibility.py      # legacy shim returning constant visibility map
├── sku.py                   # ancestor_chain, node_depth, effective_archetype, compose_sku, allocate_sequence, create_unique_variant_leaf
└── <domain routers>:
    items.py, item_units.py, movements.py, transfers.py,
    taxonomy.py (+ upload_router), field_defs.py,
    suppliers.py (+ upload_router), locations.py (+ upload_router),
    purchase_orders.py (draft_router + list_router), stock_takes.py,
    checkouts.py, checkouts_admin.py,
    dashboard.py, reports.py, reorder.py, scan.py
```

Routers each define their own `APIRouter` and are mounted in `app/main.py`. Order matters in two places: upload routers (`/admin/taxonomy/upload`, etc.) must mount BEFORE the main router so literal paths beat `/{id}` dynamic captures.

### 1.5 Auth and user model

- **Identity:** Google SSO only (Authlib OIDC against `https://accounts.google.com/.well-known/openid-configuration`).
- **Bootstrap:** First Google sign-in that matches `BOOTSTRAP_ADMIN_EMAIL` (env var) auto-promotes to `admin` + `active` *if no admin exists yet*. Every other first sign-in lands as `pending`.
- **Roles:** `admin > manager > office > workshop` — **admin always passes any role gate**; pending/disabled users are blocked even if their stored role would match.
- **Dependency factory:** `require_role(*allowed)` in `app/auth.py` returns a FastAPI dependency. Used as `Depends(require_role(Role.MANAGER))` on every protected route.
- **Sessions:** signed via `itsdangerous` (cookie-based).
- **Dev backdoors:** `POST /auth/_dev-login` (when `APP_ENV in {dev, test}`); `oauth_test_stub.py` provides a fake OIDC provider gated on `APP_ENV=test AND OAUTH_STUB_MODE=1`.
- **CSRF:** double-submit cookie. Exempt paths hardcoded in `app/csrf.py:DEFAULT_EXEMPT_PATHS`: `/auth/google/callback`, `/auth/_dev-login`.

### 1.6 Where business logic lives

- **Backend.** All validation, audit-writing, FIFO arithmetic, role gating, and CSV branching is server-side.
- **DB.** Two DB-level enforcements: (a) audit-log immutability triggers (UPDATE/DELETE blocked on `audit_log`, installed for both SQLite + Postgres by `apply_immutability_triggers()`); (b) partial unique indexes on `taxonomy_nodes.name`/`sku_prefix` and `taxonomy_stages.is_initial`.
- **Frontend.** Display-only. A handful of HTMX endpoints return template fragments (e.g. items category dropdown options at `/admin/items/_category-search`, post-category field fragment at `/admin/items/_custom-fields`).

---

## 2. Current schema — full dump

24 Alembic migrations (`migrations/versions/0001`..`0024`). The current live tables (verified via `sqlite3 dev.db .tables`):

```
alembic_version       cost_layer_consumptions   stock_movements
audit_log             cost_layers               stock_take_lines
checkouts             item_units                stock_takes
items                 locations                 suppliers
taxonomy_field_defs   purchase_order_lines      taxonomy_nodes
taxonomy_stages       purchase_orders           transfer_order_lines
transfer_orders       users
```

19 application tables + `alembic_version`. Note: `item_field_values` was dropped in migration 0024.

### 2.1 Enums (StrEnum, stored as lowercase string, never native DB enum)

All defined in `app/models.py`. All registered on `SAEnum(..., native_enum=False, length=N, values_callable=lambda enum_cls: [e.value for e in enum_cls])`.

| Enum | Members | Storage width |
|---|---|---|
| `Role` | `admin`, `manager`, `office`, `workshop` | String(16) |
| `UserStatus` | `pending`, `active`, `disabled` | String(16) |
| `Archetype` | `unique`, `bulk`, `unique_variant` | String(16) — only set on depth-0 `TaxonomyNode` rows; NULL elsewhere, inherited at read time |
| `FieldType` | `text`, `number`, `decimal`, `date`, `boolean`, `select`, `multiselect` | String(16) — used by the **field catalog**; no longer stored on `taxonomy_field_defs` post-0024 |
| `TrackingMode` | `qty`, `unique` | String(16) |
| `ItemUnitStatus` | `available`, `lost` | String(16) — note: no `checked_out` or `damaged` member; checked-out state is derived from open `checkouts` rows |
| `MovementType` | `in`, `out`, `adjustment`, `transfer`, `stage_change` | String(16) |
| `CostLayerSource` | `po_receipt`, `manual_in`, `positive_adjustment` | String(20) |
| `POStatus` | `draft`, `sent`, `in_transit`, `partially_received`, `received`, `cancelled` | String(20) |
| `TransferOrderStatus` | `draft`, `shipped`, `received`, `cancelled` | String(16) |

### 2.2 Tables, in order

**Conventions used throughout:**
- All `created_at`/`updated_at` are `DateTime(timezone=True)` with `server_default=func.now()`; `updated_at` adds `onupdate=func.now()`.
- All FK `ondelete` policies are `RESTRICT` unless noted. Two `SET NULL` exceptions: `purchase_orders.created_by`, `stock_takes.created_by`, `transfer_orders.{created_by,shipped_by,received_by}`, `audit_log.actor_id`, `stock_movements.user_id` → `users.id`.
- Soft delete is `archived_at: DateTime(tz) | None`. Hard deletes happen on: `taxonomy_field_defs` (post-0024), `cost_layer_consumptions` (never), audit rows (DB triggers block).

#### `users` (migration 0001)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `google_sub` | String(64) | No | — | unique |
| `email` | String(255) | No | — | unique |
| `name` | String(255) | No | — | |
| `role` | Enum(Role) | **Yes** | NULL | NULL = pending user awaiting admin assignment |
| `status` | Enum(UserStatus) | No | `pending` | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Indexes: `uq_users_google_sub`, `uq_users_email`. No `archived_at`. No relationships hung off this row.

#### `suppliers` (migration 0003)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `name` | String(255) | No | — | **unique across active + archived** |
| `email` | String(255) | Yes | NULL | |
| `phone` | String(64) | Yes | NULL | |
| `notes` | String(2000) | Yes | NULL | |
| `archived_at` | DateTime(tz) | Yes | NULL | soft delete |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Referenced by: `items.supplier_id`, `purchase_orders.supplier_id`.

#### `locations` (migration 0004)

Same shape as `suppliers`: `id`, `name` (unique across archive), `notes`, `archived_at`, timestamps. Referenced by `items.location_id`, `item_units.location_id`, `stock_takes.scope_location_id`, `transfer_orders.source_location_id`/`destination_location_id`.

#### `taxonomy_nodes` (migration 0005, extended by 0015, 0016, 0017 [dropped in 0023])

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `parent_id` | Integer FK self | Yes | NULL | RESTRICT; NULL = depth 0 |
| `name` | String(255) | No | — | sibling-scoped unique (see indexes) |
| `archetype` | Enum(Archetype) | Yes | NULL | **only depth 0**; depth 1/2 leave NULL and inherit via `effective_archetype()` walk |
| `sku_prefix` | String(8) | No | derived from `name` | 1-8 uppercase alnum (`@validates` in `models.py:326`); sibling-unique |
| `next_sequence` | Integer | No | 1 (`server_default=text("1")`) | atomic allocator counter |
| `sort_order` | Integer | No | 0 | auto-stepped by 10 |
| `defaults_json` | JSON | Yes | NULL | dict of items-form defaults (see §3) |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

**Indexes:**
- `ix_taxonomy_nodes_parent_id`, `ix_taxonomy_nodes_archived_at`
- `uq_taxonomy_top_name` partial unique on `name` where `parent_id IS NULL`
- `uq_taxonomy_child_name` partial unique on `(parent_id, name)` where `parent_id IS NOT NULL`
- `uq_taxonomy_sku_prefix_top` partial unique on `sku_prefix` where `parent_id IS NULL`
- `uq_taxonomy_sku_prefix_child` partial unique on `(parent_id, sku_prefix)` where `parent_id IS NOT NULL`

All four partial unique indexes scope across active + archived rows.

#### `taxonomy_field_defs` (migration 0006, **schema heavily reshaped by 0021/0022/0024**)

Post-0024 shape — a slim visibility selector (the original typed-schema columns `name`, `type`, `options_json`, `catalog_key`, `archived_at` were dropped):

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `node_id` | Integer FK → `taxonomy_nodes.id` | No | — | RESTRICT |
| `key` | String(64) | No | — | must match an entry in `app.field_catalog.CATALOG_BY_KEY` |
| `required` | Boolean | No | False (`server_default=text("0")`) | |
| `sort_order` | Integer | No | 0 | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Indexes: `ix_taxonomy_field_defs_node_id`, `uq_taxonomy_field_defs_node_key` unique on `(node_id, key)`.

No `archived_at`; picks are hard-deleted. The legacy archive/unarchive routes still exist for RBAC test stability but 400 with a "no longer supported" message.

#### `taxonomy_stages` (migration 0018)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `top_level_node_id` | Integer FK → `taxonomy_nodes.id` | No | — | RESTRICT; **must be depth 0** (enforced in route, not DB) |
| `name` | String(64) | No | — | unique with `top_level_node_id` |
| `sort_order` | Integer | No | 0 (`server_default=text("0")`) | |
| `is_initial` | Boolean | No | False | partial unique: at most one active initial per top-level |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Indexes: `ix_taxonomy_stages_top_level_node_id`, `ix_taxonomy_stages_archived_at`, `uq_taxonomy_stage_name` (unique on `top_level_node_id, name`), `uq_taxonomy_stage_initial_active` partial unique on `top_level_node_id` where `is_initial = 1 AND archived_at IS NULL` (SQLite) / `is_initial AND archived_at IS NULL` (PG).

#### `items` (migration 0007, extended by 0016 + 0018 + 0024)

Verified live SQLite schema (note column ordering reflects migration history):

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `sku` | String(64) | No | — | unique across active + archived |
| `name` | String(255) | No | — | |
| `taxonomy_node_id` | Integer FK → `taxonomy_nodes.id` | No | — | RESTRICT; **must be a leaf** (enforced in route) |
| `unit` | String(32) | No | — | free-text unit-of-measure label |
| `tracking_mode` | Enum(TrackingMode) | No | `qty` | derived from archetype at create time |
| `requires_checkout` | Boolean | No | False | |
| `current_qty` | Numeric(14,4) | No | 0 | **denormalised** from FIFO layers; updated by cost engine only |
| `reorder_threshold` | Numeric(14,4) | No | 0 | |
| `reorder_qty` | Numeric(14,4) | No | 0 | |
| `supplier_id` | Integer FK → `suppliers.id` | Yes | NULL | RESTRICT |
| `location_id` | Integer FK → `locations.id` | Yes | NULL | RESTRICT |
| `qr_code` | String(128) | Yes | NULL | partial unique where set |
| `notes` | String(2000) | Yes | NULL | |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |
| `assigned_sequence` | Integer | Yes | NULL | added 0016; numeric segment of SKU; denormalised cache |
| `current_stage_id` | Integer FK → `taxonomy_stages.id` | Yes | NULL | added 0018; RESTRICT |
| `ring_size` | String(64) | Yes | NULL | added 0024 |
| `weight_grams` | Numeric(14,4) | Yes | NULL | added 0024 |
| `stone_shape` | String(64) | Yes | NULL | added 0024 |

Indexes: `uq_items_sku`, `uq_items_qr_code` (partial where `qr_code IS NOT NULL`), `ix_items_taxonomy_node_id`, `ix_items_supplier_id`, `ix_items_location_id`, `ix_items_current_stage_id`, `ix_items_archived_at`.

**Per-leaf field picks decide which of these columns are surfaced on the items form / list / CSV.** The columns themselves are all always present.

#### `item_units` (migration 0009)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `item_id` | Integer FK → `items.id` | No | — | RESTRICT; **item must have `tracking_mode=unique`** (enforced in route) |
| `serial_or_label` | String(128) | No | — | unique within item (across archived) |
| `status` | Enum(ItemUnitStatus) | No | `available` | |
| `location_id` | Integer FK → `locations.id` | Yes | NULL | RESTRICT; units of same item can have different locations |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

#### `stock_movements` (migration 0010, FKs filled in by 0012, 0014, 0018, 0019)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | append-only |
| `item_id` | Integer FK → `items.id` | No | — | |
| `item_unit_id` | Integer FK → `item_units.id` | Yes | NULL | populated only for unique-tracked items |
| `type` | Enum(MovementType) | No | — | |
| `qty` | Numeric(14,4) | No | — | signed; `stage_change` movements carry `qty=0` |
| `user_id` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `reason` | String(255) | Yes | NULL | |
| `note` | String(2000) | Yes | NULL | |
| `po_id` | Integer FK → `purchase_orders.id` | Yes | NULL | RESTRICT (FK activated by 0012) |
| `stock_take_id` | Integer FK → `stock_takes.id` | Yes | NULL | RESTRICT (FK activated by 0014) |
| `transfer_order_id` | Integer FK → `transfer_orders.id` | Yes | NULL | added 0019; populated only by TR1 ship/receive (NULL for legacy instant-flip transfers) |
| `from_stage_id` | Integer FK → `taxonomy_stages.id` | Yes | NULL | added 0018; only populated on `stage_change` movements |
| `to_stage_id` | Integer FK → `taxonomy_stages.id` | Yes | NULL | added 0018 |
| `total_cost` | Numeric(14,4) | Yes | NULL | set by cost engine on `out`/`adjustment`; NULL on `in`/`transfer`/`stage_change` |
| `created_at` | DateTime(tz) | No | now() | **no `updated_at`** — append-only |

Indexes: per FK column + `ix_stock_movements_type`, `ix_stock_movements_created_at`.

**No edit / delete routes exist for movements.** Corrections are new compensating movements.

#### `cost_layers` (migration 0010)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `item_id` | Integer FK → `items.id` | No | — | |
| `qty_received` | Numeric(14,4) | No | — | **immutable post-insert** |
| `qty_remaining` | Numeric(14,4) | No | — | the only column the engine ever updates |
| `unit_cost` | Numeric(14,4) | No | — | immutable |
| `received_at` | DateTime(tz) | No | — | immutable; FIFO sort key |
| `source` | Enum(CostLayerSource) | No | — | |
| `source_movement_id` | Integer FK → `stock_movements.id` | No | — | RESTRICT |
| `created_at` | DateTime(tz) | No | now() | |

Indexes: per FK + composite `ix_cost_layers_item_received` on `(item_id, received_at, id)` for FIFO `ORDER BY`. No DELETE path.

#### `cost_layer_consumptions` (migration 0010)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `layer_id` | Integer FK → `cost_layers.id` | No | — | |
| `movement_id` | Integer FK → `stock_movements.id` | No | — | |
| `qty_consumed` | Numeric(14,4) | No | — | |
| `unit_cost_at_consumption` | Numeric(14,4) | No | — | snapshot of layer's `unit_cost` at tap time (always equals immutable layer cost); stored so reports are self-contained |
| `created_at` | DateTime(tz) | No | now() | |

Indexes: per FK + composite `(movement_id, layer_id)` for the item-detail consumption breakdown.

#### `purchase_orders` (migration 0011, `shipped_at` added in 0020)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `supplier_id` | Integer FK → `suppliers.id` | No | — | RESTRICT |
| `status` | Enum(POStatus) (stored as String(20)) | No | `draft` | |
| `expected_date` | Date | Yes | NULL | |
| `sent_at` | DateTime(tz) | Yes | NULL | set when manager sends PO |
| `shipped_at` | DateTime(tz) | Yes | NULL | added 0020 (POIT1 slice) — supplier dispatch marker |
| `notes` | String(2000) | Yes | NULL | |
| `created_by` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Lifecycle: `draft → sent → (optionally) in_transit → partially_received → received`. Both `draft` and `sent` can go to `cancelled`.

#### `purchase_order_lines` (migration 0011)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `po_id` | Integer FK → `purchase_orders.id` | No | — | **CASCADE** (lines are part of the PO doc) |
| `item_id` | Integer FK → `items.id` | No | — | RESTRICT |
| `qty_ordered` | Numeric(14,4) | No | — | |
| `qty_received` | Numeric(14,4) | No | 0 | incremented on receipt |
| `expected_unit_cost` | Numeric(14,4) | Yes | NULL | planning estimate; actual unit cost lives on the cost layer |

No soft-delete column. Indexes per FK.

#### `checkouts` (migration 0013)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `item_id` | Integer FK → `items.id` | No | — | |
| `item_unit_id` | Integer FK → `item_units.id` | Yes | NULL | required for unique-tracked, NULL for qty-tracked (enforced in route) |
| `user_id` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `checked_out_at` | DateTime(tz) | No | — | |
| `expected_return` | DateTime(tz) | Yes | NULL | |
| `returned_at` | DateTime(tz) | Yes | NULL | **NULL = open, set = returned** — no status enum |
| `condition_note` | String(2000) | Yes | NULL | |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Derived states: **Open** = `returned_at IS NULL`; **Overdue** = `returned_at IS NULL AND expected_return < now()`. Constraint enforced in routes: at most one open checkout per item / item_unit at a time.

#### `stock_takes` (migration 0014)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `scope_node_id` | Integer FK → `taxonomy_nodes.id` | Yes | NULL | RESTRICT; XOR with `scope_location_id` |
| `scope_location_id` | Integer FK → `locations.id` | Yes | NULL | RESTRICT; XOR with `scope_node_id` |
| `scheduled_for` | Date | No | — | |
| `started_at` | DateTime(tz) | Yes | NULL | |
| `completed_at` | DateTime(tz) | Yes | NULL | |
| `notes` | String(2000) | Yes | NULL | |
| `created_by` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Derived state (no enum): scheduled / in-progress / completed based on `started_at` and `completed_at`. XOR enforced in route — at most one of the two scope columns set; both NULL = all items.

#### `stock_take_lines` (migration 0014)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `stock_take_id` | Integer FK → `stock_takes.id` | No | — | **CASCADE** |
| `item_id` | Integer FK → `items.id` | No | — | RESTRICT |
| `system_qty` | Numeric(14,4) | No | — | snapshot of `item.current_qty` at start |
| `counted_qty` | Numeric(14,4) | Yes | NULL | operator-entered |
| `variance` | Numeric(14,4) | Yes | NULL | computed at commit (counted − system) |
| `committed` | Boolean | No | False | flips True once adjustment movement is written |
| `notes` | String(2000) | Yes | NULL | |

#### `transfer_orders` (migration 0019)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `source_location_id` | Integer FK → `locations.id` | No | — | RESTRICT |
| `destination_location_id` | Integer FK → `locations.id` | No | — | RESTRICT |
| `status` | Enum(TransferOrderStatus) | No | `draft` | |
| `shipped_at` | DateTime(tz) | Yes | NULL | |
| `received_at` | DateTime(tz) | Yes | NULL | |
| `expected_arrival` | Date | Yes | NULL | |
| `carrier` | String(128) | Yes | NULL | |
| `tracking_number` | String(128) | Yes | NULL | |
| `notes` | String(2000) | Yes | NULL | |
| `created_by` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `shipped_by` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `received_by` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `created_at`/`updated_at` | DateTime(tz) | No | now() | |

Cost engine is **not** invoked by transfers — they don't change cost basis, only location. Two paired `transfer` movements are written by the ship+receive flow.

#### `transfer_order_lines` (migration 0019)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `transfer_order_id` | Integer FK → `transfer_orders.id` | No | — | CASCADE |
| `item_id` | Integer FK → `items.id` | No | — | RESTRICT; unique with `transfer_order_id` |
| `qty` | Numeric(14,4) | No | — | informational in v1 |
| `ship_movement_id` | Integer FK → `stock_movements.id` | Yes | NULL | populated on transition to shipped |
| `receive_movement_id` | Integer FK → `stock_movements.id` | Yes | NULL | populated on transition to received |

Composite unique: `uq_transfer_order_line_item` on `(transfer_order_id, item_id)`.

#### `audit_log` (migration 0002, DB triggers installed for both SQLite and Postgres)

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | append-only |
| `actor_id` | Integer FK → `users.id` | Yes | NULL | SET NULL; NULL = system event (bootstrap, scheduled job, CSV system-event during migrations) |
| `action` | String(64) | No | — | e.g. `item.created`, `taxonomy_node.archived`, `stock_movement.in`, `supplier.csv_uploaded` |
| `entity_type` | String(32) | No | — | e.g. `item`, `stock_movement`, `taxonomy_node`, `taxonomy_field_def`, `supplier`, `location`, `user`, `purchase_order`, `stock_take`, `transfer_order`, `checkout` |
| `entity_id` | Integer | Yes | NULL | NULL for bulk events |
| `before_json` | JSON | Yes | NULL | NULL on create |
| `after_json` | JSON | Yes | NULL | NULL on delete |
| `created_at` | DateTime(tz) | No | now() | **no `updated_at`** |

Indexes: `(entity_type, entity_id)` composite, `actor_id`, `created_at`.

**DB-level immutability:** `apply_immutability_triggers()` installs SQL triggers on both engines that block UPDATE and DELETE on `audit_log`. Same SQL applied by migration 0002 and by test fixtures so behaviour stays consistent.

**Coverage discipline:** `tests/integration/test_audit_coverage.py` parametrizes over every POST/PUT/PATCH/DELETE route and asserts the handler's source text contains `record_audit(`. A small `_EXEMPT_FROM_AUDIT_WRITE` set holds documented exceptions (e.g. `/auth/logout`, `POST /admin/taxonomy/fields/{id}/unarchive` which 400s without writing).

### 2.3 Migration timeline

| # | File | Effect |
|---|---|---|
| 0001 | `create_users.py` | `users` table. |
| 0002 | `create_audit_log.py` | `audit_log` + DB triggers (UPDATE/DELETE-blocking) for both SQLite and Postgres. |
| 0003 | `create_suppliers.py` | `suppliers`. |
| 0004 | `create_locations.py` | `locations`. |
| 0005 | `create_taxonomy_nodes.py` | `taxonomy_nodes` + two partial unique indexes on `name`. |
| 0006 | `create_taxonomy_field_defs.py` | `taxonomy_field_defs` with original typed-schema columns (`name`, `type`, `options_json`, `archived_at`). |
| 0007 | `create_items.py` | `items` table. |
| 0008 | `create_item_field_values.py` | `item_field_values` sparse table (text/number/decimal/date/bool/json columns). **Dropped in 0024.** |
| 0009 | `create_item_units.py` | `item_units`. |
| 0010 | `create_cost_layers_and_movements.py` | `stock_movements`, `cost_layers`, `cost_layer_consumptions`. `stock_movements.po_id` and `stock_take_id` left without FK constraints (deferred). |
| 0011 | `create_purchase_orders.py` | `purchase_orders` + `purchase_order_lines`. |
| 0012 | `add_stock_movements_po_id_fk.py` | Activate the FK on `stock_movements.po_id` (deferred from 0010). |
| 0013 | `create_checkouts.py` | `checkouts` table (schema only — routes added in C-series later). |
| 0014 | `create_stock_takes.py` | `stock_takes` + `stock_take_lines` + activate FK on `stock_movements.stock_take_id`. |
| 0015 | `taxonomy_defaults_json.py` | Add `taxonomy_nodes.defaults_json` (JSON). |
| 0016 | `taxonomy_archetype_and_prefix.py` | Add `archetype`, `sku_prefix`, `next_sequence` to `taxonomy_nodes`; add `assigned_sequence` to `items`; backfill prefixes from names with sibling disambiguation; install four partial unique indexes on `sku_prefix`. |
| 0017 | `taxonomy_field_visibility.py` | Add `taxonomy_nodes.field_visibility_json` (JSON). **Dropped in 0023.** |
| 0018 | `lifecycle_stages.py` | `taxonomy_stages` table + `items.current_stage_id` + `stock_movements.from_stage_id` and `to_stage_id`. |
| 0019 | `transfer_orders.py` | `transfer_orders` + `transfer_order_lines` + `stock_movements.transfer_order_id`. |
| 0020 | `po_in_transit.py` | Add `purchase_orders.shipped_at`; the new `POStatus.IN_TRANSIT` enum value drops in cleanly because the column is `String(20)`. |
| 0021 | `taxonomy_field_def_catalog_key.py` | Add `taxonomy_field_defs.catalog_key` (bridge column, later removed). |
| 0022 | `backfill_catalog_key.py` | Backfill `catalog_key` from `(key, name)` pairs; archive unmatched rows; write system audit events with `actor_id = NULL`. Idempotent. |
| 0023 | `drop_field_visibility.py` | Drop `taxonomy_nodes.field_visibility_json` (became dead after catalog refactor). |
| 0024 | `promote_standard_fields.py` | Add `items.ring_size`, `items.weight_grams`, `items.stone_shape`. Wipe + drop `item_field_values` entirely. Wipe `taxonomy_field_defs` rows and drop columns `name`, `catalog_key`, `type`, `options_json`, `archived_at` — leaves the slim `(node_id, key, required, sort_order)` shape. Completes the shift from sparse custom-field storage to column-backed standard fields. |

---

## 3. Current entities and their purpose

### 3.1 `User`
**Represents:** a Google Workspace member who has signed in.
**Links to:** `audit_log.actor_id`, `stock_movements.user_id`, `checkouts.user_id`, `purchase_orders.created_by`, `stock_takes.created_by`, `transfer_orders.created_by/shipped_by/received_by`.
**Lifecycle:** first Google sign-in → `status=pending`, `role=NULL` (or auto-promoted to `admin`+`active` if matches `BOOTSTRAP_ADMIN_EMAIL` and no admin yet) → admin assigns a role → `active` → optionally `disabled`. No hard delete.
**Business rules:** admin cannot demote/disable themselves; `active` requires a non-NULL role; `disabled` and `pending` users are 403'd by `require_role(...)`.

### 3.2 `Supplier` / `Location`
**Represents:** a supplier UC buys from / a physical place stock lives.
**Lifecycle:** create → optional edit → archive → (optional) unarchive. Names are unique across active + archived rows by design.
**Business rules:** archived rows are hidden from new entry but still readable on history. Items referencing a now-archived supplier/location keep the FK.

### 3.3 `TaxonomyNode` (the inventory hierarchy)
**Represents:** one node in a 1-3-level tree of categories. Depth 0 = top-level category; depth 1 = sub-category; depth 2 = either a manager-created sub-sub-cat (for `bulk`/`unique`) or a system-minted auto-leaf (for `unique_variant`).
**Links to:** itself (`parent_id`), `taxonomy_field_defs.node_id`, `taxonomy_stages.top_level_node_id`, `items.taxonomy_node_id`, `stock_takes.scope_node_id`.
**Lifecycle:** create → edit (name/sort_order/defaults_json/archetype/sku_prefix) → archive. **Archetype + sku_prefix lock once descendant items exist** (`_has_descendant_items` in `app/taxonomy.py:405`), to guard against silent SKU drift.
**Business rules:** depth limit ≤ 2; container-or-leaf (a node with active items cannot host children, and the items form rejects a destination with active children); archetype set at depth 0 only and inherited downward; `unique_variant` roots: depth-2 nodes are system-managed via `app/sku.py:create_unique_variant_leaf` and the manager-facing depth-2 create form is disabled; `next_sequence` only ever increments via `allocate_sequence` (atomic `UPDATE ... RETURNING`).

### 3.4 `TaxonomyFieldDef` (catalog visibility pick)
**Represents:** a manager's decision to show one `field_catalog` entry on items in a given category.
**Lifecycle:** pick → (no edit) → remove (hard delete). No archive state.
**Business rules:** key must match an entry in `app/field_catalog.py:CATALOG_BY_KEY`; same-tree (ancestor or descendant) duplicate picks are rejected; sibling-level overlap is allowed; a node with active picks cannot grow children (must remove picks first).

### 3.5 `TaxonomyStage`
**Represents:** one ordered lifecycle stage owned by a top-level category (e.g. RINGS: `raw → polishing → QC → ready-to-ship → shipped`; RAW MATERIALS: `on-hand → issued`).
**Lifecycle:** create → edit (name/sort/is_initial) → archive → (optional) unarchive.
**Business rules:** only attaches to depth-0 nodes; at most one initial-active stage per top-level (partial unique index + route-side `_clear_other_initial` helper that flips off the previous initial when the user reassigns).

### 3.6 `Item` (the core inventory entity)
**Represents:** a thing UC tracks — a raw material, consumable, tool, mould, ring, or any other category. Belongs to exactly one *leaf* `TaxonomyNode`.
**Links to:** `taxonomy_nodes` (category), `suppliers` (default supplier), `locations` (location), `taxonomy_stages.current_stage_id`, `item_units` (unique-tracked), `stock_movements`, `cost_layers`, `checkouts`, `purchase_order_lines`, `stock_take_lines`, `transfer_order_lines`.
**Lifecycle:** create → optional edits to non-structural fields → archive → optional unarchive. SKU is immutable post-create.
**Business rules:**
- SKU is system-allocated via `app/sku.py:compose_sku` + `allocate_sequence`; user input ignored on create.
- `tracking_mode` is derived from the leaf's effective archetype (`bulk → qty`, `unique`/`unique_variant → unique`).
- `unique` items: `current_qty` is always 1; reorder logic is disabled.
- `unique_variant` items: a depth-2 auto-leaf is minted at create time via `create_unique_variant_leaf`; the depth-1 sub-cat owns the sequence allocator.
- `requires_checkout=True` items must have at most one open `checkouts` row at a time.
- `current_qty`, `current_stage_id`, `assigned_sequence` are denormalised — see §5.
- All eight pre-0024 columns + the three promoted standard fields (`ring_size`, `weight_grams`, `stone_shape`) are always present on the row; the per-leaf field-def picks decide which are surfaced on the items form/list/CSV.

### 3.7 `ItemUnit`
**Represents:** one physical unit of a unique-tracked item (mould #3 specifically; tool with serial XYZ). Only items with `tracking_mode=unique` may have units.
**Lifecycle:** create → optional edit (serial/status/location) → archive → optional unarchive. **Status enum members are only `available` and `lost`**; checked-out state is derived from open `checkouts` row pointing at the unit.
**Business rules:** serial unique within item.

### 3.8 `StockMovement`
**Represents:** an append-only ledger entry for every stock change. Five `type` values: `in`, `out`, `adjustment`, `transfer`, `stage_change`.
**Lifecycle:** append only. No edit/delete route.
**Business rules:**
- `in` and positive `adjustment` create a new `CostLayer`.
- `out` and negative `adjustment` consume layers FIFO and write `CostLayerConsumption` rows.
- `transfer` movements come in two flavours: TR1 ship/receive pair (with `transfer_order_id` set) or legacy instant-flip transfer route in `app/movements.py` (with `transfer_order_id` NULL).
- `stage_change` movements carry `qty=0`, populate `from_stage_id`/`to_stage_id`, write a corresponding `Item.current_stage_id` update.
- `po_id` populated on PO receipt; `stock_take_id` populated on stock-take commit adjustments.

### 3.9 `CostLayer` / `CostLayerConsumption`
**Represents:** the FIFO cost ledger. One `CostLayer` per receipt (PO or manual); consumption rows split outs across one or more layers.
**Lifecycle:** layers are immutable post-insert (only `qty_remaining` decrements). No deletes ever.
**Business rules:** FIFO order is `(item_id, received_at, id)` — backdated receipts land before existing layers; ties broken by id. `consume_fifo` raises `InsufficientStockError` *before any write* if the open total is short — route handlers map this to 400.

### 3.10 `PurchaseOrder` / `PurchaseOrderLine`
**Represents:** a supplier order document with line items.
**Lifecycle:** `draft → sent → (optionally) in_transit → partially_received → received` (or `cancelled` from `draft`/`sent`).
**Business rules:** lines are CASCADE-deleted with the PO (they're part of the doc). `expected_unit_cost` is planning estimate only — the actual unit cost is written on the new cost layer at receipt time. `qty_received` on a line never exceeds `qty_ordered`. PDF rendered via `app/pdf.py`, emailed via `app/email_backend.py`.

### 3.11 `StockTake` / `StockTakeLine`
**Represents:** a scheduled physical count with optional category or location scope (XOR).
**Lifecycle:** create (scheduled) → start (snapshots `system_qty` per line) → operator enters `counted_qty` per line → commit (writes paired adjustment movements; flips `committed=True` per line and `completed_at` on parent).
**Business rules:** scope_node_id and scope_location_id are mutually exclusive; both NULL = all items. Adjustment movements written by commit carry `stock_take_id` so the variance attribution lives in the audit + movement history.

### 3.12 `Checkout`
**Represents:** a tool / mould / item handed out to a user.
**Lifecycle:** check-out (insert row with `returned_at=NULL`) → return (set `returned_at`). No archive — checkouts are audit history.
**Business rules:** at most one open per item or item_unit; unique-tracked items must populate `item_unit_id`; qty-tracked items must leave it NULL.

### 3.13 `TransferOrder` / `TransferOrderLine`
**Represents:** a multi-line inter-location transfer with ship + receive timestamps.
**Lifecycle:** `draft → shipped → received` (or `cancelled` from `draft`). Cancellation from `shipped` is not currently supported.
**Business rules:** ship and receive each write one paired `transfer` movement per line, with the line's `ship_movement_id`/`receive_movement_id` populated and `transfer_order_id` set on the movements.

### 3.14 `AuditLog`
**Represents:** the immutable record of every state-changing route call.
**Lifecycle:** insert only; UPDATE/DELETE blocked by DB triggers.
**Business rules:** every mutating route MUST call `record_audit(...)` or be on the exempt list; `test_audit_coverage.py` is the forcing-function test that enforces this by parsing route handler source text.

---

## 4. UI surface

All HTML is server-rendered via `app/template_env.py` (shared `Jinja2Templates` with `csrf_context_processor` and `flash_context_processor`). Templates live in `app/templates/`. URL prefixes: most management surfaces sit under `/admin/`; scan surface at `/scan`.

Below is every template file with its purpose. Routes are listed per-domain.

### 4.1 Templates inventory

| Template | Purpose |
|---|---|
| `base.html` | Master layout (nav, role-aware menu, flash + CSRF wiring) |
| `_components.html` | Jinja macros (page headers, form-field helpers, buttons) |
| `index.html` | Landing page |
| `pending.html` | Notice for users in `pending` status |
| `admin_users.html` | Admin user list with role/status edit forms |
| `admin_audit.html` | Audit log table with filter + JSON peek + CSV link |
| `dashboard.html` | KPI dashboard (inventory value, low stock, top consumed, COGS, in-transit) |
| `reorder_dashboard.html` | Items below reorder threshold grouped by supplier; "Draft PO" buttons per group |
| `variance_trend.html` | Stock-take variance chart over time + CSV |
| `scan.html` | QR scan combobox + quick action links to in/out/detail |
| `items_list.html` | Items table filtered by category + show=active/archived |
| `items_form.html` | New/edit item form (driven by leaf field-def picks) |
| `items_form_builtins.html` | Fragment: built-in fields block |
| `items_form_fields.html` | Fragment: full post-category fields block (HTMX swap target) |
| `items_category_options_partial.html` | Fragment: category dropdown search results |
| `item_detail.html` | Read-only item page with open FIFO layers + recent movements |
| `item_stage_form.html` | Stage change form (lifecycle transition) |
| `item_units_list.html` | Units list for a unique-tracked item |
| `item_unit_form.html` | New/edit item unit form |
| `stock_in_form.html` | Stock receipt form (qty, unit_cost, reason, note, scan_next) |
| `stock_out_form.html` | Stock consumption form (qty, reason, note, open value summary) |
| `stock_adjust_form.html` | Variance adjustment form (qty, direction, unit_cost when increase, reason mandatory) |
| `stock_transfer_form.html` | Legacy single-item location transfer form |
| `taxonomy_list.html` | Top-level taxonomy list |
| `taxonomy_form.html` | New/edit taxonomy node form (name, archetype, sku_prefix, sort_order, defaults_json) |
| `taxonomy_children_list.html` | Sub-categories list per parent |
| `taxonomy_grandchildren_list.html` | Depth-2 list (manager-creatable for non-unique-variant; read-only for unique-variant auto-leaves) |
| `taxonomy_stages_list.html` | Stages list per top-level node |
| `taxonomy_stages_form.html` | New/edit stage form (name, sort, is_initial) |
| `taxonomy_field_defs_list.html` | Field-def picks for a node, with inherited groups + available catalog entries to pick |
| `suppliers_list.html` | Suppliers table + archive controls |
| `suppliers_form.html` | New/edit supplier form |
| `locations_list.html` | Locations table |
| `locations_form.html` | New/edit location form |
| `purchase_orders_list.html` | POs list filterable by status |
| `purchase_order_new_form.html` | New PO supplier picker |
| `purchase_order_detail.html` | PO detail (editable if draft; PDF + send buttons) |
| `purchase_order_receive_form.html` | Per-line receive form with unit cost inputs |
| `stock_takes_list.html` | Stock takes list |
| `stock_take_form.html` | New stock take form (scope_node_id XOR scope_location_id, scheduled_for) |
| `stock_take_detail.html` | Stock take session — line counts, progress, commit button |
| `transfers_list.html` | Transfer orders list |
| `transfers_form.html` | New transfer order form (multi-line) |
| `transfers_detail.html` | Transfer order detail with ship/receive/cancel actions |
| `checkout_form.html` | Quick checkout/return form |
| `checkouts_admin.html` | Open / overdue checkouts table |
| `csv_upload_form.html` | Generic CSV upload form (file input + submit) |
| `csv_upload_preview.html` | CSV preview/commit page — per-row `new/skip/error` results + warnings + commit button |

### 4.2 Route surface

**Note:** Two delegated agents helped survey the routes. Names from `taxonomy.py` and the upload-router prefixes were cross-verified against actual handler signatures in code; the field-defs and stages route paths use the precise shapes from §3 of my earlier taxonomy report. Where one agent named a path that did not match the source (e.g. `/admin/taxonomy/{id}/sub-categories`), I've used the verified path (`/admin/taxonomy/{parent_id}/children`).

#### Auth (`app/auth.py`)
- `GET /auth/google/login` → redirect to Google
- `GET /auth/google/callback` → upsert user, set session, redirect to `/`
- `POST /auth/logout` → clear session
- `GET /auth/me` → JSON `{id, email, name, role, status}`
- `POST /auth/_dev-login` (only mounted when `APP_ENV in {dev, test}`) — fields: `email`, `name`, `sub` (all optional with defaults)
- `/auth/_stub/*` (only when `APP_ENV=test AND OAUTH_STUB_MODE=1`, from `app/oauth_test_stub.py`) — fake authz, token, userinfo for Playwright

#### Root + admin users (`app/main.py`)
- `GET /` → `index.html` (or `pending.html` if pending)
- `GET /health` → JSON
- `GET /admin/users` (admin) → `admin_users.html` (CSV via `?format=csv`; headers: `id, email, name, role, status, created_at`)
- `POST /admin/users/{user_id}/role` (admin) — form `role`; prevents self-demotion
- `POST /admin/users/{user_id}/status` (admin) — form `status`; forbids activating role-less users; prevents self-disable

#### Items (`app/items.py` — the longest router, ~2700 lines)

Routes (verified from grep on `^@router|^@upload_router`):

| Route | Method | Role | Purpose |
|---|---|---|---|
| `/admin/items` | GET | manager/office/workshop | list; supports `show=active/archived`, `node_id`, `requires_checkout`, custom-field filters, `?format=csv` |
| `/admin/items/new` | GET | manager | new item form; takes `?node_id` to pre-load defaults |
| `/admin/items/_custom-fields` | GET | manager/office/workshop | **HTMX fragment** — re-renders the post-category fields block when category changes |
| `/admin/items/_category-search` | GET | manager/office/workshop | **HTMX fragment** — category dropdown search results |
| `/admin/items` | POST | manager | create item |
| `/admin/items/{item_id}/edit` | GET | manager/office | edit form |
| `/admin/items/{item_id}` | POST | manager/office | update item |
| `/admin/items/{item_id}/archive` | POST | manager | archive |
| `/admin/items/{item_id}/unarchive` | POST | manager | unarchive |
| `/admin/items/upload` | GET | manager | CSV upload form |
| `/admin/items/upload` | POST | manager | CSV preview / commit |

**Create form fields (POST `/admin/items`):** `sku` (auto-allocated; user value ignored on create), `name`, `taxonomy_node_id`, `unit`, `tracking_mode` (auto-set from archetype), `requires_checkout`, `reorder_threshold`, `reorder_qty`, `supplier_id`, `location_id`, `qr_code`, `notes`, `ring_size`, `weight_grams`, `stone_shape`. Visibility-driven via leaf-effective field-def picks.

Validation: name required when visibility is "required" (else auto-fills to SKU); unit required when visibility is "required" (else defaults to "ea"); reorder fields non-negative `Decimal`; qr_code partial-unique; weight_grams non-negative Decimal; supplier/location must exist + not be archived (unless unchanged); category must be a leaf (depth 0 leaf, or any depth-1/2 leaf, or depth-1 under `unique_variant`).

#### Item units (`app/item_units.py`)
- `/admin/items/{item_id}/units` GET (manager/office) — list
- `/admin/items/{item_id}/units/new` GET (manager) — form
- `/admin/items/{item_id}/units` POST — form: `serial_or_label`, `status`, `location_id`
- `/admin/items/units/{unit_id}/edit` GET / POST `/admin/items/units/{unit_id}` — same fields
- `/admin/items/units/{unit_id}/archive` POST / `/unarchive` POST

#### Stock movements (`app/movements.py`)
- `/admin/items/{item_id}/in` GET / POST — fields: `qty` (>0), `unit_cost` (≥0), `reason` (required), `note`, `scan_next`
- `/admin/items/{item_id}/out` GET / POST — fields: `qty` (>0), `reason` (required), `note`, `scan_next` — 400 on `InsufficientStockError`
- `/admin/items/{item_id}/adjust` GET / POST — fields: `qty` (>0), `direction` ∈ {`increase`, `decrease`}, `unit_cost` (required if increase), `reason` (mandatory), `note`, `scan_next`
- `/admin/items/{item_id}/transfer` GET / POST — legacy instant-flip transfer; fields: `from_location_id`, `to_location_id`, `qty` (required for unique-tracked, ignored for qty-tracked), `reason` (required), `note`
- `/admin/items/{item_id}/detail` GET — item detail page with open layers + recent movements
- `/admin/items/{item_id}/stage` GET / POST — stage change form

#### Taxonomy (`app/taxonomy.py`)
URL surface as previously documented:
- `/admin/taxonomy` (list, `?format=csv`)
- `/admin/taxonomy/new`, `POST /admin/taxonomy` (create top-level)
- `/admin/taxonomy/{id}/edit`, `POST /admin/taxonomy/{id}` (edit; archetype + sku_prefix locked when descendant items exist)
- `POST /admin/taxonomy/{id}/archive`, `POST /admin/taxonomy/{id}/unarchive`
- `/admin/taxonomy/{parent_id}/children` (list + new), `POST` (create sub-cat)
- `/admin/taxonomy/sub/{id}/edit`, `POST /admin/taxonomy/sub/{id}` (edit), archive/unarchive — serves both depth 1 and depth 2
- `/admin/taxonomy/{parent_id}/sub/{sub_id}/grandchildren` (list + new), `POST` (create depth 2 — refused under unique_variant)
- `/admin/taxonomy/{node_id}/stages` (list, new), `POST /admin/taxonomy/{node_id}/stages`
- `/admin/taxonomy/stages/{stage_id}/edit`, `POST /admin/taxonomy/stages/{stage_id}`, archive/unarchive
- Bulk CSV upload routes on `upload_router`:
  - `POST /admin/taxonomy/upload` (top-level)
  - `POST /admin/taxonomy/{parent_id}/children/upload`
  - `POST /admin/taxonomy/{parent_id}/sub/{sub_id}/grandchildren/upload`

Form fields for create/edit top-level (`POST /admin/taxonomy`): `name`, `sort_order`, `archetype`, `sku_prefix`, `default_unit`, `default_tracking_mode`, `default_requires_checkout`, `default_reorder_threshold`, `default_reorder_qty`, `default_supplier_id`, `default_location_id`.

#### Field defs (`app/field_defs.py`)
- `GET /admin/taxonomy/{node_id}/fields` — list with inherited groups + available catalog entries
- `POST /admin/taxonomy/{node_id}/fields/pick` — form `catalog_key`
- `POST /admin/taxonomy/fields/{field_id}/archive` — **hard-deletes** (route name retained for RBAC sweep stability)
- `POST /admin/taxonomy/fields/{field_id}/unarchive` — 400s with "no longer supported"

#### Suppliers (`app/suppliers.py` + `upload_router`)
- `/admin/suppliers` list (`?format=csv`)
- `/admin/suppliers/new`, `POST /admin/suppliers`
- `/admin/suppliers/{id}/edit`, `POST /admin/suppliers/{id}`
- archive/unarchive
- `/admin/suppliers/upload` GET/POST — CSV headers: `name, email`

#### Locations (`app/locations.py` + `upload_router`)
Same shape as suppliers. CSV headers: `name`.

#### Purchase orders (`app/purchase_orders.py`)
- `POST /admin/reorder/draft-po` — create draft PO from supplier group on reorder dashboard
- `GET /admin/purchase-orders` list (`?format=csv`)
- `GET /admin/purchase-orders/new` (new PO form), `POST` (create)
- `GET /admin/purchase-orders/{po_id}` — detail/edit (editable while draft)
- `POST /admin/purchase-orders/{po_id}` — update draft
- `POST /admin/purchase-orders/{po_id}/cancel`
- `POST /admin/purchase-orders/{po_id}/mark-shipped` (POIT1)
- `POST /admin/purchase-orders/{po_id}/send` — render PDF + email to supplier
- `GET /admin/purchase-orders/{po_id}/receive` — receive form
- `POST /admin/purchase-orders/{po_id}/receive` — write receipt movements + cost layers
- `GET /admin/purchase-orders/{po_id}/pdf` — reportlab PDF

Receive form fields: per-line `qty_received_<line_id>` (≤ qty_ordered − previously received) and `unit_cost_<line_id>` (required if qty_received > 0; non-negative Decimal).

#### Stock takes (`app/stock_takes.py`)
- `GET /admin/stock-takes` (`?format=csv`)
- `GET /admin/stock-takes/new`, `POST /admin/stock-takes`
- `GET /admin/stock-takes/{id}` — detail with line counts
- `POST /admin/stock-takes/{id}/start` — flips `started_at`
- `POST /admin/stock-takes/{id}/counts` — saves `counted_qty` per line
- `POST /admin/stock-takes/{id}/commit` — variance → adjustment movements + flips `completed_at`

#### Transfers (`app/transfers.py`)
- `GET /admin/transfers` (`?format=csv`)
- `GET /admin/transfers/new`, `POST /admin/transfers`
- `GET /admin/transfers/{id}` — detail
- `POST /admin/transfers/{id}/ship`, `/receive`, `/cancel`

#### Checkouts (`app/checkouts.py` + `app/checkouts_admin.py`)
- `GET /admin/items/{item_id}/checkout`, `POST /admin/items/{item_id}/checkout`
- `POST /admin/checkouts/{checkout_id}/return`
- `GET /admin/checkouts` (admin index, `?show=open|overdue`, `?format=csv`)

#### Dashboard / Reports / Reorder / Scan / Audit
- `GET /admin/dashboard` (manager/office/admin) — `dashboard.html` with KPIs; query params `top_days` (default 30), `cogs_start`, `cogs_end`
- `GET /admin/variance-trend` (manager/office) — `variance_trend.html`; query `days` (default 30); CSV via `?format=csv`
- `GET /admin/reorder` (manager/office) — `reorder_dashboard.html`; CSV via `?format=csv`
- `GET /scan` — QR scan page
- `POST /scan/resolve` — resolve QR → item
- `GET /scan/item/{item_id}` — item summary panel
- `GET /admin/audit` (manager/admin) — audit log table; CSV via `?format=csv`

### 4.3 Stubs and placeholders

- **`POST /admin/taxonomy/fields/{field_id}/unarchive`** — explicit no-op that 400s. Kept for RBAC test stability and listed in `_EXEMPT_FROM_AUDIT_WRITE`.
- **`/admin/items/{item_id}/transfer`** (legacy single-item instant location flip in `app/movements.py`) — preserved alongside the newer `/admin/transfers/...` flow.
- **`app/oauth_test_stub.py`** — entire module is a test-only mount.
- **README `_TODO` placeholders** — three remain intentionally (Quick Links deployed-URL, Tech-Stack deploy-target, Deployment section). Pinned by tests in `tests/integration/test_readme.py`.

No other stubbed routes, empty pages, or "coming soon" placeholders identified.

---

## 5. Data flows and integrations

### 5.1 How data gets in

| Path | Surface | Implementation |
|---|---|---|
| Manual entry (forms) | All `/admin/*` routes | Jinja+HTMX forms with `app/csrf.py` middleware |
| Barcode scan | `/scan` | QR lookup → `app/scan.py:resolve_qr` → redirect to item or movement form |
| CSV bulk upload | Five surfaces: items, suppliers, locations, top-level taxonomy, depth-1 children, depth-2 grandchildren | `app/csv_import.py` shared machinery (5 MB cap, 5000 row cap, UTF-8 + BOM, `.numbers` rejection) |
| Google OAuth | `/auth/google/callback` | Authlib OIDC discovery against `accounts.google.com` |

### 5.2 External systems

| System | Status | File | Env vars |
|---|---|---|---|
| Google Workspace (OAuth/OIDC) | **Integrated** | `app/auth.py` | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `APP_BASE_URL`, `BOOTSTRAP_ADMIN_EMAIL`, `OAUTH_STUB_MODE` |
| SMTP email | **Integrated** (PO send only) | `app/email_backend.py` | `EMAIL_BACKEND` (`console`/`smtp`), `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_USE_TLS` |
| Fly.io | Deploy target | `fly.toml`, `Dockerfile`, `scripts/fly-entrypoint.sh` | (deploy-time only) |
| HubSpot | **Not integrated** | — | — |
| Xero / accounting | **Not integrated** | — | (Out of scope per MISSION §3) |
| Stripe / payments | **Not integrated** | — | — |
| Slack | **Not integrated** | — | — |
| Casting facility / external job systems | **Not integrated** | — | (Out of scope per MISSION §3 — "treat consumption as opaque `out` movements") |
| Webhook receivers | **None** | — | — |

`httpx` is in the deps but appears only as an internal test client + the OAuth-stub server's loopback. No outbound HTTP calls from `app/` other than via Authlib and `smtplib`.

### 5.3 Sync patterns

- **All writes are synchronous request-response.** No background queues, no webhook receivers, no fire-and-forget jobs.
- **No external sync of any kind.** Items, movements, POs, etc. are owned by this app and only flow out via CSV export or the PO PDF email.

### 5.4 Scheduled jobs

**APScheduler is a declared dependency in `pyproject.toml` but not wired up anywhere in `app/`.** Grep confirms: no `BackgroundScheduler`/`AsyncIOScheduler` imports, no scheduler instance, no registered jobs. Stock takes are manually scheduled (date field on form); reorder checks are manual (visit dashboard).

### 5.5 Caching / denormalisation

Four denormalised fields, each with a single mutation pathway:

| Field | Source of truth | Mutated by | Consistency posture |
|---|---|---|---|
| `Item.current_qty` | sum of open `cost_layers.qty_remaining` | `app/cost_engine.py:record_receipt` and `consume_fifo` only | Mutated in same transaction as the layer write; movements are append-only so it never goes stale. |
| `Item.assigned_sequence` | trailing numeric segment of the SKU | Set once at `app/items.py` create, never touched again | SKUs are immutable per MISSION §3. |
| `TaxonomyNode.next_sequence` | "the next SKU sequence to allocate at this allocator" | `app/sku.py:allocate_sequence` via atomic `UPDATE ... RETURNING next_sequence` | Single UPDATE → serialised on the row by both SQLite ≥ 3.35 and Postgres. |
| `Item.current_stage_id` | most recent `stage_change` movement's `to_stage_id` | `app/items.py` stage-transition handler | Single write + audit trail via `stock_movements`. |

No application-layer caches (no Redis, no in-process LRU on hot reads). The dashboard's "current inventory value" is computed in real time by summing open layers.

---

## 6. What the app currently tracks well

Concrete list of capabilities verified live:

- **Items in a 1-3-level category tree** with system-allocated SKUs (`RAW-SIL-925-0008` style for bulk/unique; `RTS-EM-001` style for unique_variant where the design family auto-creates a depth-2 leaf per piece).
- **Three archetypes** — `bulk` (qty-tracked stock), `unique` (one-record-per-physical-unit), `unique_variant` (auto-numbered design pieces like an Emma ring family). Tracking mode is forced from the archetype.
- **Per-leaf field visibility** — managers pick which of 13 catalog fields (`name`, `unit`, `tracking_mode`, `requires_checkout`, `reorder_threshold`, `reorder_qty`, `supplier_id`, `location_id`, `qr_code`, `notes`, `ring_size`, `weight_grams`, `stone_shape`) appear on items in a given category. Picks inherit downward.
- **Per-category defaults** — manager-set defaults for `unit`, `tracking_mode`, `requires_checkout`, `reorder_threshold`, `reorder_qty`, `supplier_id`, `location_id` substitute into the items create form.
- **Lifecycle stages** per top-level category (e.g. `raw → polishing → QC → ready-to-ship → shipped`). Transitions logged as `stage_change` movements.
- **FIFO cost tracking** with variable per-receipt pricing — every receipt creates an immutable cost layer; outs and negative adjustments consume layers FIFO; `unit_cost_at_consumption` is captured per consumption row.
- **Stock movements** — five types (in/out/adjustment/transfer/stage_change), all append-only, all attributed to the actor.
- **Item units** for unique-tracked items — multiple physical units per item, each with its own serial/label, location, and (derived) checkout state.
- **Checkouts** — open/return cycle for tools/moulds/items marked `requires_checkout=True`. Overdue derived from `expected_return`.
- **Purchase orders** — supplier-grouped draft from reorder dashboard, manager edits + sends (PDF + email), in-transit, partial/full receipt, cost layers spawned at receipt with actual unit cost.
- **Stock takes** — scope by category or location, snapshot system_qty, operator-entered counted_qty, commit creates variance adjustment movements.
- **Transfer orders** — multi-line inter-location transfers with ship/receive lifecycle, paired `transfer` movements per line.
- **Audit log** — every state-changing route writes a before/after JSON snapshot; DB triggers prevent edit/delete.
- **CSV export everywhere** — 13 list views all support `?format=csv` snapshots.
- **CSV bulk upload** — items, suppliers, locations, and the three taxonomy levels (preview + commit, hash-based idempotency, per-row `new/skip/error` results).
- **QR code scanning** — printable labels, scan page resolves to item, two-tap in/out/adjust workflow.
- **Role-based access** — server-side gating on every route (`admin > manager > office > workshop`).
- **Dashboard** — total inventory value, low-stock count, open POs, in-transit transfers, top-consumed (configurable window), COGS over date range.
- **Reorder dashboard** — items below reorder_threshold grouped by supplier with one-click draft-PO.
- **Variance trend report** — stock-take variances aggregated by category × day.

---

## 7. Known gaps, hacks, and TODOs

### 7.1 Code-level TODO/FIXME/HACK
**None found in `app/`, `tests/` (other than `tests/integration/test_readme.py` which pins README placeholders), `migrations/`, or `scripts/`.** Grep across all four trees returned only matches inside `test_readme.py` and they all reference the literal string `_TODO_` to assert the README *no longer* contains placeholders.

### 7.2 README placeholders pinned by tests
Three `_TODO` placeholders remain intentionally unresolved in `README.md`, all gated on the test_readme assertions (i.e. when these slices ship, the tests flip):
- Quick Links "deployed URL" (currently resolved post-P4 — pinned at line 603 of test_readme.py)
- Tech-Stack deploy-target (currently resolved post-P4)
- Deployment section (gated on P4 — currently resolved per test_readme.py line 728)

### 7.3 Live bug list from `testnotes.md` (manual audit dated 2026-05-07)
12 items, surfaced by manual end-to-end testing:
- 4 high-severity JSON-rendering regressions: items create with bad custom field, item transfer with no location, PO receive over-receipt — all return raw JSON instead of re-rendering the form on 400.
- 2 medium labelling: category dropdown labels non-leaf parents as "(archived)" incorrectly; select field options don't degrade without JS.
- 2 medium usability: reorder dashboard shows zero-threshold items (noise); decimal inputs don't show decimal point on mobile keyboards.
- 4 low: no per-item checkout history view; 4 decimal places everywhere is visually noisy; movement timestamps lack timezone marker in UI; favicon 404.

### 7.4 Fields that exist but aren't used
- **`CostLayerConsumption.unit_cost_at_consumption`** is always equal to the immutable `CostLayer.unit_cost` (the comment in `models.py` documents this — kept for "report self-containedness" so reports don't need a join to layers).
- **`PurchaseOrderLine.expected_unit_cost`** is informational only; the cost layer at receipt time carries the authoritative unit cost.
- **`TransferOrderLine.qty`** is informational in v1 (whole-item relocations) but reserved for a future per-location-qty refactor.
- **`Item.assigned_sequence`** is a denormalised cache of the SKU's trailing number — derivable but kept for fast round-trip.
- **`Item.current_stage_id`** — for categories with no stages defined, this is permanently NULL. NULL is a legitimate value, not a gap.
- **`StockMovement.transfer_order_id`** — NULL for legacy `/admin/items/{id}/transfer` instant-flip transfers; populated only by TR1 ship/receive.

### 7.5 Fields that should exist but don't
*None claimed by the codebase*. MISSION §3 documents the v1 scope; everything in scope appears to be implemented. Two areas the codebase explicitly leaves to future slices:
- **Per-location quantity** — `TransferOrderLine.qty` is a stub; today items are atomic (`location_id` is on `items`, not per-location). Multi-location stock is not modelled.
- **Photo attachments / item images** — not in MISSION; not modelled.
- **Audit log query indexes for free-text search** — `action`/`entity_type` are indexed but `before_json`/`after_json` are not (JSON columns).

### 7.6 Workarounds documented in code
- **`app/field_visibility.py`** is a "thin compatibility shim" — the override mechanism (per-leaf field-visibility JSON) was removed in 0023 but the function is kept so route call sites don't churn. It now returns a constant default visibility map.
- **`taxonomy_field_defs` unarchive route** kept as a 400-returning no-op for RBAC test stability.
- **`sku_prefix` auto-disambiguation** — when a manager submits a top-level category without a prefix, `_disambiguate_top_prefix` derives one from name + suffixes `2`, `3` etc. if there's a sibling collision. Explicit user-supplied prefixes do not auto-disambiguate (collision → 400).
- **`effective_archetype` safety net** — orphaned trees / legacy fixtures with `archetype IS NULL` at depth 0 default to `BULK` so the items create route doesn't 400 on rows the rest of the app is happy to read.
- **`Item.qr_code` `partial unique` index** — multiple items can share NULL (no label printed yet) while every printed label is one-to-one.
- **Movements are append-only** — corrections are documented as "new compensating movements with reasons that name the original" rather than edits. MISSION §3 + §9 both forbid edit/delete.

### 7.7 Free-text where it might want to be a lookup
Judging only from model docstrings + form definitions:
- **`Item.unit`** is `String(32)` free-text (e.g. "kg", "grams", "pieces", "ea"). No `Unit` table exists. Per-category `defaults_json.unit` lets managers pre-fill but doesn't constrain.
- **`Movement.reason`** is `String(255)` free-text on every movement type. Adjustment routes mandate non-blank but otherwise it's free entry — no taxonomy of reasons.
- **`Item.ring_size`** is `String(64)` free-text. No size standard / lookup table.
- **`Item.stone_shape`** is `String(64)` free-text.
- **`TransferOrder.carrier`** and **`tracking_number`** are free-text.

### 7.8 Lookup where it might want to be free-text
None identified — the lookup-shaped fields (`supplier_id`, `location_id`, `taxonomy_node_id`, `current_stage_id`, `item_unit_id`) all reference administered tables that benefit from the constraint.

### 7.9 BLOCKED.md
**Not present.** Per CLAUDE.md, `BLOCKED.md` is the loop's halt signal; its absence means the loop is in a green state.

---

## 8. Open questions and assumptions

These are observations a reader of the code would naturally surface; the architect should confirm or override.

1. **Multi-location quantity model.** Items today carry one `location_id` per item. Stock movements aren't location-aware (no `from_location`/`to_location` columns on `stock_movements`; only `TransferOrder` carries the pair). If multi-location qty is a future requirement, the schema needs a per-(item, location) qty table and `current_qty` semantics will change. `TransferOrderLine.qty` is a stub for that future shape.

2. **Cost layer location.** Cost layers are not location-scoped. If two locations hold different historical batches at different costs, the FIFO engine cannot reflect that today.

3. **`Item.unit` free-text vs. lookup.** Several listings rely on the operator entering a consistent string (`"kg"` vs `"kgs"` vs `"kilograms"`). No normalisation. Whether to keep as free-text or introduce a `Unit` lookup is open.

4. **Stone / gem modelling.** Only three stone-adjacent fields exist (`ring_size`, `weight_grams`, `stone_shape`) and all are flat columns. Multi-stone rings (centre stone + side stones), stone provenance, certification numbers (GIA/IGI), or carat / colour / clarity attributes are **not modelled**. Adding them is a code change + migration per the catalog-driven design — but whether the architect wants a flat-column extension or a normalised `item_stones` table is open.

5. **Auto-leaf naming for `unique_variant`.** Today the auto-leaf's `name` and `sku_prefix` are both `f"{sequence:03d}"` (e.g. `"001"`). No metadata travels with the auto-leaf — design name lives on the parent depth-1 sub-cat. Future "name this piece" requirements would need an additional field on the auto-leaf or item.

6. **Stage transitions aren't gated.** The route layer accepts any `to_stage_id` belonging to the item's top-level category; there's no DAG of allowed transitions (e.g. "can't skip from raw to shipped"). MISSION doesn't specify this; it may be intentional.

7. **No per-item images.** Items have `qr_code` and `notes` but no attachment slot. If product photos are wanted, schema + storage decisions are open.

8. **PO email send is fire-and-forget.** If SMTP fails after the audit row is written, there's no retry queue. APScheduler dep is unused; a retry loop could land there.

9. **Checkout `expected_return`** is `DateTime(tz)` — used by the overdue derivation. UI input is currently a date picker per testnotes; whether expected_return should be a date or datetime is open.

10. **`taxonomy_field_defs.required`** is stored but the items form's required/optional/hidden model uses `effective_field_visibility` (currently a constant map). The two are not yet wired: marking a pick "required" doesn't currently change form validation. Verify intent before reconnecting them.

11. **CSV import for taxonomy stages and field-defs.** Five CSV upload surfaces exist (items + suppliers + locations + three taxonomy levels) but stages and field-def picks are manual-only. Open whether to add.

12. **Status enum for `stock_takes`.** No `StockTakeStatus` enum; lifecycle derived from `started_at`/`completed_at`. A future commit could re-cancel a stock take, but no cancel route exists today.

13. **`POStatus.CANCELLED` from `partially_received`.** Lifecycle docs only show cancel from `draft` or `sent`. Whether `partially_received → cancelled` is allowed is unclear from code (need to read `purchase_orders.py` cancel handler).

---

## 9. Sample data

Real rows from `dev.db` (5 items, 16 taxonomy nodes, 6 suppliers, 8 locations, 2 stock movements, 152 audit rows, 1 transfer order). Lightly anonymised where relevant.

### 9.1 `taxonomy_nodes` — the live tree

```
id | parent_id | name               | archetype       | sku_prefix | next_sequence
---+-----------+--------------------+-----------------+------------+--------------
 1 |  (root)   | RTS Rings          | unique_variant  | RTS        | 1
 2 |  (root)   | Customer Packaging | bulk            | PK         | 1
 3 |  (root)   | Stock ring         | unique_variant  | ST         | 1
 7 |  (root)   | VM Ring            | unique_variant  | VM         | 1
 9 |  (root)   | Consms             | bulk            | CONSS      | 1
10 |  (root)   | Test Category      | unique          | TEST       | 1
13 |  (root)   | Ready Rings        | unique_variant  | RR         | 1
 4 |    1      | Emma               | (inherits)      | EM         | 4
 5 |    3      | Emma               | (inherits)      | EM         | 1
 8 |    7      | Daisy              | (inherits)      | DAI        | 1
14 |   13      | Helena             | (inherits)      | HEL        | 3
 6 |    4      | 001                | (inherits)      | 001        | 1   ← auto-leaf
11 |    4      | 002                | (inherits)      | 002        | 1   ← auto-leaf
12 |    4      | 003                | (inherits)      | 003        | 1   ← auto-leaf
15 |   14      | 001                | (inherits)      | 001        | 1   ← auto-leaf
16 |   14      | 002                | (inherits)      | 002        | 1   ← auto-leaf
```

Note the depth-2 children of "Emma" (id 4) and "Helena" (id 14) are the system-minted auto-leaves — one per unique_variant item. Their `next_sequence=1` because the allocator lives one level up on the sub-cat (id 4 has `next_sequence=4`, having minted three Emma rings; id 14 has `next_sequence=3`, having minted two Helena rings — note the off-by-one: `next_sequence` is "the next number to allocate", so after allocating 1 and 2 it sits at 3).

### 9.2 `items` — every row in the dev DB

```
id | sku        | name       | node_id | unit | tracking_mode | current_qty | assigned_sequence
---+------------+------------+---------+------+---------------+-------------+--------------------
 1 | RTS-EM-001 | RTS-EM-001 |    6    | ea   | unique        |    0        |       1
 2 | RTS-EM-002 | RTS-EM-002 |   11    | pc   | unique        |    0        |       2
 3 | RTS-EM-003 | RTS-EM-003 |   12    | pc   | unique        |    1        |       3
 4 | RR-HEL-001 | RR-HEL-001 |   15    | ea   | unique        |    0        |       1
 5 | RR-HEL-002 | RR-HEL-002 |   16    | ea   | unique        |    1        |       2
```

All five items are `unique_variant`. SKU shape `<ROOT>-<SUB>-<NNN>` — root prefix from depth 0 (e.g. RTS), sub prefix from depth 1 (e.g. EM = Emma), trailing sequence from depth 2 auto-leaf. None has `ring_size`, `weight_grams`, `stone_shape`, `supplier_id`, `location_id`, or `current_stage_id` populated (the demo dataset is bare-bones; field-def picks weren't applied).

### 9.3 `stock_movements` — both rows

```
id | item_id | type | qty | reason     | po_id | total_cost | created_at
---+---------+------+-----+------------+-------+------------+---------------------
 1 |    3    | in   |  1  | csv_upload |       |    110     | 2026-05-14 03:07:50
 2 |    5    | in   |  1  | csv_upload |       |    111     | 2026-05-14 04:49:31
```

Both are CSV-bulk-upload-driven receipts; reason is the synthetic `csv_upload` marker. `unit_cost` lives on the corresponding cost layer.

### 9.4 `cost_layers` — both rows

```
id | item_id | qty_received | qty_remaining | unit_cost | source     | source_movement_id
---+---------+--------------+---------------+-----------+------------+--------------------
 1 |    3    |      1       |      1        |    110    | manual_in  |        1
 2 |    5    |      1       |      1        |    111    | manual_in  |        2
```

No consumption rows yet.

### 9.5 `suppliers` and `locations`

```
suppliers:                              locations:
 id | name                                id | name
----+----------------------------         ---+--------------------
  1 | Acme Materials Co                    1 | UC Thailand
  2 | London Bullion Co                    2 | Melbourne Workshop
  3 | Diamond Imports Ltd                  3 | Storage A
  4 | Jewellers Tools UK                   4 | Workshop Bench
  5 | Acme Findings                        5 | Safe — Vault
  6 | Test supplier (archived 2026-05-14)  6 | Display Case A
                                           7 | Display Case B
                                           8 | Quarantine
```

### 9.6 `audit_log` — most recent rows

```
id  | actor_id | action               | entity_type    | entity_id | after_json (excerpt)
----+----------+----------------------+----------------+-----------+----------------------------------
152 |    1     | supplier.archived    | supplier       |    6      | {"archived_at": "2026-05-14T...
151 |    1     | supplier.csv_uploaded| supplier       |  (NULL)   | {"count": 1, "updated_count": 0, ...
150 |    1     | supplier.created     | supplier       |    6      | {"name": "Test supplier", "email": ...
149 |    1     | item.csv_uploaded    | item           |  (NULL)   | {"count": 1, "file_sha256": "ff154...
148 |    1     | stock_movement.in    | stock_movement |    2      | {"item_id": 5, "qty": "1", "unit_cost...
```

Bulk-upload audit rows carry `entity_id IS NULL` and an `after_json` summary `{count, updated_count, file_sha256, ...}`. Per-row audits sit alongside on the same upload as separate entries.

---

## 10. Diff target — adding a single new field

Worked example: adding **`Item.diamond_carat`** (a non-negative `Decimal(14,4)`, optional, only surfaced on items in categories where a manager picks it).

This walks through the catalog-driven path. Steps in order:

### Step 1 — schema

1. **`app/models.py`** — add the column to the `Item` model alongside `ring_size`/`weight_grams`/`stone_shape`:
   ```python
   diamond_carat: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
   ```
2. **`migrations/versions/0025_add_diamond_carat.py`** — new Alembic migration: `op.add_column('items', sa.Column('diamond_carat', sa.Numeric(14,4), nullable=True))` and the matching `op.drop_column` in `downgrade()`. Migrations are forever per CLAUDE.md "Loop posture".

### Step 2 — catalog wiring

3. **`app/field_catalog.py`** — add a `CatalogEntry`:
   ```python
   CatalogEntry(
       key="diamond_carat",
       label="Diamond carat",
       type=FieldType.DECIMAL,
       column="diamond_carat",
       sort_order=230,
   ),
   ```
4. **`app/field_visibility.py`** — append `"diamond_carat"` to `BUILT_IN_FIELDS` and add `"diamond_carat": "optional"` to `_DEFAULT_VISIBILITY`.

### Step 3 — items form + routes

5. **`app/items.py`** — handle the new form field in:
   - `_collect_form_values` (or whatever the create/update routes read forms with) — parse the form field `diamond_carat`, coerce via `Decimal`, non-negative validation.
   - `create_item` and `update_item` handlers — set `item.diamond_carat`.
   - The `_built_in_field_values` / form-context builder — include the field in the render-side dict so `items_form.html` can echo it back on validation errors.
   - The `_FIELDS` / audit-diff list if items maintains one similar to taxonomy's — add `"diamond_carat"` so the audit `before/after` includes it.
   - The CSV export header list + row builder — add `diamond_carat` column.
   - The CSV upload header validation list + row coercion — add `diamond_carat` parsing.

### Step 4 — templates

6. **`app/templates/items_form_builtins.html`** (or whichever fragment renders catalog fields based on visibility) — add a `<input name="diamond_carat" type="number" step="0.0001">` block wrapped in the same `{% if visibility.diamond_carat != "hidden" %}` pattern used by the other catalog fields. If the project uses a generic catalog-driven render loop, no template edit is needed; if each field has a hand-rolled block, one is needed.
7. **`app/templates/items_list.html`** — add a `<th>` and `<td>` for `diamond_carat` inside the per-row catalog-field render loop (if it iterates the catalog) or hand-rolled (if it doesn't).

### Step 5 — read/format helpers

8. **`app/field_storage.py`** — no edit needed if the column-backed read path is generic (`getattr(item, entry.column)`); the existing `format_for_display`/`format_for_csv` already handle `Decimal`.

### Step 6 — CSV import

9. **`app/csv_import.py`** — likely no edit if header validation is catalog-driven; otherwise add `diamond_carat` to the items header whitelist.

### Step 7 — tests

10. **`tests/integration/test_items_routes.py`** — add tests:
    - Create with `diamond_carat` value visible in the form.
    - Create rejects negative `diamond_carat`.
    - CSV export includes the column.
    - CSV upload imports the column.
    - Audit diff captures `before/after` for `diamond_carat`.
11. **`tests/integration/test_audit_coverage.py`** — re-runs automatically; should stay green because audit signature didn't change.
12. **`tests/e2e/`** — optional Playwright walk if it's part of a golden-path slice.

### Step 8 — verification + commit

13. **`make migrate`** to apply the migration locally.
14. **`make check`** — runs lint + mypy + pytest + Playwright as the verification gate.
15. **Commit** with conventional message (or `slice: add-diamond-carat (DoD #<n>)` if working inside the loop), then `git push origin main`.

**Approximate blast radius for a typical column-backed catalog field:** 2 schema files, 2 catalog/visibility shims, 1 routes file with ~6 touch-points, 2-3 templates, 1 test file, 1 migration. The catalog-driven design keeps the routes file's number of touch-points relatively flat — you don't have to update a switch statement or a typed schema; you add one catalog entry and one column.

For a field that should NOT be column-backed (e.g. arbitrary multi-stone array), the design pattern is different (per MISSION §3 the catalog is "hardcoded; adding a new field is a code change plus a migration, never a settings-UI action") and would require revisiting whether to re-introduce a normalised side-table like the dropped `item_field_values`.

---

*End of document. 2026-05-15 snapshot, commit `237e66f` on `main`.*
