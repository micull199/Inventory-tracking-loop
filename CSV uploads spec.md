# CSV uploads — Spec

**Status:** Design only. Not yet implemented.
**Scope:** Bulk-create rows in four domains via CSV upload. The upload format
mirrors the download exactly (1:1 column match), plus optional columns the
download skips. Download → edit → upload is the canonical round-trip.

Domains in scope:

1. **Items** (`/admin/items`)
2. **Suppliers** (`/admin/suppliers`)
3. **Locations** (`/admin/locations`)
4. **Taxonomy** (`/admin/taxonomy`)

Out of scope (events, derived views, audit): movement timelines, audit log,
PO list, transfers, checkouts, variance reports. CSV upload doesn't make
sense for append-only event streams — those have explicit dedicated UIs.

---

## Cross-cutting design

### Where the button lives

On every list view that already has **"Download CSV"**, add an **"Upload
CSV"** button next to it. Same role gating as the existing list create
button (e.g. Manager-only on taxonomy; Manager+Office on items).

### Upload page shape

`GET /admin/<domain>/upload` — manager-only form:

- File input (`<input type=file accept=".csv">`).
- "Dry run" checkbox (default ON). When checked, parse + validate but don't
  write anything; show a preview table with what would be created /
  rejected / skipped, with row-by-row errors. Off = commit.
- Submit button labelled "Validate" when dry-run is on, "Upload N rows"
  when off (count derived from validation pass).

`POST /admin/<domain>/upload` — handles both validation pass and commit
pass. Always returns the same preview/result page; on commit, it adds a
flash "N rows imported" and 303s back to the list.

### Validation pipeline (shared)

Per row:

1. **Parse:** Split CSV row. Reject if column count doesn't match header.
2. **Type-coerce** each cell per the domain's column spec (e.g. integer,
   ISO date, decimal, boolean as `yes`/`no`).
3. **Normalise:** trim whitespace, uppercase prefixes, drop trailing empty
   columns.
4. **Domain validation:** call the existing per-domain validators (e.g.
   `_validate_name`, `_check_sku_prefix_unique_top`) without writing.
5. **Cross-row validation:** detect duplicates *inside* the uploaded file
   (e.g. two rows with the same SKU). Reject both with a clear error.
6. **Tag each row** with one of: `new` (will create), `skip` (already
   exists, idempotent re-upload), `error` (validation failed).

Then either:

- **Dry run:** render the preview table with one row per CSV row showing
  the tag + per-row error message. No writes.
- **Commit:** wrap all `new` rows in a single transaction. If any row
  fails on insert (e.g. constraint violation that validation missed),
  roll back everything; show the user the offending row and re-render
  preview. **All-or-nothing**, never partial.

### Idempotency strategy

The upload format includes the `id` column (which the download already
emits). Behaviour by row:

- **`id` blank:** treat as **create**. The CSV may emit blank rows so the
  spreadsheet doesn't have to be hand-edited.
- **`id` matches existing row:** treat as **skip** with a warning row
  ("row already exists; updates via CSV upload are out of scope of v1").
- **`id` non-matching integer:** **error**. ("Unknown id N — don't reuse
  ids from another database").

This makes "download → add rows → re-upload" safe. The user adds rows
without an id, and existing rows are skipped not updated.

**Updates via CSV are deliberately out of v1 scope.** Editing a supplier
through CSV would re-introduce the "did I just clobber a colleague's
edit" problem the existing per-row edit forms avoid via 303-after-POST
and audit shape. v1 is upload-to-create only.

### Error UX

Per-row errors render in the preview table:

| Row # | Status | Field | Error |
| ----- | ------ | ----- | ----- |
| 3 | error | sku | "already exists on row 7 of this file" |
| 7 | error | sku | "already exists on row 3 of this file" |
| 12 | skip | id | "id=42 already exists; updates not supported" |
| 14 | new | — | — |

Errors block commit. Skips don't. Dry-run always shows the full table.

### Audit

Each created row writes its normal per-domain audit row (`item.created`,
`supplier.created`, etc.) plus a `<domain>.csv_uploaded` summary row with
the count, the actor, and the file's SHA-256 (so a re-upload of the same
file is identifiable). The per-row audit shape is **identical** to a
manual create — the CSV is just an alternative input.

### File size + safety

- Max 5 MB / 5,000 rows per upload (whichever first). Configurable later.
- UTF-8 only; emit "non-UTF-8 file" if the first decode fails.
- Reject files whose header row doesn't match the expected schema with
  a clear "header mismatch; expected: …, got: …" message.

### Roles

| Domain    | View list | Download CSV | Upload CSV |
| --------- | --------- | ------------ | ---------- |
| Items     | Workshop+ | Manager+Office (existing) | Manager+Office |
| Suppliers | Manager   | Manager (existing) | Manager |
| Locations | Manager   | Manager (existing) | Manager |
| Taxonomy  | Manager   | Manager (existing) | Manager |

Admin is implicit on every Manager gate. Workshop never uploads CSVs.

---

## Domain 1 — Items

### Existing download columns

`id, sku, name, category, stage, unit, tracking_mode, current_qty,
reorder_threshold, reorder_qty, requires_checkout`

### Upload column rules

| Column            | On create | Notes |
| ----------------- | --------- | ----- |
| `id`              | blank     | non-blank → skip (existing) or error (unknown) |
| `sku`             | ignored   | server allocates from leaf prefix; uploading a non-blank value emits a row warning. (See "SKU allocation" below.) |
| `name`            | required  | unless leaf's field-visibility marks it hidden, in which case auto-fills to SKU |
| `category`        | required  | resolved as either a numeric leaf id or a slash-path like `"Rings / Silver / 925"`. 400 if no unambiguous match. |
| `stage`           | optional  | stage name on the resolved leaf's top-level category. If category has an `is_initial` stage and `stage` is blank, defaults to that. If non-blank and not a valid stage for the category, error. |
| `unit`            | required  | unless visibility=hidden → uses leaf default |
| `tracking_mode`   | optional  | `qty` or `unique`; defaults to the archetype-derived value (BULK→qty, UNIQUE/UNIQUE_VARIANT→unique). |
| `current_qty`     | ignored   | items always start at 0 — stock-in via separate movement, not CSV |
| `reorder_threshold` | optional | decimal; default 0 |
| `reorder_qty`     | optional  | decimal; default 0 |
| `requires_checkout` | optional | `yes` / `no` (matches the download); default no |

Plus **custom-field columns**. Any column not in the table above is
treated as a custom field. The column header must match an active field
def's `key` on the resolved leaf or any ancestor (inheritance applies).
Unknown column → error.

### SKU allocation

CSV uploads always use the **server-allocated SKU**, even if the file
contains a value in the `sku` column. The download emits the existing
SKU for round-trip parity, but on upload non-blank `sku` cells on new
rows are accepted as informational only and a row warning is emitted
("sku column ignored on create — server allocates from leaf prefix").

If the user wants specific SKUs they have to use the existing per-item
create form, which is also server-allocated — there's no path to a
user-chosen SKU in v1.

### Per-archetype rules

- **BULK:** straightforward. Resolved leaf is the item's
  `taxonomy_node_id`.
- **UNIQUE:** straightforward. Resolved leaf is the item's
  `taxonomy_node_id`. `tracking_mode` is forced to `unique`.
- **UNIQUE_VARIANT:** the resolved category must be a depth-1
  sub-category (not a depth-2 auto-leaf). The server mints the depth-2
  auto-leaf at create time (same as `app.sku.create_unique_variant_leaf`).
  Uploading rows that point at a depth-2 auto-leaf is rejected ("auto-leaves
  are server-managed; pick the parent sub-category").

---

## Domain 2 — Suppliers

### Existing download columns

`id, name, email, archived_at`

(`archived_at` round-trips as ISO datetime or empty.)

### Upload column rules

| Column        | On create | Notes |
| ------------- | --------- | ----- |
| `id`          | blank     | non-blank → skip / error as above |
| `name`        | required  | unique across active + archived |
| `email`       | optional  | RFC-shape validation only |
| `archived_at` | ignored on create | non-blank emits a row warning; create always lands on active. To archive, use the per-row form. |

---

## Domain 3 — Locations

### Existing download columns

`id, name, notes, archived_at`

### Upload column rules

| Column        | On create | Notes |
| ------------- | --------- | ----- |
| `id`          | blank     | as above |
| `name`        | required  | unique across active + archived |
| `notes`       | optional  | free text, ≤2000 chars |
| `archived_at` | ignored on create | as above |

---

## Domain 4 — Taxonomy

The taxonomy has three list views: top-level (`/admin/taxonomy`),
sub-categories (`/admin/taxonomy/{id}/children`), and grandchildren
(`/admin/taxonomy/{id}/sub/{id}/grandchildren`). Each has its own CSV
download. Each gets its own upload.

### Top-level (depth 0)

Download columns: `id, sort_order, name`.

Upload columns:

| Column       | On create | Notes |
| ------------ | --------- | ----- |
| `id`         | blank     | as above |
| `sort_order` | optional  | int; defaults to max+10 |
| `name`       | required  | unique across active + archived top-level |
| `sku_prefix` | optional  | 1-8 alnum; auto-derived from name if blank (existing rule) |
| `archetype`  | required  | `bulk` / `unique` / `unique_variant` (no inheritance at depth 0) |

### Sub-categories (depth 1)

Same shape as top-level, minus archetype (inherits from root). Filename
encodes the parent id: `subcategories_parent_{N}.csv`. Upload requires the
same parent id (URL param), and every row's parent is auto-set.

Columns: `id, sort_order, name, sku_prefix`.

### Grandchildren (depth 2)

Same as sub-categories. Archetype `UNIQUE_VARIANT` parents reject the
upload entirely (depth-2 nodes under UV are auto-managed by item create).

### Cross-tree concerns

- Field defs and stages cannot be created via CSV in v1 — they're per-key
  schema decisions, not bulk data. Out of scope.
- Defaults / field-visibility JSON columns are not part of the download
  CSV and are not uploadable. Manage via the per-node admin forms.

---

## Implementation plan (deferred — design only here)

When implementation lands, sequence:

1. **Items** — highest-value, biggest column set; everything below is
   smaller variants of the same machinery.
2. **Suppliers** — easiest; minimal validation surface.
3. **Locations** — same shape as suppliers.
4. **Taxonomy top-level** — needs archetype + sku_prefix machinery.
5. **Taxonomy sub / grandchildren** — variants of (4).

A shared module `app/csv_import.py` (mirror of `app/csv_export.py`) holds
the parse + validate + commit pipeline. Each domain provides a
column-rule descriptor + a per-row creator function. The route
boilerplate is identical across all four domains.

---

## Open questions for the user

None blocking. If any of these change, flag before implementation begins:

- **Updates via CSV.** Deliberately out of v1 scope. Re-uploading an
  existing row skips it. If you want true update-on-upload, that's a
  separate slice with its own audit semantics.
- **Server-allocated SKU on item upload.** v1 ignores user-supplied SKU
  on create. If you need to import items with externally-assigned SKUs
  (e.g. migration from another system), that's a separate slice.
- **Dry-run default ON.** I think this is the right default — most CSV
  uploads in inventory systems fail validation the first time. Flip to
  OFF if you find dry-run annoying in practice.
