"""Per-leaf visibility rules for built-in item-form fields.

A Manager can mark each built-in field as ``required``, ``optional``, or
``hidden`` on a taxonomy leaf via the Fields admin page. The items form
respects these on render (hide hidden, drop required attr on optional) and
the items POST handler respects them on submit (ignore submitted values for
hidden fields, skip required-string validation for optional ones,
auto-fill DB-required fields that are hidden).

Persistence: ``TaxonomyNode.field_visibility_json`` — JSON dict mapping
field name → state string. Absent keys / null column fall back to
``_DEFAULT_VISIBILITY`` below.

Categories outside the manager's surface (``sku``, ``taxonomy_node_id``) are
not configurable. SKU auto-generates from the leaf prefix; category is the
selector for the whole form, so toggling it would be incoherent.
"""

from __future__ import annotations

from typing import Final

from app.models import TaxonomyNode

# Field-name strings here mirror the items form's ``name=`` attributes and
# the items route's ``Form(...)`` parameter names. Keep in sync with both.
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

VISIBILITY_STATES: Final[tuple[str, ...]] = ("required", "optional", "hidden")

# Defaults match the current items form's behaviour pre-visibility: ``name``
# and ``unit`` are required (DB NOT NULL), everything else is optional. A
# Manager can override these per-leaf via the Fields admin page.
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
    """Resolve the effective visibility map for ``node``.

    Returns a dict with every key in ``BUILT_IN_FIELDS``. The stored
    ``field_visibility_json`` overlays the defaults; unknown keys and
    invalid values are ignored (defensive against hand-edited rows).
    """
    out = dict(_DEFAULT_VISIBILITY)
    if node is None:
        return out
    stored = node.field_visibility_json
    if not stored:
        return out
    for key in BUILT_IN_FIELDS:
        raw = stored.get(key)
        if isinstance(raw, str) and raw in VISIBILITY_STATES:
            out[key] = raw
    return out


def is_hidden(visibility: dict[str, str], field: str) -> bool:
    return visibility.get(field) == "hidden"


def is_required(visibility: dict[str, str], field: str) -> bool:
    return visibility.get(field) == "required"


def validate_visibility_submission(form: dict[str, str]) -> dict[str, str]:
    """Coerce a flat form submission into a visibility dict.

    Inputs from the admin form arrive as ``visibility_<field>`` keys (e.g.
    ``visibility_name=required``). Unknown fields and invalid states are
    silently dropped — the resulting dict is what gets stored on
    ``field_visibility_json``. An all-defaults dict still gets stored so
    the round-trip is observable in audit logs.
    """
    out: dict[str, str] = {}
    for key in BUILT_IN_FIELDS:
        raw = form.get(f"visibility_{key}", "")
        if raw in VISIBILITY_STATES:
            out[key] = raw
        else:
            out[key] = _DEFAULT_VISIBILITY[key]
    return out
