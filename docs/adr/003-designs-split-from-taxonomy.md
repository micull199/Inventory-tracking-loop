# ADR-003: Split design IP from taxonomy via a dedicated `designs` table

- **Status**: Accepted (2026-05-15)
- **Decision-maker**: Michael Cullen
- **Authors**: systems architect (via Michael)
- **Supersedes**: none
- **Related**:
  - `additions_spec.md` §3 (the proposing spec)
  - `docs/taxonomy-refinement-plan.md` (the prior model this partially supersedes)

> *This is the first formal ADR in the repo. 001 and 002 are reserved for
> retrospective records of (1) the original archetype-aware taxonomy
> refinement and (2) the catalog-driven side-table dispatcher — both are
> documented elsewhere in the codebase but haven't been written up as
> ADRs yet. Numbering starts at 003 to keep that backfill space open.*

## Context

In the existing schema, a design like "Emma" is encoded as a depth-1
`TaxonomyNode` sub-category under a `unique_variant` root (typically
`Rings → Emma → 001`). The "Emma" node simultaneously carries three
concerns:

1. **Design IP** — what Emma *is*: the CAD file, the designer credit,
   the marketing story, the intro / discontinuation dates.
2. **Production grouping** — where Emma items live in the tracking
   hierarchy; the auto-numbered leaves under it.
3. **SKU prefix allocation** — Emma contributes letters to the SKU.

This conflation works at $100M revenue and ~10 employees, but breaks
down as the business grows in three concrete ways:

- A design can't exist before any rings of it are made (no taxonomy
  node yet means no CAD file path, no designer credit).
- The same design can't live in multiple categories without
  duplicating the metadata (Emma as an engagement ring AND Emma as a
  wedding band needs two Emma nodes that drift over time).
- Discontinuing a design requires archiving the taxonomy node, which
  loses the stage definitions and sub-cat structure that production
  still depends on.

`additions_spec.md` §3 proposes splitting these concerns by introducing
a dedicated `designs` table. This ADR records the architectural
commitment and the modifications negotiated for the first shipping
slice.

## Decision

### What we ship now

1. **Add a `designs` master table** with the schema in §3.1 of the spec,
   plus three additions:
   - `cad_version` (`String(32)`, nullable) — human-readable version
     identifier the team picks ("v1.2", "2026-05-15-rev3"); deliberately
     loose because conventions vary by designer.
   - `cad_updated_at` (`DateTime(tz)`, nullable) — machine-comparable
     freshness signal. Answers "is this stale?" without depending on
     version-string discipline.
   - `standard_cost` (`Numeric(14, 4)`, nullable) — planning estimate
     per design, manager-maintained. Spec §5 flags this as a follow-on;
     including the column up-front avoids a migration when standard-vs-
     actual variance reporting starts to bite.
2. **Allocate `design_code` from a single global counter** (`DSG-NNNN`,
   4-digit pad), reusing the `sequence_counters` infrastructure that
   already backs `STN-NNNNNN` for stones. Same allocator pattern, same
   atomic `UPDATE … RETURNING` round-trip.
3. **Strategy A** (per spec §3.2): designs are referenced *from* items
   via a future `items.design_id` FK. The taxonomy tree stays unchanged;
   its depth-1 nodes under unique-variant roots become "production
   groupings", not the design itself.
4. **Minimal admin CRUD** at `/admin/designs` — list / create / edit
   only. Manager + Admin only, mirroring the existing
   `/admin/stone-shapes`, `/admin/locations`, `/admin/suppliers` pattern.
5. **Designs are shared across casting locations** (Australia +
   Thailand). One Emma row regardless of where the rings are spun.
   Location lives on `Item.location_id` and `Stone.current_location_id`,
   not on designs. If TH ever needs a deliberately different Emma
   (e.g. heavier shank for export markets), that's a new design row
   (`Emma-Heavy`), not a Thailand-flavoured Emma — the naming forces the
   divergence to be visible in reporting.

### What we hold for a follow-up slice

1. **`items.design_id` FK column** is *not* added in this slice.
2. **Backfill** of existing depth-1 unique-variant nodes into
   `designs` rows is *not* run in this slice.
3. **Items-form picker** for designs is *not* wired.
4. **Standard-vs-actual variance reporting** (which depends on the
   item-level FK) is *not* started.

These four items together are the load-bearing parts of the spec's
original S3 scope. They're held because the schema commitment is
durable (rollback is expensive) and the user wants to start populating
design metadata — CAD paths, designer credits, intro dates — before the
items table starts referencing the new rows. Operators get value from
the lookup immediately; the structural item-level commitment can wait
until the metadata has been entered for at least the active design
catalogue.

### Why Strategy A over Strategy B

Per spec §3.2, the alternative (Strategy B: `taxonomy_nodes.design_id`)
would tie designs and taxonomy at the structural level. It's a cleaner
single-source-of-truth in the short term but makes the "one design,
many categories" pattern hard — each taxonomy node can only point at
one design, so Emma-as-engagement-ring and Emma-as-wedding-band would
need separate taxonomy structures even though they share design IP.
Strategy A keeps the two concerns loose; bespoke one-offs leave
`items.design_id NULL`; the existing auto-leaf machinery doesn't
change.

## Consequences

### Positive

- Design metadata (CAD path, version, updated_at, designer, intro and
  discontinued dates) has a home that's independent of physical
  inventory. Operators can populate it as designs are added without
  waiting for the first ring to be cast.
- The `DSG-NNNN` allocator pattern is a one-liner addition on the
  existing `sequence_counters` table — no new infrastructure cost.
- The CAD freshness signal (`cad_updated_at`) gives the workshops a
  way to detect stale local CAD pulls without inventing a sync
  protocol.
- Future S3-completion work (the items FK + backfill) becomes a clean
  small slice — the table is already populated and the data shape is
  already validated against operator use.

### Negative

- For the duration of the deferred FK, designs and items remain
  unlinked. Standard-vs-actual variance reporting can't start.
- A typo on a design name doesn't surface as a broken link (because
  there is no link yet) — it surfaces only at the point a manager
  picks a design from a list. Acceptable for v1; the eventual FK will
  enforce referential integrity.
- The `designs` table can drift from operator intent during the
  deferred period (rows created speculatively that turn out not to
  match real production). Manageable: the eventual backfill slice
  will require a manual review of unlinked design rows before the FK
  is added. Documented as a known follow-up.

### Neutral but worth noting

- The taxonomy's *role* shifts the moment the items FK lands: today
  it's "where the design lives"; after the follow-up slice it's
  "where items are tracked, separate from what they are". Operators
  will need a heads-up before the change ships. This ADR records the
  intent so the change isn't a surprise.
- `archived_at` is included on `designs` (matches the codebase's
  universal soft-delete convention) but the admin route in this slice
  does *not* expose archive / unarchive — that's deferred with the
  rest of the lifecycle UX. The column exists so adding the route is
  later a one-line change, not a migration.

## Implementation summary (for the slice that ships with this ADR)

| File | Change |
|---|---|
| `migrations/versions/0044_create_designs.py` | new table + `design_code` counter row |
| `app/models.py` | `Design` model + `StyleFamily` enum |
| `app/designs.py` | new — `allocate_design_code` + CRUD routes |
| `app/templates/designs_list.html`, `_form.html` | new |
| `app/main.py` | register router |
| `app/templates/base.html` | nav link |
| `tests/unit/test_designs.py` | model + allocator |
| `tests/integration/test_designs_routes.py` | CRUD + role enforcement |
| `tests/integration/test_rbac_sweep.py` | new route entries |

## Open questions (deferred)

- **Designer attribution**: `Design.designer` is a single `String(128)`
  freetext today. If we add a `users` link (designer attribution to a
  real account) or external designer records, that's a follow-up
  migration; the freetext column can stay alongside.
- **CAD file storage**: `cad_file_path` is a string today. If we move
  to S3 / blob storage for the file contents themselves, the path
  becomes a key/URL rather than a filesystem path. The column type
  doesn't change; only the value semantics do.
- **Per-region pricing**: `standard_cost` is a single currency-agnostic
  Numeric. If AU / TH need distinct standard costs (different labour
  rates), the column splits into a side table — out of scope for this
  ADR.
