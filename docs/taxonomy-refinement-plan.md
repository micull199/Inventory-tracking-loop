# Taxonomy refinement plan

Plan for extending UC Inventory's taxonomy from a fixed two-level tree to a
flexible 1-to-3-level archetype-driven hierarchy, and reshaping the New Item
flow to match. Australian English throughout. No em dashes.

This document is discovery + plan only. Downstream agents implement against
it. The plan is concrete: file paths, line ranges, column names, route paths.
Where the requirements clash with current code I have made the call in
writing rather than punting.

## 0. Addendum: user-confirmed unique-variant model

After the plan was drafted, the user selected interpretation C for the
unique-variant SKU shape question (R1): **each unique-variant item gets its
own auto-created depth-2 leaf**. This supersedes the depth-2-with-named-leaf
model in sections 4 and 5 below. The revised rules:

### Tree shape for unique-variant
- Manager manually creates the depth-0 top-level (e.g. RTS Rings, archetype
  = `unique_variant`, prefix = `RTS`) and the depth-1 sub-category (e.g.
  Emma, prefix = `EM`).
- Manager DOES NOT create depth-2 nodes manually under a unique-variant
  tree. Depth 2 is system-managed.
- On unique-variant item create, the user picks the depth-1 sub-category
  in the New Item picker. The system atomically:
  1. Increments the sub-cat's `next_sequence` (returns N).
  2. Creates a depth-2 leaf with `name = f"{N:03d}"`, `sku_prefix = f"{N:03d}"`,
     `parent_id = sub_cat.id`, no archetype (inherited).
  3. Creates the item on that auto-leaf with `assigned_sequence = N`,
     `tracking_mode = unique`.
- Resulting SKU = `RTS-EM-001`, composed uniformly as
  `'-'.join(n.sku_prefix for n in ancestor_chain(auto_leaf))`.

### Allocator location (`next_sequence`)
- Bulk / unique: lives on the user-created leaf (any depth). The leaf can
  hold many items; each gets a 4-digit sequence appended (existing
  `<PREFIX>-<NNNN>` shape preserved).
- Unique-variant: lives on the depth-1 sub-category. Each allocation makes
  a new auto-leaf below it; that auto-leaf holds exactly one item.

### Compose SKU (replaces section 4's `compose_sku` description)

```python
def compose_sku(db, leaf, sequence, archetype):
    prefixes = [n.sku_prefix for n in ancestor_chain(db, leaf)]
    if archetype == Archetype.UNIQUE_VARIANT:
        # leaf.sku_prefix already equals f"{sequence:03d}"
        return "-".join(prefixes)
    # bulk + unique append a 4-digit sequence
    return "-".join(prefixes) + f"-{sequence:04d}"
```

### Picker semantics (replaces section 5's flat-leaves-only model)

A node is "pickable as an item destination" if any of:
- It is a leaf (no active children) AND its `effective_archetype` is
  `bulk` or `unique`. (Item lands directly on this leaf.)
- It is a depth-1 sub-category AND its `effective_archetype` is
  `unique_variant`. (Item lands on a freshly-allocated auto-leaf below.)

For unique-variant trees, the depth-1 sub-cats are picked even though they
have children. The auto-leaves themselves never appear in the picker (they
each hold their single item and are full).

### Taxonomy admin: no depth-2 actions for unique-variant trees

When rendering a depth-1 sub-category under a `unique_variant` top-level,
the taxonomy admin shows:
- "Items here" link (lists the auto-leaves under this sub-cat, since each
  auto-leaf has one item).
- "Add item" CTA (which routes through the New Item form with this sub-cat
  pre-selected).
- No "Add sub-sub-category" action.

For bulk / unique sub-cats at depth 1, the "Add sub-sub-category" action
appears (and respects the existing container-or-leaf invariant).

### Items list filter

A unique-variant tree's items live on depth-2 auto-leaves whose names are
3-digit numbers. The items list filter (`?taxonomy_node_id=...`) needs to
match items by walking the descendant ids of the selected node, not by
exact `taxonomy_node_id ==`. The downstream agent should generalise the
existing filter (`app/items.py`) to "items whose taxonomy_node_id is in
the set of descendants including the selected node id".

### Migration impact

No change to the migration plan in section 3: no existing items are
unique-variant, so no auto-leaves need to be backfilled. The schema
changes (`archetype`, `sku_prefix`, `next_sequence`, `assigned_sequence`)
remain as specified. The seeded `next_sequence` for existing leaves
applies to bulk archetype only (which is what every existing leaf becomes
after the migration).

### Container-or-leaf invariant under the new rule

The strict "either container or leaf" rule applies to **bulk + unique**
trees only. Unique-variant depth-1 sub-cats are intentionally hybrid:
they hold auto-leaves as children (so they look like containers) while
also being the user-picked "where the item goes" (so they look like
leaves). This is an explicit exemption documented in code on the picker
helper and on `_resolve_leaf_node`.

### What about the leaf prefix being a number?

The auto-leaf's `sku_prefix` is `"001"`, `"002"`, .... The schema's
`sku_prefix` validator accepts uppercase alnum; digits are allowed.
Sibling uniqueness on `(parent_id, sku_prefix)` is naturally satisfied
because `next_sequence` is monotonic.

---

The remainder of this document keeps the original plan text. Where the
addendum above conflicts with section 4 or 5, the addendum wins.

Conventions used here:
- "depth" is zero-indexed: a top-level node is depth 0, a sub-category is
  depth 1, a sub-sub-category is depth 2. "Three levels" therefore means
  depths 0 to 2 inclusive.
- "leaf" means a node with no active children. Items only attach to leaves.
- "archetype" is the per-top-level behaviour flag (`unique`, `bulk`,
  `unique_variant`); see schema section.

## 1. Current state map

The fact-check pass behind every claim in this section: I read the files,
not just their docstrings.

### `TaxonomyNode` ORM model (`app/models.py`, lines 166-239)

Current columns:
- `id` PK
- `parent_id` (nullable self-FK to `taxonomy_nodes.id`, `ondelete="RESTRICT"`)
- `name` (String 255)
- `sort_order` (int, default 0)
- `archived_at` (nullable datetime, soft delete)
- `defaults_json` (nullable JSON; see 0015 migration below)
- `created_at`, `updated_at`

Indexes (`__table_args__`):
- `uq_taxonomy_top_name` partial unique on `(name)` where `parent_id IS NULL`.
- `uq_taxonomy_child_name` partial unique on `(parent_id, name)` where
  `parent_id IS NOT NULL`.
- `ix_taxonomy_nodes_parent_id`, `ix_taxonomy_nodes_archived_at`.

Important: the model **already** uses a single self-FK `parent_id` column to
encode the hierarchy. There is no separate "top-level" vs "sub-cat" column
shape. The two-level limit lives entirely in the application layer
(`_get_top_level_parent` in `app/taxonomy.py:330-348`). That is excellent
news: extending to three levels does not require restructuring rows; it
requires relaxing the depth guard, adding the archetype/prefix columns, and
adding a sub-sub-cat URL shape.

### Existing migrations relevant to taxonomy

- `migrations/versions/0005_create_taxonomy_nodes.py` — creates the table
  with the columns + partial unique indexes listed above. Both indexes
  already use `sqlite_where` + `postgresql_where`, so the partial-index
  cross-dialect pattern is established.
- `migrations/versions/0006_create_taxonomy_field_defs.py` — creates
  `taxonomy_field_defs` keyed by `node_id`; no per-leaf depth knowledge in
  the schema. Leaf invariant is purely route-layer.
- `migrations/versions/0007_create_items.py` — creates `items` with
  `taxonomy_node_id` FK to `taxonomy_nodes.id`. SKU column is `String(64)`,
  unique across active + archived rows.
- `migrations/versions/0015_taxonomy_defaults_json.py` — adds the
  `defaults_json` JSON column to `taxonomy_nodes`. Used by
  `app/taxonomy._coerce_defaults` (write side) and
  `app/items._apply_leaf_defaults` (read side, lines 472-499 of `items.py`).
  Stores a dict whose keys are a fixed set (`unit`, `tracking_mode`,
  `requires_checkout`, `reorder_threshold`, `reorder_qty`, `supplier_id`,
  `location_id`). Pre-fills the items create form. The per-category defaults
  the requirements mention are already shipped and we should keep using them
  verbatim.

### Existing taxonomy admin routes (`app/taxonomy.py`)

All gated by `require_role(Role.MANAGER)` (Admin always passes). Two URL
clusters share the `/admin/taxonomy` prefix:

Top-level (depth 0):
- `GET /admin/taxonomy` (list, lines 413-468)
- `GET /admin/taxonomy/new` (form, lines 476-499)
- `POST /admin/taxonomy` (create, lines 502-560)
- `GET /admin/taxonomy/{node_id}/edit` (form, lines 568-599)
- `POST /admin/taxonomy/{node_id}` (update, lines 602-666)
- `POST /admin/taxonomy/{node_id}/archive` (lines 674-705)
- `POST /admin/taxonomy/{node_id}/unarchive` (lines 708-740)

Sub-category (depth 1) routes:
- `GET /admin/taxonomy/{parent_id}/children` (list, lines 775-817)
- `GET /admin/taxonomy/{parent_id}/children/new` (form, lines 825-871)
- `POST /admin/taxonomy/{parent_id}/children` (create, lines 874-950)
- `GET /admin/taxonomy/sub/{node_id}/edit` (form, lines 958-989)
- `POST /admin/taxonomy/sub/{node_id}` (update, lines 992-1051)
- `POST /admin/taxonomy/sub/{node_id}/archive` (lines 1059-1086)
- `POST /admin/taxonomy/sub/{node_id}/unarchive` (lines 1089-1117)

`_get_top_level_parent` (lines 330-348) is the place the depth limit is
hard-coded: it 400s if the caller tries to create a child under a depth-1
node. That guard is the single thing that has to relax to permit depth 2.

Field defs live in a parallel module `app/field_defs.py` (same `/admin/taxonomy`
router prefix, separate file). `has_active_field_defs` (lines 241-248) gates
sub-cat creation so a leaf's schema is not orphaned.

### Existing taxonomy admin templates

- `app/templates/taxonomy_list.html` — top-level list with `?show=active|archived`
  tabs, CSV export link, per-row "Manage" (children) + "Fields" (when leaf) +
  "Edit" + "Archive". Uses `data-testid` attributes per row. Computes
  `leaf_ids` server-side and gates the per-row "Fields" link.
- `app/templates/taxonomy_children_list.html` — children-of-parent list.
  Same tab/CSV pattern. Per-row "Fields" + "Edit" + "Archive".
- `app/templates/taxonomy_form.html` — shared create/edit form for both
  top-level and sub-cat. Renders the "Defaults for new items" section
  (`taxonomy_defaults` testid) that maps directly to `defaults_json`.
- `app/templates/taxonomy_field_def_form.html`,
  `app/templates/taxonomy_field_defs_list.html`,
  `app/templates/taxonomy_field_def_options_partial.html` — field-def admin
  (not changing in this refinement except for the leaf rule, which already
  works regardless of depth).

### `Item` model + SKU generation

`Item` model (`app/models.py`, lines 358-455). Columns relevant to SKU:
- `sku` String(64), unique across active + archived rows
  (`uq_items_sku` index).
- `taxonomy_node_id` FK to `taxonomy_nodes.id`.
- `qr_code` partial-unique nullable.

SKU generation: `_generate_sku(db, leaf)` in `app/items.py` lines 501-530.
Format `<PREFIX>-<NNNN>`: prefix is the first 3 alphanumeric chars of the
leaf's name uppercased (or `ITM` fallback). 4-digit zero-padded `NNNN`.
Linear-scan to find the next free number. Called from `create_item` at
`app/items.py:1372` when the form submits no SKU. The SKU input field on
the create form is optional and the placeholder text describes the
auto-generation pattern (template `items_form.html` lines 38-49).

This is exactly the pre-existing seedbed for the new behaviour, but the
prefix derivation is implicit (derived ad-hoc per call from the leaf name)
rather than stored. The new schema makes the prefix authoritative + stored
on the node.

### New Item route + template

`app/items.py`:
- `GET /admin/items/new` (form, lines 1171-1209) — accepts `?node_id=` to
  pre-select.
- `GET /admin/items/_custom-fields` (HTMX fragment, lines 1212-1285) —
  served when category select changes. Returns the leaf's custom-field
  inputs and out-of-band swaps the core defaults (`?include_defaults=1` on
  create).
- `POST /admin/items` (create, lines 1288-1442) — re-renders on 400, runs
  `_generate_sku` if SKU left blank.
- `GET /admin/items/{id}/edit`, `POST /admin/items/{id}` — edit and update.

The category picker today is a `<select>` whose options come from
`_leaf_options` (`app/items.py:533-624`). It groups by parent for sub-cats
and shows top-levels as flat options. With three levels, the current shape
breaks down (a depth-1 container would need to render the same way a
depth-0 container currently does) and the picker is going to grow long.
The requirements call for a flat, leaf-only searchable picker showing
breadcrumbs — this matches what we need at three levels.

`app/templates/items_form.html`:
- Lines 24-90: Identity section with optional SKU input + Category select.
- Lines 60-90: Category `<select>` wires HTMX `hx-get` /
  `hx-trigger="change"` to `/admin/items/_custom-fields` to swap the
  custom-field block and (on create) OOB-swap core defaults.
- Lines 219-224: `<div id="cf-container">` houses the custom-field inputs.

### Existing per-category defaults storage

Already shipped via migration 0015 (`taxonomy_nodes.defaults_json`).
Write side: `app/taxonomy._coerce_defaults` (lines 110-212). Read side
on the items form: `_apply_leaf_defaults` (`app/items.py:472-499`). The
HTMX `_custom-fields` fragment at `app/items.py:1212-1285` round-trips
the OOB swap of those defaults when the user picks a category. **Do not
reinvent this surface; just keep using it.** The plan assumes the
defaults_json keys stay exactly as they are; the only new question is
whether to extend the dict's recognised key set, which I am answering "no"
for this slice.

### Existing `taxonomy_field_defs`

Model: `app/models.py` lines 260-340. Routes: `app/field_defs.py` (in the
same `/admin/taxonomy` router prefix). The leaf rule is enforced by
`_is_leaf` (`app/field_defs.py:234-238`): sub-cats are always leaves, and
top-level nodes are leaves iff they have no active children. With three
levels, "always-leaf" no longer holds for depth-1 nodes (a depth-1 node
could itself have depth-2 children). The plan generalises this to "no
active children" at any depth (see section 4).

### CSRF / template / role-gating conventions

- All HTML rendering goes through `templates` from `app/template_env.py`
  (per CLAUDE.md). Use the shared instance; do not construct another.
- Mutating requests carry `csrf_token` in form body OR `X-CSRF-Token`
  header AND matching `csrftoken` cookie. Forms include
  `<input type="hidden" name="csrf_token" value="{{ csrf_token }}">` (see
  `taxonomy_form.html` line 18).
- Role gating: `require_role(Role.MANAGER)` (Admin always passes; see
  `app/auth.py`). Taxonomy work is Manager-owned. New Item form is
  Manager-only for create; edit is Manager + Office; the picker fragment
  is Manager + Office + Workshop (permissive).
- Audit: every mutating route calls `record_audit(db, actor=..., action=...)`
  before commit. The audit-coverage forcing-function test
  (`tests/integration/test_audit_coverage.py`) source-greps for
  `record_audit(` on every mutating route. New routes either call it or
  add themselves to the documented exemption set.
- Flash: `request.session["flash"] = "..."` after POST; pops on next render.

## 2. Schema changes

All changes target `taxonomy_nodes`. The `items` table gains one optional
column to record which leaf-sequence number was assigned to a
unique-variant item. No new tables.

### `taxonomy_nodes` column additions

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `parent_id` | int self-FK (already exists) | yes | No change to the column. We re-use the existing self-FK for depth 0 / 1 / 2. |
| `archetype` | String(16), CHECK in app | yes at row level, NOT NULL at depth 0 | Top-level only. Values: `unique`, `bulk`, `unique_variant`. Inherited at read time by walking up to depth 0. |
| `sku_prefix` | String(8), NOT NULL after backfill | no | Stored uppercased; alnum-only (validator at write). |
| `next_sequence` | Integer NOT NULL default 1 | no | Used **only** when this node is a depth-2 leaf under a unique-variant archetype. On other nodes it is dead weight that defaults to 1 and is never read. Storing it on every row keeps the column type simple. |

Notes on `archetype` storage:
- Stored only at depth 0. Depth 1 + 2 rows leave `archetype IS NULL`.
  Application code resolves `effective_archetype(node)` by walking to the
  root via `parent_id`. This is the cheapest correct shape: no triggers,
  no copy-down maintenance, no risk of drift between parent + child.
- A SQL CHECK constraint `(parent_id IS NULL) = (archetype IS NOT NULL)`
  would enforce the rule at DB level, but SQLite's CHECK semantics + the
  Alembic `batch_alter_table` flow make this finicky. The plan keeps the
  invariant in the application layer (`_validate_archetype_placement`) so
  the migration stays portable and reversible.

Notes on `sku_prefix`:
- Backfilled by the migration (see section 3). Mandatory at write time
  going forward; uppercase, 1-8 alphanumeric chars. Reject blanks.
- Sibling uniqueness within a parent: a partial unique index
  `uq_taxonomy_sku_prefix_top` on `(sku_prefix)` where `parent_id IS NULL`
  and `uq_taxonomy_sku_prefix_child` on `(parent_id, sku_prefix)` where
  `parent_id IS NOT NULL`. Reasoning: an ancestor chain like
  `RTS-EM-001` must not be ambiguous, which means within one parent no two
  active-or-archived siblings can share a prefix. (Use partial indexes
  scoped across active + archived rows, same pattern as `uq_taxonomy_top_name`.)

Notes on `next_sequence`:
- Default 1 on insert. Only incremented by the SKU helper when allocating
  a sequence number on a unique-variant leaf. See section 4.

### `taxonomy_nodes` constraints / indexes added by the new migration

- `uq_taxonomy_sku_prefix_top` partial unique on `(sku_prefix)` where
  `parent_id IS NULL`.
- `uq_taxonomy_sku_prefix_child` partial unique on `(parent_id, sku_prefix)`
  where `parent_id IS NOT NULL`.
- No new FK or check constraints.

### `items` column additions

| Column | Type | Null | Notes |
| --- | --- | --- | --- |
| `assigned_sequence` | Integer | yes | Set only on unique-variant items. Stores the integer that was rendered as the last segment of the SKU at create time. NULL for `bulk` + `unique` archetype items. Audit + reporting can read this without re-parsing the SKU string. |

The composed SKU stays on `items.sku` (`String(64)`, unique). That is the
human-facing artefact and the audit/PO history keys on it. Storing the
sequence number alongside it makes the relationship to the leaf's
`next_sequence` round-trippable; without it, the migration backfill would
have no way to find the high-water mark per leaf.

I considered storing the segments separately (parent_prefix, sub_prefix,
leaf_prefix, sequence) but rejected it: the SKU is one string everywhere
else in the codebase (scan lookups in `app/scan.py:56`, PO lines, audit
diffs) and decomposing it everywhere would balloon the change set.

### Item table: anything else?

No. The leaf-FK already exists. `archived_at` already exists.
`tracking_mode` exists (`qty` / `unique`) but is now misleading: with
archetypes, the operational `tracking_mode` should be derived from the
effective archetype (`bulk` → `qty`, `unique` → `unique`, `unique_variant`
→ `unique`). The plan keeps the column as a stored field for v1 to avoid
breaking M-series / I3 / C-series code that reads it, but the New Item
route stops asking the user for it and just sets it from the archetype
(see section 4).

## 3. Migration strategy

Single new Alembic revision: `0016_taxonomy_archetype_and_prefix.py`,
`down_revision = "0015_taxonomy_defaults_json"`. Both directions must run
cleanly.

### `upgrade()` steps

1. `batch_alter_table("taxonomy_nodes")`:
   - Add `archetype` (String(16), nullable).
   - Add `sku_prefix` (String(8), **nullable for now** — backfilled in
     step 2, then tightened in step 3).
   - Add `next_sequence` (Integer, NOT NULL, server_default `1`).
2. Backfill all existing rows:
   - For every row, derive an `sku_prefix` from `name`:
     - Take the uppercased alphanumeric chars of `name`.
     - First try the first 3 alpha-only chars (skip digits); fall back to
       first 3 alphanumerics; fall back to `CAT` if the name yields none.
     - Truncate to length 8 (it will never reach 8 from a 3-char rule,
       but the column allows ad-hoc longer prefixes set later).
   - Disambiguate within siblings: scan in `(parent_id, id)` order; if a
     candidate prefix is already used by an active-or-archived sibling,
     append `2`, `3`, ... until unique. Examples:
     - `Raw Materials` → `RAW`. If a sibling is already `RAW`,
       `RAW2`, `RAW3`, ...
     - `12g Box` → first alpha-only attempt is empty after digits, so
       fall back to alphanumerics: `12G`. (Stays under 8 chars.)
   - Set `archetype = 'bulk'` for every top-level row. Leave depth-1 rows
     with `archetype IS NULL`. (The migration cannot guess whether a
     category should be `unique` or `unique_variant` from history; `bulk`
     is the safe default that matches existing items' `tracking_mode`
     behaviour and the MISSION-§3 seed taxonomy.)
   - Leave `next_sequence` at the server default `1`.
3. Tighten `sku_prefix` to NOT NULL (`batch_alter_table` again).
4. Backfill `items.assigned_sequence`:
   - Add the column nullable.
   - Leave NULL for every existing item. (No existing item is a
     `unique_variant` archetype after step 2, so this column is genuinely
     N/A everywhere. If the production DB has existing items that need to
     become unique-variant later, the manager re-creates them; see "Risks
     & open questions".)
5. Create the partial unique indexes:
   - `uq_taxonomy_sku_prefix_top` on `(sku_prefix)` where
     `parent_id IS NULL`.
   - `uq_taxonomy_sku_prefix_child` on `(parent_id, sku_prefix)` where
     `parent_id IS NOT NULL`.
6. Seed `next_sequence` from existing item SKUs **per leaf**, so future
   numbering does not collide. For each leaf with existing items:
   - Find every item whose SKU starts with `<sku_prefix>-` (after backfill
     step 2 makes the prefix authoritative).
   - Parse the suffix as `int` where possible (skip non-numeric trailing
     segments — current items use `<PREFIX>-<NNNN>` so they parse cleanly).
   - Set `leaf.next_sequence = max(parsed) + 1`. Nodes without matching
     items stay at `1`.
   - For a depth-0 node that is currently a leaf (no active children), the
     items live there and the seed runs there. For depth-1 leaves, items
     live on the sub-cat, so the seed runs on the sub-cat. This handles
     today's mixed shape without special-casing.

### `downgrade()` steps

1. Drop both partial unique indexes.
2. `batch_alter_table("items")` drop `assigned_sequence`.
3. `batch_alter_table("taxonomy_nodes")`:
   - Drop `next_sequence`.
   - Drop `sku_prefix`.
   - Drop `archetype`.

No data loss: every column added is additive, every backfill is computable
from columns that already exist + new columns; the inverse is "drop the
new columns".

### What happens to the existing 2-level structure

Nothing structural. All existing top-level nodes stay top-level
(`parent_id IS NULL`) with new `archetype='bulk'` + derived `sku_prefix`.
All existing sub-cats stay depth-1 (`parent_id` points to a top-level
node) with `archetype IS NULL` (inherited from parent at read time) +
derived `sku_prefix`. No row moves between depths.

After the migration, depth-2 sub-sub-cats become legal but none exist
yet. The Manager creates them via the new route shape (section 4).

### Forward / backward verifiability

The migration must be tested by:
- Running `alembic upgrade head` on a fresh DB and confirming every row
  has the new columns populated with valid values.
- Running it on a DB seeded with the demo data
  (`scripts/seed_demo_data.py`) and confirming SKUs still resolve, items
  list view still renders, and the `next_sequence` per leaf matches the
  max numeric suffix of existing items.
- Running `alembic downgrade -1` and confirming all three columns are
  gone, indexes are gone, and the app still imports + the items list
  still renders (against the pre-0016 model).

## 4. Backend changes

This section names the helpers, route changes, and validation rules.
Specific file targets:
- `app/models.py` — add columns to `TaxonomyNode` + `Item`.
- `app/taxonomy.py` — three-level depth support, archetype handling,
  prefix validation, archetype-lock-after-items.
- `app/items.py` — leaf-only picker, server-side SKU compose, archetype
  validation.
- `app/sku.py` (new module, small) — `compose_sku`, `next_sku`. Kept out
  of `app/items.py` so the helpers are reusable from `app/scan.py` /
  audit code without circular imports.

### New module: `app/sku.py`

Public surface:

```python
def effective_archetype(db: Session, node: TaxonomyNode) -> Archetype | None:
    """Walk parent_id to depth 0; return that row's archetype.

    Returns None only for an orphaned tree (defensive). Depth 0 nodes
    return their own archetype.
    """

def ancestor_chain(db: Session, node: TaxonomyNode) -> list[TaxonomyNode]:
    """Return [root, ..., node] in top-down order. Length 1 to 3."""

def compose_sku(db: Session, leaf: TaxonomyNode, sequence: int) -> str:
    """Concatenate ancestor sku_prefixes + zero-padded sequence.

    sequence is 1-indexed and rendered as 3 digits (zero-pad). Example:
    leaf chain is RTS / EM (depth 2 = leaf), sequence = 7 → "RTS-EM-007".
    For depth-1 leaf: "RAW-ABC-007". For depth-0 leaf: "TOOL-007".
    """

def next_sku(db: Session, leaf: TaxonomyNode) -> tuple[str, int]:
    """Atomically allocate the next sequence on this leaf and return the
    composed SKU + the integer assigned.

    Locking strategy (cross-dialect):
      Postgres: SELECT ... FROM taxonomy_nodes WHERE id = :id FOR UPDATE,
                then UPDATE next_sequence = next_sequence + 1 in the same
                transaction. The row lock blocks concurrent allocators on
                the same leaf.
      SQLite:   SQLAlchemy's default isolation under sqlite3 driver is
                "deferred"; combined with a single "UPDATE ... RETURNING"
                statement (SQLite 3.35+) this is atomic at the row level.
                For older SQLite, fall back to:
                  BEGIN IMMEDIATE; UPDATE ... ; SELECT next_sequence;
                  COMMIT.
                pytest runs are single-threaded so contention is not a
                test concern, but we still want the same code path.

      Implementation: use SQLAlchemy's UPDATE...RETURNING construct
      (`sqlalchemy.update().returning(...)`) which is supported on
      both dialects from the versions we target (sqlite >= 3.35, psycopg
      >= 3.x against pg 13+). On dialects without RETURNING, a two-step
      "lock then update" path is the fallback. We do not advertise a
      sub-millisecond budget here; the leaf-row UPDATE plus a row read is
      well under the 500ms scan-to-recorded budget in MISSION §4.

    Raises RuntimeError if the leaf is not a unique-variant leaf at
    depth 2 (caller must gate). The function does not validate archetype;
    that is the route handler's job.
    """
```

Concurrency notes for `next_sku`:
- The hot path is "one UPDATE returning next_sequence". The lock scope is
  one row in `taxonomy_nodes`; two concurrent create-item requests on the
  **same** leaf serialise on it, which is exactly the contract we want.
- Two concurrent requests on **different** leaves do not contend.
- The unique index on `items.sku` is the belt-and-braces: if the helper
  ever returns a duplicate (e.g. someone hand-edited the column), the
  insert fails with `IntegrityError`. The route maps that to a 500 with a
  clear log line.

### `app/models.py` changes

- Add `Archetype` StrEnum with three values.
- `TaxonomyNode`: add `archetype: Mapped[Archetype | None]`, `sku_prefix:
  Mapped[str]` (NOT NULL), `next_sequence: Mapped[int]` (default 1).
- `Item`: add `assigned_sequence: Mapped[int | None]`.

### `app/taxonomy.py` changes

`_get_top_level_parent` (lines 330-348) **renames + relaxes** to
`_get_parent_node` and accepts any non-leaf-occupied node up to depth 1:

```
- A parent at depth 0 or 1 can have children.
- A parent at depth 2 cannot (depth limit).
- If the parent has items attached (Item.taxonomy_node_id == parent.id
  with archived_at IS NULL), reject — it is a leaf-with-items, you
  cannot un-leaf it. Same posture as the existing
  "field defs present → reject sub-cat" rule (which stays).
```

Two new helpers in `app/taxonomy.py`:

```python
def _node_depth(db, node) -> int:
    """0 for top-level, 1 for sub-cat, 2 for sub-sub-cat."""

def _has_items(db, node_id) -> bool:
    """True if any non-archived item references this node id."""
```

Form changes:
- Top-level create form (`new_taxonomy_form`, `create_taxonomy`) gains:
  - `archetype` select (required): `unique`, `bulk`, `unique_variant`.
  - `sku_prefix` text input (required, 1-8 alnum chars, server uppercases).
- Sub-cat create form (`new_sub_category_form`, `create_sub_category`)
  gains `sku_prefix` (required). Archetype is hidden (inherited).
- New routes for depth 2:
  - `GET /admin/taxonomy/{parent_id}/sub/{sub_id}/grandchildren` (list)
  - `GET /admin/taxonomy/{parent_id}/sub/{sub_id}/grandchildren/new` (form)
  - `POST /admin/taxonomy/{parent_id}/sub/{sub_id}/grandchildren` (create)
  Decision: I went with a 2-segment URL (`/sub/{sub_id}/grandchildren`)
  rather than a generic `/admin/taxonomy/{node_id}/children`. The current
  routes already split top-level vs sub-cat into distinct shapes for the
  same "where am I in the URL parent vs DB parent" reason called out in
  the `app/taxonomy.py` module docstring. The depth-2 list/new lives
  under the depth-1 parent's URL; depth-2 edit/archive use the existing
  flat `/admin/taxonomy/sub/{node_id}/...` shape (which already 404s on
  top-level ids and now keeps working at depth 2 because the only thing
  that differs is whether the row has a parent — and `_get_sub_category`
  at lines 351-359 already accepts any non-top-level node).
- Edit form for top-level shows archetype **as read-only with a lock
  badge** once any descendant leaf has an item. Compute the lock state
  by SQL: `EXISTS(SELECT 1 FROM items WHERE taxonomy_node_id IN
  (descendant_ids))`. Reject any inbound archetype change with a 400 if
  locked.

`_check_top_name_unique` + `_check_child_name_unique` stay. Add the
analogous `_check_sku_prefix_unique` checks (top + child scope).

Archetype enforcement on `create_sub_category` + `create_grandchild`:
- Always derive archetype from the ancestor chain. Reject any inbound
  `archetype` form field on non-top-level routes (defence-in-depth).
- For a unique-variant tree, allow creating depth-1 + depth-2 nodes
  freely. The "must be depth 2" rule only applies to **items**, not to
  the taxonomy structure (a unique-variant tree of "RTS Rings / Emma"
  can validly have no items until the manager wants to mint one).

Container-or-leaf invariant: covered today by the field-def + sub-cat
guards. Extended now to:
- Cannot create a child under a node that has any non-archived items.
- Cannot create an item under a node that has any active children.

Last detail: `_LIST_ORDER`, `_csv_rows_for_taxonomy`, +
`_csv_rows_for_sub_categories` all gain a `sku_prefix` column (and
optionally `archetype` on the top-level CSV). Existing CSV consumers
gain trailing columns; nothing renames.

### `app/items.py` changes

New helper:

```python
def _leaf_breadcrumb(db, leaf) -> str:
    """Return e.g. "Raw Materials / Silver / 925" for a depth-2 leaf,
    "Raw Materials / Silver" for depth-1, "Tools" for depth-0."""

def _leaf_picker_options(db, *, current_id=None) -> list[dict]:
    """Replacement for _leaf_options: flat, leaf-only, breadcrumb labels.

    Output rows: {id, label, archetype, sku_prefix, breadcrumb}. The
    template uses ``breadcrumb`` for display + filtering. Archived leaves
    are excluded unless ``current_id`` references one (same archived-FK-
    preservation rule the existing _leaf_options has)."""
```

`_resolve_leaf_node` (lines 157-203) keeps its current contract but its
"node has sub-categories" rejection text generalises ("category has
sub-categories at any depth").

`create_item` (`POST /admin/items`, lines 1288-1442) new rules:
- Look up the leaf, walk ancestors, resolve `effective_archetype`.
- For `unique_variant` archetype:
  - Reject the request if `_node_depth(leaf) != 2` (with a clear 400:
    "Unique-variant items require a 3-level path; pick a depth-2 leaf").
  - Ignore any inbound `sku` form field; allocate via `next_sku(db,
    leaf)` and set both `item.sku` + `item.assigned_sequence`.
  - Force `tracking_mode = TrackingMode.UNIQUE`.
- For `unique` archetype:
  - Compose SKU from the ancestor prefixes + a sequence (use the leaf's
    `next_sequence` the same way). Set `tracking_mode = UNIQUE`.
  - `assigned_sequence` set on the row (handy for audit).
- For `bulk` archetype:
  - Compose SKU from ancestor prefixes + sequence. `tracking_mode = QTY`.
  - `assigned_sequence` set.

In all three cases the server now **owns** SKU generation. The form's
optional SKU input goes away from the create UI (it stays as a hidden
read-only display on edit, because SKUs are immutable today; see
`items_form.html` lines 28-50). Existing power-user "I'll pass my own
SKU" tests need updating; the route's contract is now "client-supplied
SKU on create is ignored".

`update_item` SKU handling: unchanged. SKU is read-only on edit
templates already (line 28-36 of `items_form.html`). The hidden POST
field preserves the current value, and `_normalise` validates it through
`_check_sku_unique(exclude_id=item.id)`. No change.

The HTMX `_custom-fields` fragment (`app/items.py:1212-1285`) gains a
preview block: when called with `include_defaults=1` (the create form
flag), it ALSO renders an OOB swap into a new `#sku-preview` element
showing either:
- "Next SKU: RTS-EM-007" for unique-variant on a depth-2 leaf;
- "Next SKU: RAW-ABC-008" for bulk / unique on any-depth leaf;
- nothing if the picked node is not a leaf.

This lets the user see what the server will allocate without committing.
The preview is informational only; the actual allocation happens at POST
time inside the `next_sku` transaction. (A user opening two tabs sees
the same preview number; the second commit gets the next-after-it; the
preview was a guess and we say so in the template helper text.)

### Per-archetype validation summary

| Archetype | Item depth allowed | SKU shape | tracking_mode forced |
| --- | --- | --- | --- |
| `bulk` | leaf at any depth 0-2 | `<root>-[<sub>-[<leaf>-]]<NNN>` | `qty` |
| `unique` | leaf at any depth 0-2 | same shape | `unique` |
| `unique_variant` | leaf at depth 2 only | `<root>-<sub>-<NNN>` (3 segments) | `unique` |

For `unique_variant` the leaf's own `sku_prefix` is part of the chain
(`RTS-EM`), but the final segment is the **sequence**, not the leaf's
prefix — which matches the requirement example "RTS Rings → Emma → 001".
In other words a `unique_variant` tree always has exactly three segments
in the SKU: `<top.sku_prefix>-<sub.sku_prefix>-<NNN>`. The leaf's own
`sku_prefix` is required by the schema but functions only as a name +
URL slug — the SKU does not include it. Decision rationale:
the spec example shows three segments and zero-padded sequence at the
leaf, and including a fourth segment for the leaf prefix would produce
`RTS-EM-NNN-001` which contradicts the spec.

For `bulk` and `unique` archetypes the leaf's own `sku_prefix` IS part of
the chain. Example: a depth-2 leaf called "925 Sterling" under
"Raw Materials / Silver" produces `RAW-SIL-925-008`. The first item on a
freshly-created depth-0 `bulk` leaf produces `TOOL-001` (depth-0
single-segment is legal because `bulk` permits any depth).

This is the most surprising decision in the plan. Documented in the
"Risks & open questions" section so a reviewer can object before code
ships.

## 5. Frontend changes

Templates touched: `taxonomy_list.html`, `taxonomy_children_list.html`,
`taxonomy_form.html`, `items_form.html`, plus one new
`taxonomy_grandchildren_list.html`. Plus a small new `app/static`
contribution: a vanilla-JS combobox helper for the leaf picker (50-80
lines, no framework — see "leaf picker" below).

### `taxonomy_list.html` (depth 0)

- Header column "SKU prefix" inserted after Order.
- Header column "Archetype" inserted after Name.
- Per-row, render the prefix as a code-styled badge and the archetype as
  a pill (`bulk` / `unique` / `unique_variant`).
- Existing "Manage" + "Fields" actions stay. The "Fields" link shows only
  when the node is currently a leaf (already computed as `leaf_ids` in
  `app/taxonomy.py:447-458`; logic generalises to "any node with no
  active children" which is what it does today).
- New action: "Add sub-category" (already exists in the children-list
  page; we add a shortcut here too for the depth-0-is-currently-leaf
  case, but I am leaving that as a non-essential nicety — the current
  "Manage" link does the same job).
- The form for top-level create gains the archetype select + sku_prefix
  input; see below.

### `taxonomy_children_list.html` (depth 1)

- Same SKU-prefix column added.
- Per-row "Manage" action goes to the new
  `/admin/taxonomy/{parent_id}/sub/{node_id}/grandchildren` URL.
- "Fields" action stays (leaf rule already covers depth-1 leaves).
- Page header gains an "Archetype: <value> (inherited)" line so the
  Manager understands the depth-2 constraint they are about to inherit.

### New `taxonomy_grandchildren_list.html` (depth 2)

- Copy of `taxonomy_children_list.html` shape, scoped to a depth-1
  parent. Top-of-page breadcrumb "← Back to <root> / <sub>".
- No further "Manage" action (depth 2 cannot have children).
- "Add item" action becomes the prominent CTA for a `unique_variant`
  tree — the typical use case here is "mint another Emma ring".
- Each row shows sku_prefix.

### `taxonomy_form.html`

Add two sections above the "Defaults for new items" section:

**Identity** (always shown):
- Existing Name input.
- New `sku_prefix` input (required, 1-8 chars, uppercased on the server,
  alnum only). Helper text: "Used to build item SKUs. Cannot be changed
  after items are created." Reject change at write time if any descendant
  has an item (server returns 400 with that helper text).
- Existing Sort order input.

**Archetype** (shown only on the depth-0 create form, hidden on depth-1
+ depth-2 create + on edit):
- `<select name="archetype" required>` with the three options + helper
  text describing each ("Unique: one-of-a-kind, leaf-only depth 1 to 3";
  "Bulk: quantity-tracked, has reorder thresholds"; "Unique-variant:
  design family with auto-numbered pieces; requires 3-level path").
- On edit, if `_has_descendant_items()` is true, render the archetype as
  a read-only label with a lock icon and helper text "Locked: items
  exist under this category." The route rejects any change.

**Defaults for new items** (unchanged from today).

The Action button label updates to reflect depth ("Create top-level
category" / "Create sub-category" / "Create sub-sub-category" /
"Save changes").

### `items_form.html`: leaf-only searchable picker

Replace the current `<select id="taxonomy_node_id">` (lines 60-90) with
a composite:

```html
<div class="uc-field" data-testid="item-category-picker">
  <label for="taxonomy_node_id">Category *</label>
  <input type="hidden" name="taxonomy_node_id" id="taxonomy_node_id"
         value="{{ form.taxonomy_node_id }}">
  <input type="text" id="taxonomy_node_search"
         class="uc-input"
         placeholder="Search categories...">
  <ul id="taxonomy_node_results" role="listbox"
      hx-get="/admin/items/_category-search"
      hx-trigger="keyup changed delay:150ms from:#taxonomy_node_search"
      hx-include="#taxonomy_node_search"
      hx-target="#taxonomy_node_results"
      hx-swap="innerHTML">
    {% include "items_category_options_partial.html" %}
  </ul>
  <output id="sku-preview" data-testid="sku-preview"
          hx-trigger="change from:#taxonomy_node_id">
    {# Populated by /admin/items/_custom-fields OOB swap on category change #}
  </output>
</div>
```

The HTMX flow:
1. User types into `#taxonomy_node_search` → fragment route
   `/admin/items/_category-search?q=...` returns the filtered list of
   leaf options (capped at 20). Each option carries `data-id` +
   breadcrumb.
2. User clicks a result → small inline JS sets the hidden
   `#taxonomy_node_id` value + dispatches `change`, which triggers the
   existing `/admin/items/_custom-fields` HTMX chain.
3. `_custom-fields` (with `include_defaults=1`) is extended to OOB-swap
   `#sku-preview` with the computed next SKU.

The inline JS is small (about 30 lines, vanilla, no build step) and goes
in `app/static/js/category-picker.js`. Keyboard navigation: arrow keys +
enter to select; Escape clears. Accessibility: ARIA `role="combobox"` +
`aria-autocomplete="list"` on the input.

New route `GET /admin/items/_category-search` (Manager + Office +
Workshop, same gating as `_custom-fields`):

```python
@router.get("/_category-search", response_class=HTMLResponse)
def category_search(
    request: Request,
    q: str = "",
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Return up to 20 leaf-node options matching the breadcrumb query.

    Matches breadcrumb chars case-insensitively. Empty query returns the
    first 20 leaves in sort order. Leaves only (Item.taxonomy_node_id
    must point at one of these). Archived nodes excluded.
    """
```

Template: `items_category_options_partial.html` renders the `<li>`
options. Each `<li>` has `data-id` + `data-breadcrumb`.

### What goes away from `items_form.html`

- The optional SKU input on create (lines 38-49) is removed. Replaced
  with a small `<output id="sku-preview">` showing the server-computed
  next SKU. On edit, the SKU stays read-only (existing behaviour, lines
  28-36).
- The `<select>` + groupings logic for the category picker (lines
  60-89) is removed; the picker above replaces it.
- The Stock-behaviour section's Tracking-mode select is **kept** on edit
  but **hidden on create** (the value is forced from the archetype).
  Reason: stock-behaviour data on existing items has to stay editable
  for Office to be able to fix mistakes; we just don't surface the
  choice in the new-item flow.

### Australian English

I scanned `app/templates/*.html` for the obvious US spellings (color,
gray, license, organize, organization, behavior, customize, catalog).
The codebase already uses "colour" + "behaviour" (see
`items_form.html` line 95 "Stock behaviour"). The new copy must follow
suit:
- "Sub-category" not "Subcategory".
- "Sub-sub-category" rather than "child-of-child". (Yes, ugly. But
  consistent with the project's existing voice.)
- "Archive" not "Trash" / "Delete".
- "Sort order" stays.
- Helper text for the archetype options uses "behaviour" + "one-of-a-kind"
  + "auto-numbered" (no "auto-generated"; the existing items form uses
  "auto-generate" once, which is fine).

No em dashes in new copy. The existing templates use em dashes liberally
in helper text and headings; that is a separate clean-up not in this
slice's scope.

## 6. Test coverage targets

Add or extend these files. The list is concrete; downstream agents wire
the actual assertions.

### Unit tests

- `tests/unit/test_sku.py` (new file)
  - `compose_sku_depth_0()` — single segment.
  - `compose_sku_depth_1()` — two segments.
  - `compose_sku_depth_2()` — three segments.
  - `compose_sku_unique_variant_uses_root_and_sub_only()` — confirms the
    "three segments end in sequence, leaf prefix omitted" decision.
  - `compose_sku_zero_pads_to_three_digits()` — `1 → 001`, `42 → 042`,
    `1000 → 1000` (no truncate).
  - `next_sku_increments_per_leaf()` — two sequential calls on the same
    leaf return n, n+1. Two calls on different leaves do not collide.
  - `next_sku_concurrent_create()` — start two transactions, each calling
    `next_sku` on the same leaf, commit in order, assert the second
    receives a strictly greater integer (and that the first's row update
    blocked the second).
  - `effective_archetype_walks_to_root()` — depth-2 leaf returns the
    depth-0 ancestor's archetype.

- `tests/unit/test_taxonomy.py` (extend)
  - Existing tests for the `_coerce_defaults` helper stay green.
  - `_node_depth()` returns 0 / 1 / 2 correctly; rejects orphans.
  - `_has_descendant_items()` walks down through both child levels.

### Migration tests

- `tests/integration/test_migration_0016.py` (new file)
  - Seed pre-0016 fixture data: a top-level + a sub-cat + an item on
    each leaf. Upgrade to 0016, assert:
    - Every row has `sku_prefix` populated, uppercase, alnum-only.
    - Sibling prefixes collide-and-disambiguate (`RAW`, `RAW2`).
    - Every top-level row has `archetype='bulk'`.
    - Every sub-cat has `archetype IS NULL`.
    - `next_sequence` on each leaf equals `max(item.sku numeric suffix)
      + 1`.
  - Downgrade from 0016, assert the three new columns + the two new
    indexes are gone.
  - Existing items keep their SKUs (no rewrite during migration).

### Integration tests

- `tests/integration/test_taxonomy_routes.py` (extend)
  - Manager creating a top-level node: `archetype` + `sku_prefix`
    accepted, missing prefix → 400, prefix-too-long → 400, prefix-with-
    non-alnum → 400.
  - Manager creating a sub-cat under a `unique_variant` top-level:
    archetype is inherited; do not need to specify; passing one is
    silently ignored.
  - Manager creating a sub-sub-cat under a sub-cat: depth 2 reached,
    further child creates 400 "depth limit reached".
  - Sibling prefix uniqueness: two children with same `sku_prefix` under
    same parent → 400 from the route guard, plus the partial unique
    index would catch a hand-rolled INSERT.
  - Lock archetype after items: create a top-level + leaf + item, then
    attempt to change archetype on the top-level → 400 "category has
    items".
  - Lock sku_prefix after items: same shape, attempt to change prefix
    → 400 "items already created".
  - Cannot add a sub-category under a node with active items → 400.

- `tests/integration/test_items_routes.py` (extend)
  - Create a `bulk` item on a depth-0 leaf: SKU is `<prefix>-001`,
    `assigned_sequence=1`, tracking_mode=qty.
  - Create a `bulk` item on a depth-2 leaf: SKU has three segments + 3-
    digit sequence.
  - Create a `unique_variant` item on a depth-2 leaf: succeeds, returns
    SKU shaped `<root>-<sub>-001`, `assigned_sequence=1`,
    tracking_mode=unique.
  - Create a `unique_variant` item on a depth-1 leaf → 400 "unique-
    variant items require a 3-level path".
  - Create a `unique_variant` item on a depth-0 leaf → 400 (same).
  - Client-supplied `sku` form field on a `unique_variant` create is
    **ignored**; server allocates anyway. Test the response SKU does not
    equal the supplied value.
  - Two sequential creates on the same `unique_variant` leaf: SKUs end
    in `001` then `002`. `taxonomy_nodes.next_sequence` = 3.
  - Leaf-only validation: trying to submit a category id that has active
    children → 400 with the existing "pick one of its sub-categories"
    message generalised.
  - SKU preview HTMX fragment route: GET
    `/admin/items/_custom-fields?taxonomy_node_id=<leaf_id>&include_defaults=1`
    returns a response containing `Next SKU:` and a syntactically valid
    SKU.
  - Category search fragment route: `GET /admin/items/_category-search?q=`
    returns up to 20 leaves; `?q=ring` filters to matching breadcrumbs.

- `tests/integration/test_audit_coverage.py` — the source-text sweep
  picks up the new routes automatically as long as they call
  `record_audit(`. New mutating routes: `POST` on grandchildren list +
  the new category-search fragment is GET (no audit needed). No action
  required beyond ensuring `record_audit(` appears in the new POST.

### E2E (Playwright)

- `tests/e2e/test_taxonomy_e2e.py` (extend) — walk: create top-level
  with archetype `unique_variant` + prefix `RTS`, create sub-cat
  `Emma` + prefix `EM`, navigate to grandchildren list, see depth-2
  page render with breadcrumb.
- `tests/e2e/test_items_e2e.py` (extend) — walk: open New Item form,
  type "emma" in the picker, click the `RTS / Emma` result, see SKU
  preview "Next SKU: RTS-EM-001", submit, land on items list with the
  new SKU visible.

## 7. Risks & open questions

Listed in rough order of "most likely to bite".

### R1. The unique-variant SKU shape decision

The spec example "RTS Rings → Emma → 001" shows three SKU segments. The
plan honours that by treating the depth-2 leaf's own `sku_prefix` as
**name-only** (it appears in URLs + breadcrumbs but not in the SKU). For
`bulk` / `unique` archetypes, the leaf's prefix IS in the SKU. This
asymmetry is genuinely confusing.

Alternatives considered + rejected:
- Drop `sku_prefix` from depth-2 leaves entirely. Rejected: forces a
  conditional NOT-NULL constraint (NOT NULL except when parent's
  archetype is `unique_variant` AND depth = 2), which is messy.
- Force `unique_variant` depth-2 leaf prefix == leaf name. Rejected:
  loses the human-friendly short prefix.
- Always include the leaf prefix in the SKU regardless of archetype.
  Rejected: contradicts the spec example.

Final call: keep the column always-NOT-NULL, document the asymmetry in
the `compose_sku` docstring, mirror it in the SKU preview. If the
business team objects on review, switching to "include leaf prefix in
SKU" is a one-line change inside `compose_sku`.

### R2. Existing items already in the production DB

CHANGELOG.md shows the project is feature-complete through DoD #12 (CI1
+ P4 + DOC9 + OAUTH3 shipped). MISSION §3 seeds first-run taxonomy with
"Raw Materials, Consumables, Tools, Wax Injection Moulds, each with no
sub-categories and no custom fields" — but I cannot find a seed step in
`app/main.py` or `app/db.py`. The only seeder is
`scripts/seed_demo_data.py` (run-by-hand). So the production DB might
have:
- No taxonomy + no items at all (fresh deploy on Fly.io).
- A manager-curated taxonomy + items created manually.
- The demo seed data.

The migration must handle all three. The "default to bulk + derive
prefix + seed next_sequence from existing SKUs" backfill handles all
three correctly because every existing item came through `_generate_sku`,
which already produces `<PREFIX>-<NNNN>`. **Action: the downstream agent
should run the migration once against a fresh DB, then once against a
DB seeded by `scripts/seed_demo_data.py`, before declaring it green.**

### R3. The seeded MISSION-§3 first-run taxonomy is missing

MISSION §3 mandates "Seed taxonomy on first run: Raw Materials,
Consumables, Tools, Wax Injection Moulds". There is no such seed in
`app/main.py`. Either:
- The shipped CHANGELOG-completed product silently dropped that
  requirement, or
- The user does it manually.

For this refinement: I am **not** adding the missing seed (it's not in
the requirements). But I flag it: anyone running `make dev` against an
empty DB sees an empty taxonomy admin. Downstream agents in this
4-agent plan should not address this — it's a separate ticket.

### R4. `taxonomy_field_defs` leaf rule at three levels

`app/field_defs.py:234-238` defines `_is_leaf` as "sub-cats are always
leaves; top-level nodes are leaves iff no active children". At three
levels, a depth-1 sub-cat is no longer "always a leaf" — it could have
depth-2 children. The generalisation is straightforward: "leaf iff no
active children, regardless of depth". The downstream agent should
update both `app/field_defs._is_leaf` AND `app/items._is_leaf` (they are
deliberate duplicates per the docstring at lines 144-154 of
`app/items.py`). The `has_active_field_defs` gate in
`app/taxonomy.create_sub_category` already works correctly under the
new shape because it queries `taxonomy_field_defs.node_id == parent.id`
not `depth`-aware.

### R5. The `defaults_json` keys do not include `sku_prefix` or `archetype`

By design. `defaults_json` is "defaults applied to new items on this
leaf". `sku_prefix` + `archetype` are taxonomy structural columns, not
item defaults. They never appear in the items form, so they have no
business in `defaults_json`. The plan keeps the dict's recognised keys
exactly as today (`_DEFAULT_KEYS` in `app/taxonomy.py:76-84`).

### R6. Tracking mode is now derived but still stored

`Item.tracking_mode` (`models.py:403-413`) is a stored column read by
`item_units`, `checkouts`, `stock_takes`, the items list filter, scan
routes, M-series movements, and the items form. Auto-deriving it from
archetype at create time + leaving the column writable on edit is a
hybrid. I prefer this over making it a computed property (which would
require migrations across every caller). The downside: an Office user
who edits a `bulk` item and accidentally flips `tracking_mode` to
`unique` can land an inconsistent row. The plan does not fix this — it
is out of scope. A future slice could either:
- Lock the field on edit too (forcing manager-only changes), or
- Derive it on the fly from `effective_archetype` and drop the column.

### R7. Concurrent allocation under SQLite

The MISSION's note "SQLite for local/test, Postgres for cloud" makes
SQLite a real concurrency surface for tests. The plan uses
UPDATE...RETURNING for `next_sku`. SQLite 3.35+ supports this; pytest
in-memory engines should be fine. If the CI image ships an older SQLite
(check `python -c "import sqlite3; print(sqlite3.sqlite_version)"` in
CI before merging) the fallback path (BEGIN IMMEDIATE) is required.
**Action for downstream agent: confirm sqlite version in CI before
relying on UPDATE...RETURNING.**

### R8. URL shape for depth 2 grandchildren

I chose `/admin/taxonomy/{parent_id}/sub/{sub_id}/grandchildren` over a
flatter `/admin/taxonomy/{node_id}/children` because the current code
already encodes the parent's id in the URL for the children list and
the docstring at `app/taxonomy.py:13-23` explicitly documents the "URL
parent / DB parent mismatch" concern that made the flat
`/sub/{node_id}/...` shape exist for edit/archive routes. Putting the
parent + grandparent in the URL keeps the breadcrumb obvious and stops
a hand-edited URL from drifting from the DB shape. Open question for
the implementer: if this URL feels too clunky, an acceptable alternative
is `/admin/taxonomy/{node_id}/children` (flat, polymorphic on depth)
— but then the depth-2 form has to compute breadcrumbs at render time
rather than read them from the URL. Either works. I'll defer the choice
to whoever implements, with a slight preference for the explicit shape.

### R9. Australian English spot-check on existing UI copy

Quick scan: I found no obvious US spellings in the templates that
matter. `items_form.html` uses "behaviour"; `taxonomy_form.html` uses
"Categories". `_components.html` line 50 uses "coloured" already. The
existing copy uses em dashes heavily; the new copy must not, per
instructions. Nothing else to flag.

### R10. The HTMX SKU-preview is a guess, not a reservation

The preview at category-pick time is computed by reading
`taxonomy_nodes.next_sequence` without locking. If the user keeps the
form open while another manager creates an item on the same leaf, the
preview becomes stale. The committed SKU is always allocated atomically
at POST time, so there is no correctness bug — only a display lag.
Plan: render the preview with `output` element + helper text "Server
allocates the actual SKU at save time; this is the current next value."
A tighter UX (reservation tokens that release after N minutes) is
deliberately out of scope.

### R11. Two competing "leaf" definitions in `app/items.py` and `app/field_defs.py`

Mentioned at R4. The duplicate is documented in code (it is on
purpose). The plan extends both in lockstep; the duplication is
acceptable noise for v1.

### R12. The existing field-def "node has active field defs blocks sub-cat add" rule

Still required at three levels. With three levels the rule is "if this
node has active field defs, you cannot add children at any depth". The
existing `has_active_field_defs` gate already enforces this per-node,
so the generalisation is automatic.

### R13. Out-of-scope items called out by the user

The user's brief explicitly excludes: stock takes, POs, checkouts, audit
screens, CSV bulk import, archetype editing post-items. The plan
respects this. The audit code DOES write `taxonomy_node.created` /
`updated` / `archived` / `unarchived` audit rows for the new routes
because the audit-coverage forcing-function test mandates it — this is
mechanical, not user-facing.

### R14. Demo seed data needs an update

`scripts/seed_demo_data.py` (lines 60-95) constructs the demo taxonomy
without `archetype` or `sku_prefix`. After this change it must:
- Pass `archetype="bulk"` on every top-level construction.
- Pass an explicit `sku_prefix` per node (or rely on a one-shot
  post-insert backfill helper inside the seed script).

This is a non-blocking follow-up — the seed script is dev-only and not
under `make check`. The plan flags it; downstream agents should fix it
in the same slice if practical, otherwise leave a `scripts/seed_demo_data.py`
TODO comment and move on.

### R15. Single migration vs. split

I considered splitting the migration into (a) add nullable columns +
backfill, and (b) tighten NOT NULL + add indexes. Single migration is
fine for a project without an in-flight production DB; if someone
discovers there IS a production database mid-migration, the split form
is the safer pattern (deploy v1, run migration, deploy v2 with the
NOT NULL + indexes). Recommendation: ship as a single migration. If the
team disagrees, the splitting work is mechanical.

---

End of plan. Total: ~520 lines.
