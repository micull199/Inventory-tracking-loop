"""Default visibility for built-in item-form fields.

Slice 6 of the catalog-driven taxonomy refactor removed per-leaf
overrides — every node now uses the same defaults. The module survives
as a thin compatibility shim because ``app.items`` route handlers pass
the resulting dict through to the form template, and inlining that
constant at every callsite would just be churn.

Two things follow from the deletion of the override:

1. ``effective_field_visibility(node)`` no longer reads the database. It
   ignores ``node`` entirely and returns the defaults. The parameter is
   retained so callers don't have to be touched.
2. ``TaxonomyNode.field_visibility_json`` is dropped in migration 0023.
"""

from __future__ import annotations

from typing import Final

from app.models import TaxonomyNode

# Field-name strings mirror the items form's ``name=`` attributes and the
# items route's ``Form(...)`` parameter names. Keep in sync with both.
BUILT_IN_FIELDS: Final[tuple[str, ...]] = (
    "name",
    "unit",
    "tracking_mode",
    "requires_checkout",
    "reorder_threshold",
    "reorder_qty",
    "supplier_id",
    "location_id",
    "qr_code",
)

# ``name`` and ``unit`` are DB NOT NULL on ``Item``, so they stay required
# at form level — without them the route would 400 anyway. Everything else
# is optional. SKU is auto-allocated and handled as a structural field
# elsewhere; it's never in this dict.
_DEFAULT_VISIBILITY: Final[dict[str, str]] = {
    "name": "required",
    "unit": "required",
    "tracking_mode": "optional",
    "requires_checkout": "optional",
    "reorder_threshold": "optional",
    "reorder_qty": "optional",
    "supplier_id": "optional",
    "location_id": "optional",
    "qr_code": "optional",
}


def effective_field_visibility(node: TaxonomyNode | None) -> dict[str, str]:
    """Return the default visibility map. ``node`` is ignored.

    Pre-slice-6 this looked up ``node.field_visibility_json`` and overlaid
    the defaults; that column is gone and the override mechanism with it.
    """

    return dict(_DEFAULT_VISIBILITY)


def is_hidden(visibility: dict[str, str], field: str) -> bool:
    return visibility.get(field) == "hidden"


def is_required(visibility: dict[str, str], field: str) -> bool:
    return visibility.get(field) == "required"
