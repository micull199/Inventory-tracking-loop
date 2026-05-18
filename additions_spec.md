# UC Inventory — Architectural Additions Spec

**For:** coding agent on `main` (commit `237e66f`+)
**Author:** systems architect (via Michael)
**Date:** 2026-05-15
**Posture:** descriptive of intended end state + a sequenced migration plan. **Do not implement all at once.** Slices are ordered; each slice should ship green before the next opens.

---

## 0. Context and posture

The existing schema is sound. This document does **not** rewrite it. It adds:

1. A **Stone master** entity (the load-bearing addition).
2. A **Metal master** lookup (replaces freetext metal references).
3. A **Design master** lookup (separates design IP from the category tree).
4. **Category-specific attribute groups** as side tables (`item_ring_attrs`, `item_band_attrs`, `item_earring_attrs`, `item_chain_attrs`) — *not* more columns on `items`.
5. **Item ↔ Stone linkage table** (`item_stones`) for centre + accent stones.
6. A few small lookup tables (`metal_master`, `ring_size_standard`, `stone_shape_master`) replacing freetext.

Everything is additive. No existing tables are dropped or renamed in this spec. Migration order matters because some additions reference others.

The existing **catalog-driven field visibility** model continues to work: new attribute-group fields become catalog entries that read/write a side-table column instead of an items-table column. The diff target in §10 of the handoff describes the column-backed path; we extend it with a "side-table-backed" path (see §9).

---

## 1. The Stone entity (slice S1)

**Why:** Currently `stone_shape` is a string on `items`. There's no way to (a) track a centre stone independently of the ring it's set in, (b) move a stone between mounts, (c) reconcile loose-diamond inventory against set stones, (d) carry GIA/IGI cert data, or (e) handle the memo/consignment ownership cases that come with significant stones.

**Tracking rule (locked):** *if a stone has a diamond/grading report, it is a tracked entity. Otherwise it is melee.* Carried on the parent item as aggregate count + CT, not as a stone record.

### 1.1 New table `stones`

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `stone_code` | String(32) | No | — | unique across active + archived; format `STN-NNNNNN`, system-allocated. Mirrors the SKU pattern. |
| `stone_type` | Enum(`StoneType`) | No | — | `diamond`, `lab_diamond`, `sapphire`, `ruby`, `emerald`, `moissanite`, `other`. New `StoneType` StrEnum in `app/models.py`. |
| `shape_id` | Integer FK → `stone_shape_master.id` | No | — | RESTRICT |
| `length_mm` | Numeric(8,3) | Yes | NULL | |
| `width_mm` | Numeric(8,3) | Yes | NULL | |
| `depth_mm` | Numeric(8,3) | Yes | NULL | |
| `carat_weight` | Numeric(8,4) | No | — | required (this is what makes a stone a stone) |
| `colour_grade` | String(8) | Yes | NULL | D-Z for diamonds; descriptive code for coloured |
| `clarity_grade` | String(8) | Yes | NULL | FL..I3 |
| `cut_grade` | String(16) | Yes | NULL | Excellent / VG / Good / Fair / Poor |
| `polish` | String(16) | Yes | NULL | |
| `symmetry` | String(16) | Yes | NULL | |
| `fluorescence` | String(16) | Yes | NULL | |
| `lab` | Enum(`StoneLab`) | Yes | NULL | `gia`, `igi`, `hrd`, `gcal`, `other`, `none` |
| `cert_number` | String(64) | Yes | NULL | partial unique where set + `lab` IS NOT NULL |
| `cert_url` | String(512) | Yes | NULL | |
| `origin` | Enum(`StoneOrigin`) | No | `natural` | `natural`, `lab_grown`, `treated_natural` |
| `treatment` | String(64) | Yes | NULL | e.g. heat, oil, fracture-filled |
| `supplier_id` | Integer FK → `suppliers.id` | Yes | NULL | RESTRICT |
| `ownership` | Enum(`StoneOwnership`) | No | `owned` | `owned`, `memo`, `consignment` |
| `memo_due_date` | Date | Yes | NULL | required if ownership = memo |
| `acquisition_cost` | Numeric(14,4) | Yes | NULL | what we paid; for memo this is the agreed cost if we keep it |
| `acquisition_date` | Date | Yes | NULL | |
| `current_location_id` | Integer FK → `locations.id` | Yes | NULL | RESTRICT |
| `status` | Enum(`StoneStatus`) | No | `available` | `available`, `reserved`, `set`, `sold`, `returned_to_supplier`, `lost` |
| `current_item_id` | Integer FK → `items.id` | Yes | NULL | RESTRICT; populated when status = `set` |
| `notes` | String(2000) | Yes | NULL | |
| `archived_at` | DateTime(tz) | Yes | NULL | soft delete |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

**Indexes:**
- `uq_stones_stone_code` unique on `stone_code`
- `uq_stones_cert` partial unique on `(lab, cert_number)` where both NOT NULL
- `ix_stones_supplier_id`, `ix_stones_current_location_id`, `ix_stones_current_item_id`, `ix_stones_status`, `ix_stones_archived_at`

**Status transitions** (enforce in route, mirroring stage rules):
- `available` → `reserved` | `set` | `sold` | `returned_to_supplier` | `lost`
- `reserved` → `available` | `set` | `sold`
- `set` → `available` (unset) | `sold` (with the ring)
- `sold`, `returned_to_supplier`, `lost` → terminal (admin override only)

### 1.2 New table `stone_shape_master`

Replaces freetext `Item.stone_shape`. Lookup, administered.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `name` | String(32) | No | — | unique active+archived. Seed: round, oval, cushion, emerald, pear, radiant, marquise, princess, asscher, heart, trillion, baguette, other |
| `sort_order` | Integer | No | 0 | |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

Same pattern as `suppliers`/`locations`. Admin-only CRUD route at `/admin/stone-shapes`.

### 1.3 New ledger `stone_events`

Stones are entities with their own lifecycle, so they need their own append-only ledger. Do NOT extend `stock_movements` for this — the cost engine assumes qty/value flows, and stones have neither in the same sense.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | append-only |
| `stone_id` | Integer FK → `stones.id` | No | — | RESTRICT |
| `event_type` | String(32) | No | — | `created`, `set`, `unset`, `sold`, `returned`, `lost`, `relocated`, `cert_updated`, `ownership_changed` |
| `from_item_id` | Integer FK → `items.id` | Yes | NULL | for unset/transferred |
| `to_item_id` | Integer FK → `items.id` | Yes | NULL | for set |
| `from_location_id`/`to_location_id` | Integer FK → `locations.id` | Yes | NULL | for relocated |
| `from_status`/`to_status` | Enum(`StoneStatus`) | Yes | NULL | |
| `actor_id` | Integer FK → `users.id` | Yes | NULL | SET NULL |
| `note` | String(2000) | Yes | NULL | |
| `created_at` | DateTime(tz) | No | now() | |

The `stones.status`, `current_item_id`, `current_location_id` are denormalised from the latest event of relevant types — same posture as `items.current_qty` from `cost_layers`. Single mutation pathway: every set/unset/sell/relocate writes a `stone_event` AND updates the denormalised field, in one transaction.

### 1.4 New table `item_stones` (linkage)

A ring can hold multiple tracked stones (trilogy = 3, three-stone = 3, side stones on solitaire = 1+N). This is a join table with position semantics.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `item_id` | Integer FK → `items.id` | No | — | RESTRICT |
| `stone_id` | Integer FK → `stones.id` | No | — | RESTRICT; **unique across active**: a stone can be set in at most one item at a time |
| `position` | Enum(`StonePosition`) | No | — | `centre`, `accent_left`, `accent_right`, `accent`, `halo`, `gallery`, `other` |
| `position_index` | Integer | No | 0 | for distinguishing multiple accents (0, 1, 2…) |
| `set_at` | DateTime(tz) | No | now() | |
| `unset_at` | DateTime(tz) | Yes | NULL | soft-end (set when stone removed from item) |
| `notes` | String(500) | Yes | NULL | |

**Indexes:**
- `uq_item_stones_active_stone` partial unique on `stone_id` where `unset_at IS NULL` — a stone can only be in one item at a time
- `ix_item_stones_item_id`, `ix_item_stones_stone_id`
- `uq_item_stones_position` partial unique on `(item_id, position, position_index)` where `unset_at IS NULL` — only one stone per slot

**Why a join table with soft-end vs hard delete:** historical record. If a centre stone is later replaced, you want to see what was in the ring previously. This pattern (active set has `unset_at IS NULL`) is consistent with how `archived_at` works elsewhere in the codebase.

### 1.5 Updates to existing `items` model

- Add: `centre_stone_id` Integer FK → `stones.id` nullable RESTRICT — denormalised pointer to the *current* centre stone (the `item_stones` row where `position=centre AND unset_at IS NULL`). Allows fast queries without joining. Single mutation pathway: the set/unset handlers maintain this.
- Add: `total_carat_weight` Numeric(10,4) nullable — derived, sum of tracked stones in this item + `melee_total_ct`. Updated when stones set/unset and when melee fields change.
- Add: `melee_count` Integer NOT NULL default 0
- Add: `melee_total_ct` Numeric(10,4) NOT NULL default 0
- Add: `melee_stone_type` String(32) nullable — usually "diamond" but recorded

**Field catalog entries** added for: `centre_stone_id` (linked picker), `melee_count`, `melee_total_ct`, `melee_stone_type`. These are column-backed and follow the existing diff target pattern.

`stone_shape` (existing freetext String(64)) should be **deprecated** but not dropped in this slice. New ring categories use `centre_stone_id` (which links to a stone, which has a `shape_id`). Migration to drop the freetext field can come later once usage is zero — same posture you took with `item_field_values`.

---

## 2. The Metal entity (slice S2)

**Why:** metal type, purity, colour, and weight are critical for (a) precious metal accounting (you need pure-weight to reconcile gold pool), (b) costing (gold price moves daily), (c) customer-facing display (18k yellow gold), (d) Thailand transfer pricing (declarations need purity). Today there's nothing.

### 2.1 New table `metal_master`

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `metal_code` | String(16) | No | — | unique; e.g. `18KYG`, `14KWG`, `PLAT950`, `PD950`, `SS`, `9KRG` |
| `name` | String(64) | No | — | human label, e.g. "18ct Yellow Gold" |
| `alloy_family` | Enum(`AlloyFamily`) | No | — | `gold`, `platinum`, `palladium`, `silver`, `other` |
| `karat` | Integer | Yes | NULL | 9/14/18/22/24 for gold; null for non-gold |
| `purity_pct` | Numeric(6,3) | No | — | e.g. 75.000 for 18k, 95.000 for Plat950 |
| `colour` | Enum(`MetalColour`) | No | — | `yellow`, `white`, `rose`, `green`, `two_tone`, `platinum`, `palladium`, `silver` |
| `density_g_per_cc` | Numeric(8,3) | Yes | NULL | for volume → weight calc and yield modelling |
| `hallmark_stamp` | String(16) | Yes | NULL | e.g. "750", "PLAT950" |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

**Indexes:** `uq_metal_master_code`, `ix_metal_master_archived_at`

### 2.2 New table `metal_spot_prices`

Daily spot per metal. Separate table because it changes daily and gets append-heavy.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `metal_id` | Integer FK → `metal_master.id` | No | — | RESTRICT |
| `as_of_date` | Date | No | — | unique with `metal_id` |
| `price_per_gram` | Numeric(14,6) | No | — | in AUD (or `currency` if you add it) |
| `source` | String(64) | No | — | e.g. `manual`, `lbma_pm_fix`, `kitco` |
| `notes` | String(500) | Yes | NULL | |
| `created_at` | DateTime(tz) | No | now() | |

**Indexes:** `uq_metal_spot_prices_date` unique on `(metal_id, as_of_date)`, `ix_metal_spot_prices_metal_id`

For v1: manual entry by manager via `/admin/metal-prices`. For v2 (post-$200M): pull from a feed.

### 2.3 Updates to `items`

- Add: `metal_id` Integer FK → `metal_master.id` nullable RESTRICT — primary metal
- Add: `secondary_metal_id` Integer FK → `metal_master.id` nullable RESTRICT — for two-tone
- Add: `pure_metal_weight_g` Numeric(14,4) nullable — derived from `weight_grams × metal_master.purity_pct`. Stored for precious-metal accounting reports.

**Field catalog entries** added: `metal_id`, `secondary_metal_id`. The existing `weight_grams` is reused.

---

## 3. The Design entity (slice S3)

**Why:** today design names ("Emma", "Helena", "Daisy") are encoded as depth-1 nodes under `unique_variant` roots. This works at $100M but couples three concerns:

1. **Design IP** (the named CAD file, the designer credit, the marketing story)
2. **Production hierarchy** (how items flow through stages)
3. **SKU prefix allocation**

Splitting these into a proper `designs` master means: (a) a design can exist before any rings of it are made, (b) the same design can have variants across categories (Emma as ER, Emma as wedding band), (c) design discontinuation doesn't require archiving a taxonomy node, (d) design metadata (cad file, intro date) has a home.

**This slice is the most architecturally significant change and is a one-way door.** Do not ship it without explicit signoff from Michael. The other slices can ship without this one; come back to it once stones and metals are in.

### 3.1 New table `designs`

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `id` | Integer PK | — | — | |
| `design_code` | String(16) | No | — | unique; e.g. `DSG-EMMA`, `DSG-HELENA`. System-allocated. |
| `name` | String(128) | No | — | e.g. "Emma" |
| `collection` | String(64) | Yes | NULL | for grouping |
| `style_family` | Enum(`StyleFamily`) | Yes | NULL | `solitaire`, `halo`, `hidden_halo`, `three_stone`, `trilogy`, `cluster`, `vintage`, `bezel`, `tension`, `cathedral`, `plain_band`, `eternity`, `half_eternity`, `pendant`, `chain`, `stud`, `drop`, `hoop`, `other` |
| `designer` | String(128) | Yes | NULL | internal credit |
| `cad_file_path` | String(512) | Yes | NULL | |
| `default_metal_id` | Integer FK → `metal_master.id` | Yes | NULL | RESTRICT |
| `intro_date` | Date | Yes | NULL | |
| `discontinued_date` | Date | Yes | NULL | |
| `notes` | String(2000) | Yes | NULL | |
| `archived_at` | DateTime(tz) | Yes | NULL | |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 3.2 Update to `items` and `taxonomy_nodes`

Two strategies, pick one:

**Strategy A (preferred): Items reference designs directly.**
- Add: `items.design_id` Integer FK → `designs.id` nullable RESTRICT
- Taxonomy tree stays as today, but the depth-1 "Emma" node becomes a **production grouping**, not the design itself.
- Backfill: every existing depth-1 node under a `unique_variant` root creates a corresponding `designs` row. The `taxonomy_nodes.name` and `designs.name` may diverge over time; that's fine.

**Strategy B: Taxonomy node points to design.**
- Add: `taxonomy_nodes.design_id` Integer FK → `designs.id` nullable
- Items inherit design via their leaf node.
- Cleaner inheritance, but ties design and taxonomy at the structural level. Harder to support "same design in multiple categories".

**Recommend A.** It's looser coupling and gives you the "one design, many physical configurations" pattern that high-volume jewellery brands need by $500M. Bespoke one-offs (no repeat design) leave `design_id` NULL. The existing `unique_variant` auto-leaf machinery doesn't change.

---

## 4. Category-specific attribute groups (slice S4)

**Why:** the items table will balloon if every category-specific attribute becomes a column. Today there are 3 (`ring_size`, `weight_grams`, `stone_shape`). Wedding bands alone need 5-6 more (profile, width, depth, finish, comfort_fit, band_set_style). Earrings need a different 5-6 (closure_type, style, drop_length, etc.).

The pattern: **one side table per attribute group**, FK to `items.id` (unique), nullable everywhere. Joined only when a category needs them. Catalog entries that read these tables get a new `storage` mode: `side_table` instead of `column`.

### 4.1 New table `item_ring_attrs`

Applies to engagement rings, wedding rings, dress rings — anywhere `ring_size` makes sense.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE on delete (this is a per-item extension) |
| `ring_size` | Numeric(6,2) | Yes | NULL | e.g. 6.50. **Replaces freetext `items.ring_size` for new categories.** Existing freetext can coexist during migration. |
| `size_standard` | Enum(`RingSizeStandard`) | Yes | NULL | `us`, `au_uk`, `eu` |
| `resize_tolerance_low` | Numeric(6,2) | Yes | NULL | |
| `resize_tolerance_high` | Numeric(6,2) | Yes | NULL | |
| `band_width_mm` | Numeric(6,2) | Yes | NULL | |
| `band_depth_mm` | Numeric(6,2) | Yes | NULL | |
| `profile` | Enum(`BandProfile`) | Yes | NULL | `court`, `d_shape`, `flat`, `flat_court`, `halfround`, `knife_edge`, `cathedral`, `euro_shank` |
| `finish` | Enum(`MetalFinish`) | Yes | NULL | `polished`, `matte`, `brushed`, `hammered`, `milgrain`, `sandblast` |
| `comfort_fit` | Boolean | Yes | NULL | |
| `shank_style` | Enum(`ShankStyle`) | Yes | NULL | `solid`, `split`, `twisted`, `pave_set`, `plain` |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 4.2 New table `item_engagement_attrs`

Engagement rings only. Most "engagement ring-y" attributes.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE |
| `setting_style` | Enum(`SettingStyle`) | Yes | NULL | `solitaire`, `halo`, `hidden_halo`, `three_stone`, `trilogy`, `cluster`, `vintage`, `bezel`, `tension` |
| `setting_variation` | String(64) | Yes | NULL | freetext sub-variant ("setting variation" from your sheet) |
| `prong_count` | Integer | Yes | NULL | |
| `prong_style` | Enum(`ProngStyle`) | Yes | NULL | `round`, `claw`, `v_tip`, `double_claw` |
| `gallery_style` | Enum(`GalleryStyle`) | Yes | NULL | `open`, `closed`, `filigree` |
| `under_bezel` | Boolean | Yes | NULL | |
| `pairs_with_wedding_band_item_id` | Integer FK → `items.id` | Yes | NULL | RESTRICT; for matched sets |
| `mount_price` | Numeric(14,4) | Yes | NULL | "less the centre stone" — useful for quoting different stones against the same mount |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 4.3 New table `item_band_attrs`

Wedding bands and dress bands.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE |
| `band_set_style` | Enum(`BandSetStyle`) | Yes | NULL | `plain`, `channel_set`, `pave`, `eternity`, `half_eternity`, `mixed_metal` |
| `pairs_with_engagement_item_id` | Integer FK → `items.id` | Yes | NULL | RESTRICT |
| `matching_set_id` | String(32) | Yes | NULL | optional grouping code for his/hers sets |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 4.4 New table `item_earring_attrs`

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE |
| `sold_as` | Enum(`EarringSold`) | Yes | NULL | `pair`, `single` |
| `closure_type` | Enum(`EarringClosure`) | Yes | NULL | `butterfly`, `screw_back`, `lever_back`, `hook`, `french_wire`, `clip`, `huggie` |
| `style` | Enum(`EarringStyle`) | Yes | NULL | `stud`, `drop`, `hoop`, `chandelier`, `huggie`, `threader`, `climber` |
| `drop_length_mm` | Numeric(6,2) | Yes | NULL | |
| `hoop_diameter_mm` | Numeric(6,2) | Yes | NULL | nullable; only hoops |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 4.5 New table `item_chain_attrs`

Applies to chains, necklaces, bracelets — the linear products.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE |
| `chain_style` | Enum(`ChainStyle`) | Yes | NULL | `cable`, `curb`, `box`, `rope`, `snake`, `figaro`, `belcher`, `wheat`, `singapore`, `herringbone` |
| `length_mm` | Numeric(8,2) | Yes | NULL | |
| `adjustable` | Boolean | Yes | NULL | |
| `min_length_mm` | Numeric(8,2) | Yes | NULL | nullable; adjustable only |
| `max_length_mm` | Numeric(8,2) | Yes | NULL | nullable; adjustable only |
| `link_width_mm` | Numeric(6,2) | Yes | NULL | |
| `closure_type` | Enum(`ChainClosure`) | Yes | NULL | `lobster`, `spring_ring`, `box`, `toggle`, `s_hook`, `barrel`, `magnetic` |
| `worn_as` | Enum(`WornAs`) | Yes | NULL | `necklace`, `bracelet`, `anklet` |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 4.6 New table `item_pendant_attrs`

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE |
| `length_mm` | Numeric(8,2) | Yes | NULL | |
| `width_mm` | Numeric(8,2) | Yes | NULL | |
| `bail_type` | Enum(`BailType`) | Yes | NULL | `fixed`, `hinged`, `hidden`, `enhancer` |
| `bail_opening_mm` | Numeric(6,2) | Yes | NULL | |
| `includes_chain` | Boolean | Yes | NULL | |
| `default_chain_item_id` | Integer FK → `items.id` | Yes | NULL | RESTRICT |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

### 4.7 New table `item_engraving_attrs`

Orthogonal to category — applies wherever engraving is offered.

| Field | Type | Null | Default | Notes |
|---|---|---|---|---|
| `item_id` | Integer PK FK → `items.id` | No | — | CASCADE |
| `engraving_available` | Boolean | No | False | |
| `max_chars_outside` | Integer | Yes | NULL | |
| `max_chars_inside` | Integer | Yes | NULL | |
| `engraving_text` | String(255) | Yes | NULL | |
| `engraving_font` | String(64) | Yes | NULL | |
| `engraving_style` | Enum(`EngravingStyle`) | Yes | NULL | `machine`, `hand`, `laser` |
| `created_at` / `updated_at` | DateTime(tz) | No | now() | |

---

## 5. Cost method posture (no schema change)

The handoff confirms the cost engine is **FIFO universally**. For your business this resolves as follows, and no change is needed — but document the reasoning so it doesn't get re-litigated:

| make_type (existing archetype) | Effective cost behaviour under existing FIFO engine |
|---|---|
| `bulk` (qty-tracked stock) | True FIFO. Multi-receipt averaging happens naturally as layers consume. |
| `unique` (one-record-per-unit) | Each item has exactly one receipt → one cost layer → de facto actual cost. |
| `unique_variant` (auto-numbered design pieces) | Same as `unique`: one item, one layer, one cost. De facto actual cost. |

In other words: **the hybrid model (standard for stock, actual for bespoke) emerges naturally from the existing archetype distinction.** The FIFO engine, plus archetype, plus one-layer-per-unique-item, equals the right behaviour.

What is **not** captured today and you'll want later:
- **Standard cost** as a planning estimate per design. `designs.standard_cost` would belong on the new `designs` table — manager-maintained, used for pre-build quoting. Adds in slice S3.
- **Cost variance reporting** (standard vs actual at the design level). Becomes possible once designs exist.

No urgent action. Flag this in the architectural notes file when you ship S3.

---

## 6. Free-text-to-lookup conversions (slice S5, low priority)

Three of the four freetext fields flagged in §7.7 of the handoff get lookups in earlier slices (`stone_shape` → `stone_shape_master` in S1; `ring_size` → numeric in `item_ring_attrs` in S4). Two remain:

- **`Item.unit`**: introduce `unit_master` with seed values (`ea`, `pc`, `g`, `kg`, `ct`, `mm`, `cm`, `m`, `pair`, `pack`). Existing freetext column stays during migration; backfill normalises common variants (`"kg"` / `"kgs"` / `"kilograms"` → unit "kg"). FK column `unit_id` added; freetext column eventually dropped.
- **`Movement.reason`**: introduce `reason_codes` lookup scoped by movement type. E.g. `out` reasons: `sale`, `customer_pickup`, `bench_consumption`, `casting_consumption`, `wastage`, `internal_use`, `damaged`. Keeps an `other_text` field for the long tail.

Low priority because (a) the existing freetext works, (b) the value of lookups here is reporting cleanliness, which matters more at $500M than at $100M. Defer until reporting starts to bite.

---

## 7. What NOT to add (and why)

Things I considered and rejected for this iteration. Recording them so the discussion doesn't reopen unprompted.

| Idea | Why not |
|---|---|
| Per-location stock for items | The handoff §8.1 already flags this as a known future need. It's a deep refactor (cost layers become location-scoped, `current_qty` denormalisation changes, stock movements grow `from_location_id`/`to_location_id`). Don't bundle it with attribute additions. Separate slice when triggered by Thailand needing per-location consumption tracking. |
| Photo attachments on items | Out of v1 scope per MISSION. Add when retail/web channel needs them. Likely shape: `item_attachments` table with `kind`, `url`, `sort_order`, `is_primary`. Storage via S3 or Fly Volumes; defer. |
| Native customer table | Customer master belongs in HubSpot, not in inventory. When integration is built, items link to customers via a HubSpot ID, not a local customer record. Avoid the temptation to mirror customer data into this app. |
| Stone movement integrated into `stock_movements` | Stones are entities with non-quantity lifecycles. Conflating them breaks the cost engine's FIFO assumptions. Use the dedicated `stone_events` ledger. |
| Sparse `item_field_values` resurrection | Was correctly dropped in migration 0024. Side-table attribute groups (S4) are the right replacement — type-safe, indexed, and join only when needed. |
| Per-SKU `cost_method` override field | Not needed; the archetype + FIFO combo gives you the right behaviour by construction. |
| Status enum for stock takes | Derived from timestamps today; adding an enum invites drift. Leave alone unless cancel-mid-take becomes a requirement. |

---

## 8. Slice sequencing and dependencies

Ship in this order. Each slice should pass `make check` green before the next opens. Migrations stack: don't squash.

```
S1: Stones                              [START HERE]
    ├── 0025_create_stone_shape_master.py
    ├── 0026_create_stones.py
    ├── 0027_create_stone_events.py
    ├── 0028_create_item_stones.py
    └── 0029_add_items_stone_columns.py     (centre_stone_id, total_carat_weight, melee fields)

S2: Metals                              [DEPENDS ON NONE]
    ├── 0030_create_metal_master.py
    ├── 0031_create_metal_spot_prices.py
    └── 0032_add_items_metal_columns.py     (metal_id, secondary_metal_id, pure_metal_weight_g)

S3: Designs                             [DEPENDS ON S2 for default_metal_id; ARCHITECTURAL SIGNOFF GATE]
    ├── 0033_create_designs.py
    └── 0034_add_items_design_id.py + backfill from existing unique_variant depth-1 nodes

S4: Attribute groups                    [DEPENDS ON S1 (item_engagement uses stones), S2 (rings use metals)]
    ├── 0035_create_item_ring_attrs.py
    ├── 0036_create_item_engagement_attrs.py
    ├── 0037_create_item_band_attrs.py
    ├── 0038_create_item_earring_attrs.py
    ├── 0039_create_item_chain_attrs.py
    ├── 0040_create_item_pendant_attrs.py
    └── 0041_create_item_engraving_attrs.py

S5: Free-text lookups                   [DEPENDS ON NONE; lowest priority]
    ├── 0042_create_unit_master.py
    ├── 0043_create_reason_codes.py
    └── 0044_backfill_unit_and_reason.py
```

S1 and S2 can be parallelised by a small team. S3 needs explicit Michael signoff before it ships. S4 should not start until S1 lands.

---

## 9. Pattern: side-table-backed catalog field

Extends the existing diff target (§10 of handoff) for fields that live on a side table instead of `items`.

### Required additions to `field_catalog.CatalogEntry`

```python
@dataclass(frozen=True)
class CatalogEntry:
    key: str
    label: str
    type: FieldType
    sort_order: int
    # existing:
    column: str | None = None        # column on `items` table (legacy path)
    # new:
    storage: Storage = Storage.ITEM_COLUMN   # ITEM_COLUMN | SIDE_TABLE | FK_LOOKUP
    side_table: str | None = None    # e.g. "item_ring_attrs"
    side_column: str | None = None   # e.g. "band_width_mm"
    fk_table: str | None = None      # e.g. "stones" (for centre_stone_id)
```

### Read path (extend `field_storage.py`)

```python
def read_value(item, entry):
    if entry.storage == Storage.ITEM_COLUMN:
        return getattr(item, entry.column)
    if entry.storage == Storage.SIDE_TABLE:
        side = get_side_row(item, entry.side_table)   # lazy-load helper
        return getattr(side, entry.side_column) if side else None
    if entry.storage == Storage.FK_LOOKUP:
        return getattr(item, entry.column)            # FK id; UI resolves to label
```

### Write path

- On create/update of an item, after the items row is upserted, group form fields by `side_table` and upsert one row per side table where any field is non-NULL.
- Side rows with all NULLs are deleted (no zombie rows).
- All in the same DB transaction as the items write.

### Catalog entry example for `band_width_mm`

```python
CatalogEntry(
    key="band_width_mm",
    label="Band width (mm)",
    type=FieldType.DECIMAL,
    sort_order=410,
    storage=Storage.SIDE_TABLE,
    side_table="item_ring_attrs",
    side_column="band_width_mm",
),
```

### Updated diff target — adding a side-table field

For a new side-table-backed catalog field, the blast radius is similar to the column-backed path but with one extra file:

1. `app/models.py` — add column to the existing side-table model (or new model if it's the first field in a new group)
2. New Alembic migration adding the column (or new table)
3. `app/field_catalog.py` — `CatalogEntry` with `storage=SIDE_TABLE`
4. `app/field_visibility.py` — `BUILT_IN_FIELDS` + default visibility (same as today)
5. `app/items.py` — read form value, no per-field handler change once the side-table groupby helper exists
6. `app/field_storage.py` — already handles via `storage` dispatch, no per-field code
7. Templates: same as today
8. CSV export/import: extended to flatten side-table fields into columns (one-time work in `csv_export.py` / `csv_import.py` when the side-table dispatch is added)
9. Tests

After the initial side-table machinery lands (one-time cost in `items.py`, `field_storage.py`, `csv_*.py`), adding a 20th side-table field is one migration + one catalog entry + tests.

---

## 10. Open decisions for Michael before S1 starts

Three things to lock before the agent writes the S1 migration:

1. **Melee threshold tightening.** Confirmed rule = "has a cert → tracked". One edge case: *coloured stones >$X cost or >X.X ct without a cert*. Recommend extending the rule: `cert OR coloured-stone-above-threshold OR override`. The override case is a manual flag on the stone record (`tracking_trigger = manual`). Confirm threshold values.

2. **Stone code allocator.** Mirror SKU allocator pattern (`stones.next_sequence` somewhere? Or a single global counter table)? Recommend: single global counter, `STN-NNNNNN`, no prefix variation — stones aren't categorised the way items are.

3. **Should `stones.acquisition_cost` flow into the FIFO cost engine** when the stone is set into a ring? Two paths:
   - **A:** Stone cost is *separate* from the ring's cost layers. Ring shows "mount cost + stone cost" as two components.
   - **B:** Stone cost is added to the ring's cost layer at set-time, becoming part of FIFO.

   Recommend **A** for traceability — keeps stone P&L isolated, makes memo stones cleanly accountable. The ring's display price computes as `ring_cost_layer_sum + sum(set_stones.acquisition_cost)` on the fly.

---

*End of additions spec.*
