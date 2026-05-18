"""Hardcoded catalog of standard fields on items + side-table extensions.

A taxonomy node *picks* entries from this catalog to opt its items' form +
list + CSV views into showing those fields. There are two storage modes:

- ``ITEM_COLUMN`` (legacy / default): the entry maps 1:1 to a real column
  on the ``items`` table. ``entry.column`` is the matching attribute on
  the ``Item`` model.
- ``SIDE_TABLE`` (S4 architectural additions, spec §9): the entry maps
  to a column on a per-category side table (``item_ring_attrs``,
  ``item_engagement_attrs``, …). ``entry.side_table`` names the table
  and ``entry.side_column`` names the column on it.

Adding a field is still a deliberate code change + migration. The storage
mode picks whether you add a column to ``Item`` or a column to a side-
table model — but the catalog entry shape, audit shape, and CSV column
behaviour stay uniform across the two paths.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Final

from app.models import FieldType


class Storage(enum.StrEnum):
    """Where a catalog field's value physically lives.

    ``ITEM_COLUMN`` is the legacy / default path: a real column on
    ``items``. Every pre-S4 catalog entry uses this and continues working
    untouched. ``SIDE_TABLE`` (S4 / spec §9) points at a column on a
    per-item side table (one-to-one PK FK CASCADE) such as
    ``item_ring_attrs``. The read-path dispatcher in
    ``app.field_storage.read_catalog_value`` selects the path off this
    value; the items-form write path is staged for a follow-up slice.
    """

    ITEM_COLUMN = "item_column"
    SIDE_TABLE = "side_table"


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One field a category may pick.

    ``key`` is the stable identifier the rest of the app uses (and the
    form input name + CSV column header). ``label`` is the display name.
    The pair ``(type, options)`` defines the input shape; ``options`` is
    empty unless ``type`` is ``SELECT`` or ``MULTISELECT``.

    ``storage`` picks the read/write path. For ``Storage.ITEM_COLUMN``
    (default), ``column`` names a real column on the ``Item`` model and
    ``side_table`` / ``side_column`` are left ``None``. For
    ``Storage.SIDE_TABLE``, ``side_table`` and ``side_column`` are
    required and ``column`` is left ``None``.
    """

    key: str
    label: str
    type: FieldType
    column: str | None = None
    options: tuple[str, ...] = ()
    help_text: str | None = None
    sort_order: int = 0
    storage: Storage = Storage.ITEM_COLUMN
    side_table: str | None = None
    side_column: str | None = None

    def __post_init__(self) -> None:
        """Cross-field invariants for the two storage modes.

        Raises ``ValueError`` if a catalog entry violates the shape its
        ``storage`` requires. Catches author errors at import time rather
        than letting them surface as cryptic ``None`` reads at runtime.
        """

        if self.storage is Storage.ITEM_COLUMN:
            if not self.column:
                raise ValueError(
                    f"{self.key}: ITEM_COLUMN entries require a column name"
                )
            if self.side_table or self.side_column:
                raise ValueError(
                    f"{self.key}: ITEM_COLUMN entries must leave "
                    f"side_table / side_column unset"
                )
        else:  # Storage.SIDE_TABLE
            if not self.side_table or not self.side_column:
                raise ValueError(
                    f"{self.key}: SIDE_TABLE entries require both "
                    f"side_table and side_column"
                )
            if self.column:
                raise ValueError(
                    f"{self.key}: SIDE_TABLE entries must leave column unset"
                )


# Tracking mode semantics (``unique`` forces qty=1, disables reorder logic) are
# enforced in ``app/items.py`` create/edit handlers — the catalog only declares
# the input shape.
FIELD_CATALOG: Final[tuple[CatalogEntry, ...]] = (
    CatalogEntry(
        key="name",
        label="Name",
        type=FieldType.TEXT,
        column="name",
        sort_order=10,
    ),
    CatalogEntry(
        key="unit",
        label="Unit of measure",
        type=FieldType.TEXT,
        column="unit",
        sort_order=20,
    ),
    CatalogEntry(
        key="tracking_mode",
        label="Tracking mode",
        type=FieldType.SELECT,
        column="tracking_mode",
        options=("qty", "unique"),
        help_text="`unique` forces qty=1 and disables reorder logic.",
        sort_order=30,
    ),
    CatalogEntry(
        key="requires_checkout",
        label="Requires checkout",
        type=FieldType.BOOLEAN,
        column="requires_checkout",
        sort_order=40,
    ),
    CatalogEntry(
        key="reorder_threshold",
        label="Reorder threshold",
        type=FieldType.DECIMAL,
        column="reorder_threshold",
        sort_order=50,
    ),
    CatalogEntry(
        key="reorder_qty",
        label="Reorder qty",
        type=FieldType.DECIMAL,
        column="reorder_qty",
        sort_order=60,
    ),
    CatalogEntry(
        key="supplier_id",
        label="Supplier",
        type=FieldType.NUMBER,
        column="supplier_id",
        help_text="Pick from active suppliers.",
        sort_order=70,
    ),
    CatalogEntry(
        key="location_id",
        label="Location",
        type=FieldType.NUMBER,
        column="location_id",
        help_text="Pick from active locations.",
        sort_order=80,
    ),
    CatalogEntry(
        key="qr_code",
        label="QR code",
        type=FieldType.TEXT,
        column="qr_code",
        sort_order=90,
    ),
    CatalogEntry(
        key="notes",
        label="Notes",
        type=FieldType.TEXT,
        column="notes",
        sort_order=100,
    ),
    # Standardised fields promoted from the previous "field_value" path. Each
    # has a dedicated nullable column on the ``items`` table (migration 0024).
    # Unit cost is intentionally NOT here — FIFO cost layers are the source of
    # truth for cost. The items list/CSV reads the open-layer unit_cost.
    CatalogEntry(
        key="ring_size",
        label="Ring size",
        type=FieldType.TEXT,
        column="ring_size",
        sort_order=200,
    ),
    CatalogEntry(
        key="weight_grams",
        label="Weight (g)",
        type=FieldType.DECIMAL,
        column="weight_grams",
        sort_order=210,
    ),
    CatalogEntry(
        key="stone_shape",
        label="Stone shape",
        type=FieldType.TEXT,
        column="stone_shape",
        sort_order=220,
    ),
    # Stone-master integration (S1 of architectural additions spec).
    # ``centre_stone_id`` is an FK into ``stones`` — rendered as a linked
    # picker in a future slice (no UI yet; the column is column-backed and
    # round-trips like any other NUMBER catalog entry). Melee fields are
    # aggregate carriers on the parent item for stones too small to track
    # individually.
    CatalogEntry(
        key="centre_stone_id",
        label="Centre stone",
        type=FieldType.NUMBER,
        column="centre_stone_id",
        help_text="Pick from active stones.",
        sort_order=230,
    ),
    CatalogEntry(
        key="melee_count",
        label="Melee count",
        type=FieldType.NUMBER,
        column="melee_count",
        sort_order=240,
    ),
    CatalogEntry(
        key="melee_total_ct",
        label="Melee total (ct)",
        type=FieldType.DECIMAL,
        column="melee_total_ct",
        sort_order=250,
    ),
    CatalogEntry(
        key="melee_stone_type",
        label="Melee stone type",
        type=FieldType.TEXT,
        column="melee_stone_type",
        sort_order=260,
    ),
    # Metal master integration (S2 of architectural additions spec). Both
    # are FKs to ``metal_master`` rendered as pickers in a future slice;
    # the columns are column-backed and round-trip like ``supplier_id``.
    # ``pure_metal_weight_g`` is derived (not user-editable) so it doesn't
    # get a catalog entry.
    CatalogEntry(
        key="metal_id",
        label="Metal",
        type=FieldType.NUMBER,
        column="metal_id",
        help_text="Pick from active metals.",
        sort_order=300,
    ),
    CatalogEntry(
        key="secondary_metal_id",
        label="Secondary metal",
        type=FieldType.NUMBER,
        column="secondary_metal_id",
        help_text="For two-tone pieces; otherwise leave blank.",
        sort_order=310,
    ),
    # ------------------------------------------------------------------
    # S4 side-table-backed catalog entries (spec §9). One entry per
    # non-FK column on each side table. Catalog keys are globally unique
    # so column names that collide across tables get a category prefix
    # (``earring_closure_type`` vs ``chain_closure_type``). FK pickers
    # (``pairs_with_*``, ``default_chain_item_id``) are deferred — they
    # need linked-picker UI that doesn't exist yet.
    #
    # SELECT options must match the corresponding enum's wire values
    # verbatim — see ``app/models.py`` for the source of truth on each.
    # ------------------------------------------------------------------

    # --- item_ring_attrs ---
    # ``ring_size`` already exists as an ITEM_COLUMN entry (freetext on
    # ``items``) and stays for back-compat; the side-table numeric version
    # lives under a distinct key so both can coexist during migration.
    CatalogEntry(
        key="ring_size_numeric",
        label="Ring size (numeric)",
        type=FieldType.DECIMAL,
        sort_order=400,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="ring_size",
    ),
    CatalogEntry(
        key="ring_size_standard",
        label="Ring-size standard",
        type=FieldType.SELECT,
        options=("us", "au_uk", "eu"),
        sort_order=401,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="size_standard",
    ),
    CatalogEntry(
        key="band_width_mm",
        label="Band width (mm)",
        type=FieldType.DECIMAL,
        sort_order=410,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="band_width_mm",
    ),
    CatalogEntry(
        key="band_depth_mm",
        label="Band depth (mm)",
        type=FieldType.DECIMAL,
        sort_order=411,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="band_depth_mm",
    ),
    CatalogEntry(
        key="band_profile",
        label="Band profile",
        type=FieldType.SELECT,
        options=(
            "court", "d_shape", "flat", "flat_court", "halfround",
            "knife_edge", "cathedral", "euro_shank",
        ),
        sort_order=412,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="profile",
    ),
    CatalogEntry(
        key="band_finish",
        label="Finish",
        type=FieldType.SELECT,
        options=(
            "polished", "matte", "brushed", "hammered", "milgrain", "sandblast",
        ),
        sort_order=413,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="finish",
    ),
    CatalogEntry(
        key="comfort_fit",
        label="Comfort fit",
        type=FieldType.BOOLEAN,
        sort_order=414,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="comfort_fit",
    ),
    CatalogEntry(
        key="shank_style",
        label="Shank style",
        type=FieldType.SELECT,
        options=("solid", "split", "twisted", "pave_set", "plain"),
        sort_order=415,
        storage=Storage.SIDE_TABLE,
        side_table="item_ring_attrs",
        side_column="shank_style",
    ),

    # --- item_engagement_attrs ---
    CatalogEntry(
        key="setting_style",
        label="Setting style",
        type=FieldType.SELECT,
        options=(
            "solitaire", "halo", "hidden_halo", "three_stone", "trilogy",
            "cluster", "vintage", "bezel", "tension",
        ),
        sort_order=500,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="setting_style",
    ),
    CatalogEntry(
        key="setting_variation",
        label="Setting variation",
        type=FieldType.TEXT,
        sort_order=501,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="setting_variation",
    ),
    CatalogEntry(
        key="prong_count",
        label="Prong count",
        type=FieldType.NUMBER,
        sort_order=502,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="prong_count",
    ),
    CatalogEntry(
        key="prong_style",
        label="Prong style",
        type=FieldType.SELECT,
        options=("round", "claw", "v_tip", "double_claw"),
        sort_order=503,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="prong_style",
    ),
    CatalogEntry(
        key="gallery_style",
        label="Gallery style",
        type=FieldType.SELECT,
        options=("open", "closed", "filigree"),
        sort_order=504,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="gallery_style",
    ),
    CatalogEntry(
        key="under_bezel",
        label="Under bezel",
        type=FieldType.BOOLEAN,
        sort_order=505,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="under_bezel",
    ),
    CatalogEntry(
        key="mount_price",
        label="Mount price",
        type=FieldType.DECIMAL,
        help_text="Mount cost less the centre stone.",
        sort_order=506,
        storage=Storage.SIDE_TABLE,
        side_table="item_engagement_attrs",
        side_column="mount_price",
    ),

    # --- item_band_attrs ---
    CatalogEntry(
        key="band_set_style",
        label="Band set style",
        type=FieldType.SELECT,
        options=(
            "plain", "channel_set", "pave", "eternity", "half_eternity",
            "mixed_metal",
        ),
        sort_order=600,
        storage=Storage.SIDE_TABLE,
        side_table="item_band_attrs",
        side_column="band_set_style",
    ),
    CatalogEntry(
        key="matching_set_id",
        label="Matching-set code",
        type=FieldType.TEXT,
        help_text="Optional grouping code for his/hers/ours sets.",
        sort_order=601,
        storage=Storage.SIDE_TABLE,
        side_table="item_band_attrs",
        side_column="matching_set_id",
    ),

    # --- item_earring_attrs ---
    CatalogEntry(
        key="earring_sold_as",
        label="Earring sold as",
        type=FieldType.SELECT,
        options=("pair", "single"),
        sort_order=700,
        storage=Storage.SIDE_TABLE,
        side_table="item_earring_attrs",
        side_column="sold_as",
    ),
    CatalogEntry(
        key="earring_closure_type",
        label="Earring closure",
        type=FieldType.SELECT,
        options=(
            "butterfly", "screw_back", "lever_back", "hook", "french_wire",
            "clip", "huggie",
        ),
        sort_order=701,
        storage=Storage.SIDE_TABLE,
        side_table="item_earring_attrs",
        side_column="closure_type",
    ),
    CatalogEntry(
        key="earring_style",
        label="Earring style",
        type=FieldType.SELECT,
        options=(
            "stud", "drop", "hoop", "chandelier", "huggie", "threader",
            "climber",
        ),
        sort_order=702,
        storage=Storage.SIDE_TABLE,
        side_table="item_earring_attrs",
        side_column="style",
    ),
    CatalogEntry(
        key="earring_drop_length_mm",
        label="Drop length (mm)",
        type=FieldType.DECIMAL,
        sort_order=703,
        storage=Storage.SIDE_TABLE,
        side_table="item_earring_attrs",
        side_column="drop_length_mm",
    ),
    CatalogEntry(
        key="earring_hoop_diameter_mm",
        label="Hoop diameter (mm)",
        type=FieldType.DECIMAL,
        sort_order=704,
        storage=Storage.SIDE_TABLE,
        side_table="item_earring_attrs",
        side_column="hoop_diameter_mm",
    ),

    # --- item_chain_attrs ---
    CatalogEntry(
        key="chain_style",
        label="Chain style",
        type=FieldType.SELECT,
        options=(
            "cable", "curb", "box", "rope", "snake", "figaro", "belcher",
            "wheat", "singapore", "herringbone",
        ),
        sort_order=800,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="chain_style",
    ),
    CatalogEntry(
        key="chain_length_mm",
        label="Chain length (mm)",
        type=FieldType.DECIMAL,
        sort_order=801,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="length_mm",
    ),
    CatalogEntry(
        key="chain_adjustable",
        label="Adjustable",
        type=FieldType.BOOLEAN,
        sort_order=802,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="adjustable",
    ),
    CatalogEntry(
        key="chain_min_length_mm",
        label="Minimum length (mm)",
        type=FieldType.DECIMAL,
        sort_order=803,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="min_length_mm",
    ),
    CatalogEntry(
        key="chain_max_length_mm",
        label="Maximum length (mm)",
        type=FieldType.DECIMAL,
        sort_order=804,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="max_length_mm",
    ),
    CatalogEntry(
        key="chain_link_width_mm",
        label="Link width (mm)",
        type=FieldType.DECIMAL,
        sort_order=805,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="link_width_mm",
    ),
    CatalogEntry(
        key="chain_closure_type",
        label="Chain closure",
        type=FieldType.SELECT,
        options=(
            "lobster", "spring_ring", "box", "toggle", "s_hook", "barrel",
            "magnetic",
        ),
        sort_order=806,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="closure_type",
    ),
    CatalogEntry(
        key="chain_worn_as",
        label="Worn as",
        type=FieldType.SELECT,
        options=("necklace", "bracelet", "anklet"),
        sort_order=807,
        storage=Storage.SIDE_TABLE,
        side_table="item_chain_attrs",
        side_column="worn_as",
    ),

    # --- item_pendant_attrs ---
    CatalogEntry(
        key="pendant_length_mm",
        label="Pendant length (mm)",
        type=FieldType.DECIMAL,
        sort_order=900,
        storage=Storage.SIDE_TABLE,
        side_table="item_pendant_attrs",
        side_column="length_mm",
    ),
    CatalogEntry(
        key="pendant_width_mm",
        label="Pendant width (mm)",
        type=FieldType.DECIMAL,
        sort_order=901,
        storage=Storage.SIDE_TABLE,
        side_table="item_pendant_attrs",
        side_column="width_mm",
    ),
    CatalogEntry(
        key="pendant_bail_type",
        label="Bail type",
        type=FieldType.SELECT,
        options=("fixed", "hinged", "hidden", "enhancer"),
        sort_order=902,
        storage=Storage.SIDE_TABLE,
        side_table="item_pendant_attrs",
        side_column="bail_type",
    ),
    CatalogEntry(
        key="pendant_bail_opening_mm",
        label="Bail opening (mm)",
        type=FieldType.DECIMAL,
        sort_order=903,
        storage=Storage.SIDE_TABLE,
        side_table="item_pendant_attrs",
        side_column="bail_opening_mm",
    ),
    CatalogEntry(
        key="pendant_includes_chain",
        label="Includes chain",
        type=FieldType.BOOLEAN,
        sort_order=904,
        storage=Storage.SIDE_TABLE,
        side_table="item_pendant_attrs",
        side_column="includes_chain",
    ),

    # --- item_engraving_attrs ---
    CatalogEntry(
        key="engraving_available",
        label="Engraving available",
        type=FieldType.BOOLEAN,
        sort_order=1000,
        storage=Storage.SIDE_TABLE,
        side_table="item_engraving_attrs",
        side_column="engraving_available",
    ),
    CatalogEntry(
        key="engraving_max_chars_outside",
        label="Max chars (outside)",
        type=FieldType.NUMBER,
        sort_order=1001,
        storage=Storage.SIDE_TABLE,
        side_table="item_engraving_attrs",
        side_column="max_chars_outside",
    ),
    CatalogEntry(
        key="engraving_max_chars_inside",
        label="Max chars (inside)",
        type=FieldType.NUMBER,
        sort_order=1002,
        storage=Storage.SIDE_TABLE,
        side_table="item_engraving_attrs",
        side_column="max_chars_inside",
    ),
    CatalogEntry(
        key="engraving_text",
        label="Engraving text",
        type=FieldType.TEXT,
        sort_order=1003,
        storage=Storage.SIDE_TABLE,
        side_table="item_engraving_attrs",
        side_column="engraving_text",
    ),
    CatalogEntry(
        key="engraving_font",
        label="Engraving font",
        type=FieldType.TEXT,
        sort_order=1004,
        storage=Storage.SIDE_TABLE,
        side_table="item_engraving_attrs",
        side_column="engraving_font",
    ),
    CatalogEntry(
        key="engraving_style",
        label="Engraving style",
        type=FieldType.SELECT,
        options=("machine", "hand", "laser"),
        sort_order=1005,
        storage=Storage.SIDE_TABLE,
        side_table="item_engraving_attrs",
        side_column="engraving_style",
    ),
)


CATALOG_BY_KEY: Final[dict[str, CatalogEntry]] = {e.key: e for e in FIELD_CATALOG}


def get_entry(key: str) -> CatalogEntry | None:
    """Return the catalog entry for ``key`` or ``None`` if it has been removed."""
    return CATALOG_BY_KEY.get(key)
