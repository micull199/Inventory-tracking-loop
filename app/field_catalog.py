"""Hardcoded catalog of fields a category can pick from.

A taxonomy node defines its schema by *picking* entries from this catalog,
rather than typing free-text field names. Each picked entry becomes a
``TaxonomyFieldDef`` row whose ``catalog_key`` points back here.

Two storage targets:

- ``"column"``: the value lives on an existing ``Item`` column (e.g. ``name``,
  ``unit``, ``supplier_id``). Picking the entry opts the category into using
  that column on the item form / list / CSV. Items in categories that did not
  pick the entry leave the column at its default (NULL or 0).
- ``"field_value"``: the value lives in ``item_field_values`` as a sparse row
  keyed by the def's id. Mirrors the pre-catalog "custom field" mechanic.

The catalog is closed: adding an entry is a code change + a migration + tests.
That is deliberate — it forces a deliberate review (audit-log shape, CSV
column meaning, etc.) for every new field, instead of letting unstructured
drift accumulate in user-typed names.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from app.models import FieldType

Storage = Literal["column", "field_value"]


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One field a category may pick.

    ``key`` is the stable identifier the rest of the app uses — also written to
    ``TaxonomyFieldDef.catalog_key`` and to the materialised
    ``TaxonomyFieldDef.key`` at pick time. ``label`` is the display name. The
    pair ``(type, options)`` defines the input shape; ``options`` is empty
    unless ``type`` is ``SELECT`` or ``MULTISELECT``. ``storage`` and
    (for ``column``) ``column`` control how the value is read/written.
    """

    key: str
    label: str
    type: FieldType
    storage: Storage
    column: str | None = None
    options: tuple[str, ...] = ()
    help_text: str | None = None
    sort_order: int = 0


# Tracking mode semantics (``unique`` forces qty=1, disables reorder logic) are
# enforced in ``app/items.py`` create/edit handlers — the catalog only declares
# the input shape.
FIELD_CATALOG: Final[tuple[CatalogEntry, ...]] = (
    # Column-backed entries — values live on the items table.
    CatalogEntry(
        key="name",
        label="Name",
        type=FieldType.TEXT,
        storage="column",
        column="name",
        sort_order=10,
    ),
    CatalogEntry(
        key="unit",
        label="Unit of measure",
        type=FieldType.TEXT,
        storage="column",
        column="unit",
        sort_order=20,
    ),
    CatalogEntry(
        key="tracking_mode",
        label="Tracking mode",
        type=FieldType.SELECT,
        storage="column",
        column="tracking_mode",
        options=("qty", "unique"),
        help_text="`unique` forces qty=1 and disables reorder logic.",
        sort_order=30,
    ),
    CatalogEntry(
        key="requires_checkout",
        label="Requires checkout",
        type=FieldType.BOOLEAN,
        storage="column",
        column="requires_checkout",
        sort_order=40,
    ),
    CatalogEntry(
        key="reorder_threshold",
        label="Reorder threshold",
        type=FieldType.DECIMAL,
        storage="column",
        column="reorder_threshold",
        sort_order=50,
    ),
    CatalogEntry(
        key="reorder_qty",
        label="Reorder qty",
        type=FieldType.DECIMAL,
        storage="column",
        column="reorder_qty",
        sort_order=60,
    ),
    CatalogEntry(
        key="supplier_id",
        label="Supplier",
        type=FieldType.NUMBER,
        storage="column",
        column="supplier_id",
        help_text="Pick from active suppliers.",
        sort_order=70,
    ),
    CatalogEntry(
        key="location_id",
        label="Location",
        type=FieldType.NUMBER,
        storage="column",
        column="location_id",
        help_text="Pick from active locations.",
        sort_order=80,
    ),
    CatalogEntry(
        key="qr_code",
        label="QR code",
        type=FieldType.TEXT,
        storage="column",
        column="qr_code",
        sort_order=90,
    ),
    CatalogEntry(
        key="notes",
        label="Notes",
        type=FieldType.TEXT,
        storage="column",
        column="notes",
        sort_order=100,
    ),
    # Field-value-backed entries — values live in item_field_values.
    CatalogEntry(
        key="karat",
        label="Karat",
        type=FieldType.SELECT,
        storage="field_value",
        options=("9ct", "14ct", "18ct", "22ct", "24ct"),
        sort_order=200,
    ),
    CatalogEntry(
        key="weight_grams",
        label="Weight (g)",
        type=FieldType.DECIMAL,
        storage="field_value",
        sort_order=210,
    ),
    CatalogEntry(
        key="material",
        label="Material",
        type=FieldType.SELECT,
        storage="field_value",
        options=("Silver", "Gold", "Platinum", "Brass", "Steel"),
        sort_order=220,
    ),
    CatalogEntry(
        key="purity_pct",
        label="Purity %",
        type=FieldType.DECIMAL,
        storage="field_value",
        sort_order=230,
    ),
    CatalogEntry(
        key="hallmark",
        label="Hallmark",
        type=FieldType.TEXT,
        storage="field_value",
        sort_order=240,
    ),
    CatalogEntry(
        key="ring_size",
        label="Ring size",
        type=FieldType.TEXT,
        storage="field_value",
        sort_order=250,
    ),
    CatalogEntry(
        key="gem_type",
        label="Gem type",
        type=FieldType.SELECT,
        storage="field_value",
        options=("Diamond", "Sapphire", "Ruby", "Emerald", "Opal", "Pearl", "Other"),
        sort_order=260,
    ),
    CatalogEntry(
        key="finishes",
        label="Finishes",
        type=FieldType.MULTISELECT,
        storage="field_value",
        options=("Matte", "Polished", "Satin", "Brushed", "Hammered", "Antiqued"),
        sort_order=270,
    ),
    CatalogEntry(
        key="expiry_date",
        label="Expiry date",
        type=FieldType.DATE,
        storage="field_value",
        help_text="For consumables with a shelf life.",
        sort_order=280,
    ),
)


CATALOG_BY_KEY: Final[dict[str, CatalogEntry]] = {e.key: e for e in FIELD_CATALOG}


def get_entry(key: str) -> CatalogEntry | None:
    """Return the catalog entry for ``key`` or ``None`` if it has been removed.

    Returning ``None`` (rather than raising) lets callers handle archived /
    legacy catalog keys defensively — useful while migration 0022 backfills.
    """

    return CATALOG_BY_KEY.get(key)
