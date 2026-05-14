"""Hardcoded catalog of standard fields on every item.

A taxonomy node *picks* entries from this catalog to opt its items' form +
list + CSV views into showing those fields. Every entry maps 1:1 to a real
column on the ``items`` table — there is no "custom field" storage path.
Adding a field is a code change + a migration: edit ``FIELD_CATALOG`` here,
add the column on ``Item``, write an Alembic migration. That is deliberate
— it forces a deliberate review (audit-log shape, CSV column meaning, etc.)
for every new field, instead of letting unstructured drift accumulate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.models import FieldType


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One field a category may pick.

    ``key`` is the stable identifier the rest of the app uses (and the form
    input name + CSV column header). ``column`` is the matching attribute on
    the ``Item`` model. ``label`` is the display name. The pair
    ``(type, options)`` defines the input shape; ``options`` is empty unless
    ``type`` is ``SELECT`` or ``MULTISELECT``.
    """

    key: str
    label: str
    type: FieldType
    column: str
    options: tuple[str, ...] = ()
    help_text: str | None = None
    sort_order: int = 0


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
)


CATALOG_BY_KEY: Final[dict[str, CatalogEntry]] = {e.key: e for e in FIELD_CATALOG}


def get_entry(key: str) -> CatalogEntry | None:
    """Return the catalog entry for ``key`` or ``None`` if it has been removed."""
    return CATALOG_BY_KEY.get(key)
