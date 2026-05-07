"""Item CRUD against a taxonomy *leaf* node (I1a + I1b + I2).

Items are the unblocking primitive for everything in MISSION §3 from "Stock
movements" onward. The shipped fields: SKU, name, leaf-node, unit, tracking
mode, requires-checkout flag, reorder thresholds, optional supplier/location/
QR/notes, plus the **custom field values** inherited from the leaf node's
schema (I2). ``current_qty`` is read-only at 0; only stock movements (M1+)
move it. Unique-tracked per-unit rows (I3), QR label generation (I4), and
movements (M1+) are deferred.

Custom fields (I2): each item inherits the active field defs of its leaf
node (see ``app/field_defs.py``). Values are stored sparsely in
``item_field_values`` (one row per (item, field def) with a non-null value)
and rendered + validated by ``app/items.py`` directly — no extra route layer.
Required defs raise 400 if blank; type-coercion failures (bad date, non-int
in a number field, out-of-options select) also 400. Archived defs are
preserved on the item but invisible to the form: edits leave them untouched
("Deleting a field hides it from new entry but preserves the value", per
MISSION §3). Boolean ``required`` is interpreted as "must be checked", since
an unchecked box is a definite "False" answer rather than a missing one — a
"required" boolean would otherwise be meaningless.

Access (I1b refines I1a):
- **Manager / Admin**: full access — list, create, edit (every field),
  archive, unarchive.
- **Office**: list + edit only. Cannot create. Cannot archive. Cannot change
  ``reorder_threshold`` / ``reorder_qty`` (MISSION §3: "cannot change reorder
  thresholds"). Threshold inputs are hidden from the form and the route
  silently overrides any inbound values with the existing row's values
  before validation, so Office submissions can't even *fail* on those
  fields — they're inert.
- **Workshop**: 403 across the board for now. Read-only access per §3
  ("view items") is deferred to a follow-up slice.

URL shape mirrors ``app/suppliers.py`` / ``app/locations.py`` — flat-by-id —
because items don't have a parent in the URL the way sub-cats do; the leaf
node is just one of the form fields.

Archived-FK preservation: when an item references a now-archived supplier,
location, or leaf node, the form keeps that row in the dropdown with an
"(archived)" suffix and the validators accept it *unchanged*. Switching to
a *different* archived FK is still rejected. Clearing an optional FK
(``supplier_id``/``location_id`` blank) is allowed even if the existing one
is archived — that's an explicit user action, not silent data loss.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.datastructures import FormData
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import (
    FieldType,
    Item,
    ItemFieldValue,
    Location,
    Role,
    Supplier,
    TaxonomyFieldDef,
    TaxonomyNode,
    TrackingMode,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/items", tags=["items"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Audit-diff vocabulary for ``item.updated``. Order is the order ``after_json``
# entries are written when this list is iterated; keep stable so audit history
# is greppable. ``current_qty`` is intentionally NOT in this list — it never
# changes via the route, and once movements land it'll have its own audit
# vocabulary (``movement.in`` etc., not ``item.updated``).
_FIELDS: tuple[str, ...] = (
    "sku",
    "name",
    "taxonomy_node_id",
    "unit",
    "tracking_mode",
    "requires_checkout",
    "reorder_threshold",
    "reorder_qty",
    "supplier_id",
    "location_id",
    "qr_code",
    "notes",
)


def _parse_decimal(raw: str, *, field_name: str) -> Decimal:
    """Parse a non-negative decimal; blank → 0; reject negatives + garbage."""
    text = (raw or "").strip()
    if text == "":
        return Decimal("0")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot be negative",
        )
    return value


def _coerce_tracking_mode(raw: str) -> TrackingMode:
    try:
        return TrackingMode(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown tracking mode",
        ) from exc


def _has_active_children(db: Session, node_id: int) -> bool:
    stmt = (
        select(TaxonomyNode.id)
        .where(TaxonomyNode.parent_id == node_id)
        .where(TaxonomyNode.archived_at.is_(None))
    )
    return db.execute(stmt).first() is not None


def _is_leaf(db: Session, node: TaxonomyNode) -> bool:
    """Sub-cats are always leaves; top-level nodes are leaves iff no active children.

    Mirrors ``app.field_defs._is_leaf`` — duplicated rather than imported to
    keep the cross-module dependency graph one-way (taxonomy → field_defs;
    field_defs is the schema-shape module). Both implementations must move
    in lockstep; if a third caller appears, extract.
    """
    if node.parent_id is not None:
        return True
    return not _has_active_children(db, node.id)


def _resolve_leaf_node(
    db: Session, raw_node_id: str, *, current_id: int | None = None
) -> TaxonomyNode:
    """Load a taxonomy node by id and verify it's a non-archived leaf.

    ``current_id`` is the item's existing ``taxonomy_node_id`` on edit (None
    on create). If the user submits the same id and that node is archived,
    accept the unchanged assignment so editing other fields doesn't force a
    category change. Switching to any *different* archived id still 400s.
    The leaf-rule check applies regardless: an archived top-level node with
    active children would still 400 even if it's the current value, but
    that's a degenerate state we can't construct (active children require
    an active parent in the route layer).
    """
    text = (raw_node_id or "").strip()
    if text == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category is required",
        )
    try:
        node_id = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category is invalid",
        ) from exc
    node = db.get(TaxonomyNode, node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category not found",
        )
    if node.archived_at is not None and node.id != current_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot move items to an archived category",
        )
    if not _is_leaf(db, node):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "category has sub-categories — pick one of its sub-categories "
                "instead"
            ),
        )
    return node


def _resolve_optional_supplier(
    db: Session, raw: str, *, current_id: int | None = None
) -> int | None:
    """Parse a supplier id; allow keeping an existing archived supplier unchanged.

    Blank → None (clears the link, even if the previous value was archived —
    that's an explicit user choice). Otherwise the supplier must exist; if
    it's archived, ``current_id`` must match (preserving the link), else 400.
    """
    text = (raw or "").strip()
    if text == "":
        return None
    try:
        supplier_id = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier is invalid",
        ) from exc
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier not found",
        )
    if supplier.archived_at is not None and supplier.id != current_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier is archived",
        )
    return supplier.id


def _resolve_optional_location(
    db: Session, raw: str, *, current_id: int | None = None
) -> int | None:
    """Parse a location id; allow keeping an existing archived location unchanged.

    Same contract as ``_resolve_optional_supplier``.
    """
    text = (raw or "").strip()
    if text == "":
        return None
    try:
        location_id = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="location is invalid",
        ) from exc
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="location not found",
        )
    if location.archived_at is not None and location.id != current_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="location is archived",
        )
    return location.id


def _normalise(
    db: Session,
    *,
    sku: str,
    name: str,
    taxonomy_node_id: str,
    unit: str,
    tracking_mode: str,
    requires_checkout: bool,
    reorder_threshold: str,
    reorder_qty: str,
    supplier_id: str,
    location_id: str,
    qr_code: str,
    notes: str,
    current_node_id: int | None = None,
    current_supplier_id: int | None = None,
    current_location_id: int | None = None,
) -> dict[str, Any]:
    """Strip / parse / validate every form field. Returns the value-shape stored on the row.

    Raises ``HTTPException(400)`` on any validation error. Uniqueness checks
    against the DB (sku / qr_code) live in their own callers because they need
    the per-route ``exclude_id``.

    The ``current_*`` ids are the existing item's FK values on edit (all
    ``None`` on create). Used by the FK resolvers to keep an unchanged
    archived FK assignment without 400ing.
    """
    clean_sku = (sku or "").strip()
    if not clean_sku:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="SKU is required"
        )
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    clean_unit = (unit or "").strip()
    if not clean_unit:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unit is required",
        )

    node = _resolve_leaf_node(db, taxonomy_node_id, current_id=current_node_id)
    mode = _coerce_tracking_mode(tracking_mode)

    threshold = _parse_decimal(reorder_threshold, field_name="reorder threshold")
    qty = _parse_decimal(reorder_qty, field_name="reorder quantity")

    sup_id = _resolve_optional_supplier(
        db, supplier_id, current_id=current_supplier_id
    )
    loc_id = _resolve_optional_location(
        db, location_id, current_id=current_location_id
    )

    clean_qr = (qr_code or "").strip() or None
    clean_notes = (notes or "").strip() or None

    return {
        "sku": clean_sku,
        "name": clean_name,
        "taxonomy_node_id": node.id,
        "unit": clean_unit,
        "tracking_mode": mode,
        "requires_checkout": bool(requires_checkout),
        "reorder_threshold": threshold,
        "reorder_qty": qty,
        "supplier_id": sup_id,
        "location_id": loc_id,
        "qr_code": clean_qr,
        "notes": clean_notes,
    }


def _check_sku_unique(
    db: Session, sku: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Item.id).where(Item.sku == sku)
    if exclude_id is not None:
        stmt = stmt.where(Item.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="an item with that SKU already exists",
        )


def _check_qr_unique(
    db: Session, qr_code: str | None, *, exclude_id: int | None = None
) -> None:
    if qr_code is None:
        return
    stmt = select(Item.id).where(Item.qr_code == qr_code)
    if exclude_id is not None:
        stmt = stmt.where(Item.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="an item with that QR code already exists",
        )


def _diff(
    item: Item, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(item, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _form_for_item(item: Item | None) -> dict[str, Any]:
    """Render the form-input shape for a fresh form or an existing item."""
    if item is None:
        return {
            "sku": "",
            "name": "",
            "taxonomy_node_id": "",
            "unit": "",
            "tracking_mode": TrackingMode.QTY.value,
            "requires_checkout": False,
            "reorder_threshold": "",
            "reorder_qty": "",
            "supplier_id": "",
            "location_id": "",
            "qr_code": "",
            "notes": "",
        }
    return {
        "sku": item.sku,
        "name": item.name,
        "taxonomy_node_id": str(item.taxonomy_node_id),
        "unit": item.unit,
        "tracking_mode": item.tracking_mode.value,
        "requires_checkout": item.requires_checkout,
        "reorder_threshold": str(item.reorder_threshold),
        "reorder_qty": str(item.reorder_qty),
        "supplier_id": str(item.supplier_id) if item.supplier_id is not None else "",
        "location_id": str(item.location_id) if item.location_id is not None else "",
        "qr_code": item.qr_code or "",
        "notes": item.notes or "",
    }


_DEFAULT_KEYS_TO_FORM: dict[str, str] = {
    "unit": "unit",
    "tracking_mode": "tracking_mode",
    "reorder_threshold": "reorder_threshold",
    "reorder_qty": "reorder_qty",
    "supplier_id": "supplier_id",
    "location_id": "location_id",
}


def _apply_leaf_defaults(
    form: dict[str, Any], leaf: TaxonomyNode | None
) -> None:
    """Pre-fill ``form`` with the leaf's ``defaults_json`` (in-place).

    Only writes a key when the form's current value is "empty" (str
    "" / False) — never overwrites a user-typed value already in the form.
    The leaf-defaults feature is a UX accelerator on the create flow; on
    edit the existing item's stored values are authoritative and this
    helper isn't called.

    Decimal-valued defaults (reorder_threshold / reorder_qty) round-trip
    via str() so the form input renders the canonical string the user
    typed when defining the default. Bool ``requires_checkout`` only
    populates from the dict when explicitly True (matches the audit-diff
    convention in ``app.taxonomy``).
    """
    if leaf is None or not leaf.defaults_json:
        return
    src = leaf.defaults_json
    for src_key, form_key in _DEFAULT_KEYS_TO_FORM.items():
        if src_key not in src:
            continue
        if form.get(form_key) in ("", None):
            form[form_key] = str(src[src_key])
    if src.get("requires_checkout") is True and not form.get("requires_checkout"):
        form["requires_checkout"] = True


def _generate_sku(db: Session, leaf: TaxonomyNode | None) -> str:
    """Generate a unique, predictable SKU.

    Format: ``<PREFIX>-<NNNN>`` where ``PREFIX`` is the first 3 alphanumeric
    chars of the leaf's name uppercased (or ``ITM`` if no leaf is supplied
    or the name yields no usable chars), and ``NNNN`` is a 4-digit
    zero-padded sequence — the next number not already taken under that
    prefix.

    Linear-scan collision check is fine for v1 volumes (a small workshop's
    item count is in the hundreds, not millions). If this ever needs to
    scale, replace the loop with ``MAX(SUBSTR(sku, ...))`` plus a single
    increment — the format keeps that path open.
    """
    if leaf is not None:
        candidate_prefix = "".join(
            ch for ch in leaf.name.upper() if ch.isalnum()
        )[:3]
        prefix = candidate_prefix or "ITM"
    else:
        prefix = "ITM"
    n = 1
    while True:
        sku = f"{prefix}-{n:04d}"
        if (
            db.execute(select(Item.id).where(Item.sku == sku)).first()
            is None
        ):
            return sku
        n += 1


def _leaf_options(
    db: Session, *, current_id: int | None = None
) -> list[dict[str, Any]]:
    """Active leaf-node options for the form's category <select>.

    Top-level nodes appear only when they have no active children (i.e. they
    *are* the leaf). Sub-cats appear under their parent. Output is shaped for
    the template — list of ``{id, label, is_group}`` dicts.

    If ``current_id`` references an archived node (or one not present in the
    active set), it's appended as a selectable option with an "(archived)"
    suffix and ``is_group=False`` so the user can keep the existing
    assignment without seeing a missing dropdown value.
    """
    rows = list(
        db.execute(
            select(TaxonomyNode)
            .where(TaxonomyNode.archived_at.is_(None))
            .order_by(TaxonomyNode.sort_order, TaxonomyNode.name)
        )
        .scalars()
        .all()
    )
    # Index children by parent id so we can render under each top-level node.
    children: dict[int, list[TaxonomyNode]] = {}
    tops: list[TaxonomyNode] = []
    for n in rows:
        if n.parent_id is None:
            tops.append(n)
        else:
            children.setdefault(n.parent_id, []).append(n)

    options: list[dict[str, Any]] = []
    rendered_ids: set[int] = set()
    for top in tops:
        kids = children.get(top.id, [])
        if kids:
            # Top with active children is NOT a leaf — render the parent as a
            # disabled group label, then each child as a selectable option
            # prefixed with its parent name for screen-reader clarity.
            options.append(
                {
                    "id": None,
                    "label": top.name,
                    "is_group": True,
                }
            )
            for kid in kids:
                options.append(
                    {
                        "id": kid.id,
                        "label": f"{top.name} / {kid.name}",
                        "is_group": False,
                    }
                )
                rendered_ids.add(kid.id)
        else:
            options.append(
                {"id": top.id, "label": top.name, "is_group": False}
            )
            rendered_ids.add(top.id)

    if current_id is not None and current_id not in rendered_ids:
        cur = db.get(TaxonomyNode, current_id)
        if cur is not None:
            if cur.parent_id is None:
                label = f"{cur.name} (archived)"
            else:
                parent = db.get(TaxonomyNode, cur.parent_id)
                parent_name = parent.name if parent is not None else "?"
                label = f"{parent_name} / {cur.name} (archived)"
            options.append(
                {"id": cur.id, "label": label, "is_group": False}
            )
    return options


def _supplier_options(
    db: Session, *, current_id: int | None = None
) -> list[dict[str, Any]]:
    """Active suppliers + the assigned archived row (with "(archived)" suffix) if any."""
    rows = list(
        db.execute(
            select(Supplier)
            .where(Supplier.archived_at.is_(None))
            .order_by(Supplier.name)
        )
        .scalars()
        .all()
    )
    options: list[dict[str, Any]] = [
        {"id": s.id, "label": s.name} for s in rows
    ]
    if current_id is not None and not any(
        opt["id"] == current_id for opt in options
    ):
        cur = db.get(Supplier, current_id)
        if cur is not None:
            options.append({"id": cur.id, "label": f"{cur.name} (archived)"})
    return options


def _location_options(
    db: Session, *, current_id: int | None = None
) -> list[dict[str, Any]]:
    """Active locations + the assigned archived row (with "(archived)" suffix) if any."""
    rows = list(
        db.execute(
            select(Location)
            .where(Location.archived_at.is_(None))
            .order_by(Location.name)
        )
        .scalars()
        .all()
    )
    options: list[dict[str, Any]] = [
        {"id": loc.id, "label": loc.name} for loc in rows
    ]
    if current_id is not None and not any(
        opt["id"] == current_id for opt in options
    ):
        cur = db.get(Location, current_id)
        if cur is not None:
            options.append({"id": cur.id, "label": f"{cur.name} (archived)"})
    return options


def _can_edit_thresholds(user: User) -> bool:
    """MISSION §3: Office cannot change reorder thresholds. Manager + Admin can.

    ``Admin`` always passes ``require_role`` regardless of the allowed list,
    so we have to check role explicitly here rather than relying on the
    dependency to gate a sub-set of fields.
    """
    return user.role in (Role.MANAGER, Role.ADMIN)


def _can_save_item(user: User) -> bool:
    """I1c: Manager + Office + Admin can save edits to items; Workshop cannot.

    Drives the ``can_save`` template flag (form inputs ``disabled`` + submit
    button hidden when False). The POST routes still server-enforce the
    contract via ``require_role(MANAGER, OFFICE)`` — this predicate only
    shapes the form UI for the read-only Workshop view.
    """
    return user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN)


# ---------------------------------------------------------------------------
# Custom field helpers (I2)
# ---------------------------------------------------------------------------
#
# Items inherit the field schema of their leaf node (MISSION §3). Field defs
# live in ``taxonomy_field_defs`` (S5); per-item values live in
# ``item_field_values`` (one row per (item, field def) with a non-null value).
# The form renders one input per *active* field def for the chosen leaf;
# archived defs are not rendered, but their existing values are preserved on
# the item — "Deleting a field hides it from new entry but preserves the
# value in audit history."

# Map a field def's type → the column on ``ItemFieldValue`` that stores the
# value. Select stores the chosen option as a plain string in ``value_text``;
# multiselect stores a list of strings in ``value_json``.
_VALUE_COLUMN: dict[FieldType, str] = {
    FieldType.TEXT: "value_text",
    FieldType.NUMBER: "value_number",
    FieldType.DECIMAL: "value_decimal",
    FieldType.DATE: "value_date",
    FieldType.BOOLEAN: "value_bool",
    FieldType.SELECT: "value_text",
    FieldType.MULTISELECT: "value_json",
}


def _get_active_field_defs(
    db: Session, node_id: int
) -> list[TaxonomyFieldDef]:
    """Active (non-archived) field defs for ``node_id``, ordered by sort_order then name."""
    stmt = (
        select(TaxonomyFieldDef)
        .where(TaxonomyFieldDef.node_id == node_id)
        .where(TaxonomyFieldDef.archived_at.is_(None))
        .order_by(TaxonomyFieldDef.sort_order, TaxonomyFieldDef.name)
    )
    return list(db.execute(stmt).scalars().all())


def _parse_custom_field(
    field_def: TaxonomyFieldDef, raw: str | list[str] | None
) -> Any:
    """Coerce a raw form value (or list, for multiselect) into the right Python type.

    Returns ``None`` for blank / empty / unset (for booleans, returns ``False``
    when the checkbox is absent — booleans always have a definite value). The
    required-flag check is the caller's job (``_collect_custom_fields``).

    Raises ``HTTPException(400)`` on a type mismatch or an out-of-options
    select / multiselect.
    """
    field_label = field_def.name
    if field_def.type == FieldType.MULTISELECT:
        # ``raw`` may be a list (the form returned multiple values) or None
        # (no entries submitted). Empty list → None.
        values = [
            v.strip()
            for v in (raw if isinstance(raw, list) else [])
            if v and v.strip()
        ]
        if not values:
            return None
        options = field_def.options_json or []
        bad = [v for v in values if v not in options]
        if bad:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label}: {bad[0]!r} is not a valid option",
            )
        # De-dup while preserving submission order; HTML <select multiple>
        # can't naturally repeat but a tampered request could.
        seen: set[str] = set()
        out: list[str] = []
        for v in values:
            if v not in seen:
                out.append(v)
                seen.add(v)
        return out

    # Scalar types — collapse list (defensive) to first entry.
    text = raw if isinstance(raw, str) else (raw[0] if isinstance(raw, list) and raw else "")
    text = (text or "").strip()

    if field_def.type == FieldType.BOOLEAN:
        # HTML checkbox: present (any non-empty value) → True; absent → False.
        return text != ""

    if text == "":
        return None

    if field_def.type == FieldType.TEXT:
        return text

    if field_def.type == FieldType.NUMBER:
        try:
            return int(text)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be a whole number",
            ) from exc

    if field_def.type == FieldType.DECIMAL:
        try:
            return Decimal(text)
        except InvalidOperation as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be a number",
            ) from exc

    if field_def.type == FieldType.DATE:
        try:
            return date.fromisoformat(text)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label} must be a date (YYYY-MM-DD)",
            ) from exc

    if field_def.type == FieldType.SELECT:
        options = field_def.options_json or []
        if text not in options:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{field_label}: {text!r} is not a valid option",
            )
        return text

    # Defensive — should be unreachable given FieldType is closed.
    raise HTTPException(  # pragma: no cover
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"unknown field type {field_def.type!r}",
    )


def _is_set(field_def: TaxonomyFieldDef, value: Any) -> bool:
    """Whether a parsed value should be stored / treated as filled.

    For booleans, both True and False count as "set" — a boolean checkbox
    always has a definite value. For everything else, ``None`` (blank input)
    counts as not set; required-flag enforcement and persistence both gate on
    this.
    """
    if field_def.type == FieldType.BOOLEAN:
        return True
    if value is None:
        return False
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, str):
        return value != ""
    return True


def _collect_custom_fields(
    form: FormData, field_defs: list[TaxonomyFieldDef]
) -> dict[int, Any]:
    """Parse every active field def's submission and enforce ``required``.

    Returns a dict keyed by ``field_def.id`` of parsed values (None for
    blank-and-not-required, False for unchecked boolean). Raises 400 on the
    first violation: bad type, out-of-options pick, or a required field that
    wasn't filled. For boolean, "required" means the checkbox must be checked
    (must be True) — see the route docstring for the rationale.
    """
    out: dict[int, Any] = {}
    for fd in field_defs:
        key = f"cf_{fd.key}"
        raw: str | list[str] | None
        if fd.type == FieldType.MULTISELECT:
            raw = list(form.getlist(key))  # type: ignore[arg-type]
        else:
            raw_val = form.get(key)
            raw = raw_val if isinstance(raw_val, str) else None
        value = _parse_custom_field(fd, raw)
        if fd.required:
            if fd.type == FieldType.BOOLEAN:
                if value is not True:
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"{fd.name} must be checked",
                    )
            else:
                if not _is_set(fd, value):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"{fd.name} is required",
                    )
        out[fd.id] = value
    return out


def _audit_friendly(field_def: TaxonomyFieldDef, value: Any) -> Any:
    """Convert a parsed value into a JSON-serialisable shape for the audit log.

    Decimals → str (preserves precision); dates → ISO; everything else passes
    through (lists and primitives are already JSON-friendly).
    """
    if value is None:
        return None
    if field_def.type == FieldType.DECIMAL and isinstance(value, Decimal):
        return str(value)
    if field_def.type == FieldType.DATE and isinstance(value, date):
        return value.isoformat()
    return value


def _persist_custom_field_values(
    db: Session,
    item_id: int,
    field_defs: list[TaxonomyFieldDef],
    values: dict[int, Any],
) -> dict[str, Any]:
    """Insert sparse ``ItemFieldValue`` rows; return audit dict (key → friendly value).

    Skips rows for fields whose value is "not set" (blank text, None number,
    empty multiselect). Boolean ``False`` IS persisted — it's a meaningful
    answer.
    """
    audit: dict[str, Any] = {}
    for fd in field_defs:
        v = values.get(fd.id)
        if not _is_set(fd, v):
            continue
        ifv = ItemFieldValue(item_id=item_id, field_def_id=fd.id)
        col = _VALUE_COLUMN[fd.type]
        setattr(ifv, col, v)
        db.add(ifv)
        audit[fd.key] = _audit_friendly(fd, v)
    return audit


def _load_custom_field_value_rows(
    db: Session, item_id: int
) -> dict[int, ItemFieldValue]:
    """Existing ``ItemFieldValue`` rows for an item, keyed by ``field_def_id``."""
    stmt = select(ItemFieldValue).where(ItemFieldValue.item_id == item_id)
    return {row.field_def_id: row for row in db.execute(stmt).scalars().all()}


def _stored_value(field_def: TaxonomyFieldDef, row: ItemFieldValue) -> Any:
    """Extract the populated value-column off a stored row."""
    return getattr(row, _VALUE_COLUMN[field_def.type])


def _diff_and_apply_custom_fields(
    db: Session,
    item_id: int,
    field_defs: list[TaxonomyFieldDef],
    parsed: dict[int, Any],
    existing: dict[int, ItemFieldValue],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply parsed custom-field updates and return sparse before/after dicts.

    For each active field def: insert / update / delete the row to match the
    parsed value. ``before`` and ``after`` are keyed by the field def's
    ``key`` and contain *only* fields whose value changed (``before`` may be
    ``None`` for newly-set fields; ``after`` may be ``None`` for cleared
    ones). Caller folds these into the wider audit diff.
    """
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for fd in field_defs:
        new_val = parsed.get(fd.id)
        new_set = _is_set(fd, new_val)
        old_row = existing.get(fd.id)
        old_val = _stored_value(fd, old_row) if old_row is not None else None
        old_set = old_row is not None
        if new_set and not old_set:
            # Insert a new row.
            ifv = ItemFieldValue(item_id=item_id, field_def_id=fd.id)
            setattr(ifv, _VALUE_COLUMN[fd.type], new_val)
            db.add(ifv)
            before[fd.key] = None
            after[fd.key] = _audit_friendly(fd, new_val)
        elif not new_set and old_set:
            # Clear: delete the row.
            assert old_row is not None
            db.delete(old_row)
            before[fd.key] = _audit_friendly(fd, old_val)
            after[fd.key] = None
        elif new_set and old_set:
            # Both set — compare. Decimals compare correctly across str/Decimal
            # representations so we don't need to normalise here.
            if old_val != new_val:
                assert old_row is not None
                setattr(old_row, _VALUE_COLUMN[fd.type], new_val)
                before[fd.key] = _audit_friendly(fd, old_val)
                after[fd.key] = _audit_friendly(fd, new_val)
        # else: both unset — nothing to do.
    return before, after


def _form_for_custom_fields(
    field_defs: list[TaxonomyFieldDef],
    rows: dict[int, ItemFieldValue],
) -> dict[str, Any]:
    """Render-shape the existing values for the form template.

    Returns a dict keyed by field key. Values are stringified per type so the
    template can drop them straight into ``value=`` attributes; multiselect
    values stay as lists for the ``selected`` check.
    """
    out: dict[str, Any] = {}
    for fd in field_defs:
        row = rows.get(fd.id)
        if row is None:
            if fd.type == FieldType.MULTISELECT:
                out[fd.key] = []
            elif fd.type == FieldType.BOOLEAN:
                out[fd.key] = False
            else:
                out[fd.key] = ""
            continue
        v = _stored_value(fd, row)
        if fd.type == FieldType.MULTISELECT:
            out[fd.key] = list(v) if v is not None else []
        elif fd.type == FieldType.BOOLEAN:
            out[fd.key] = bool(v)
        elif fd.type == FieldType.DECIMAL and v is not None:
            out[fd.key] = str(v)
        elif fd.type == FieldType.DATE and v is not None:
            out[fd.key] = v.isoformat()
        else:
            out[fd.key] = v if v is not None else ""
    return out


def _category_label(item: Item, db: Session) -> str:
    """Display label for an item's category: ``Parent / Leaf`` or ``Top``."""
    node = db.get(TaxonomyNode, item.taxonomy_node_id)
    if node is None:  # FK guarantees this; defensive for the type checker.
        return ""
    if node.parent_id is None:
        return node.name
    parent = db.get(TaxonomyNode, node.parent_id)
    if parent is None:  # pragma: no cover — same FK guarantee
        return node.name
    return f"{parent.name} / {node.name}"


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------
#
# Active first, archived after when the filter requests it. Within each bucket,
# alphabetical by SKU so the page is stable across loads.

_LIST_ORDER = case((Item.archived_at.is_(None), 0), else_=1)


_ITEMS_CSV_HEADERS: list[str] = [
    "id",
    "sku",
    "name",
    "category",
    "unit",
    "tracking_mode",
    "current_qty",
    "reorder_threshold",
    "reorder_qty",
    "requires_checkout",
]


def _csv_rows_for_items(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Map view-shaped item rows to CSV cell values.

    The ``requires_checkout`` cell renders as the literal string ``"yes"`` /
    ``"no"`` rather than ``"True"`` / ``"False"`` — same posture as the PO
    list's ``supplier_archived`` cell. Spreadsheet receivers find yes/no
    easier to filter on.
    """
    return [
        [
            r["item"].id,
            r["item"].sku,
            r["item"].name,
            r["category_label"],
            r["item"].unit,
            r["item"].tracking_mode.value,
            r["item"].current_qty,
            r["item"].reorder_threshold,
            r["item"].reorder_qty,
            "yes" if r["item"].requires_checkout else "no",
        ]
        for r in rows
    ]


@router.get("")
def list_items(
    request: Request,
    show: str = "active",
    node_id: int | None = None,
    requires_checkout: str = "",
    format: str = "",
    _user: User = Depends(
        require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)
    ),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"
    # Filter is on/off only — "yes" turns it on; anything else (including
    # blank, "no", "all", a tampered value) is treated as no filter. Same
    # silent-coerce posture as ``show`` above.
    requires_checkout_filter = requires_checkout == "yes"

    # CSV export is gated tighter than the HTML branch: Workshop reads the
    # HTML list (per I1c) but cannot pull a snapshot artefact. MISSION §3:
    # Workshop "cannot see aggregated cost data or reports". Same posture as
    # the variance-trend + PO list CSV surfaces (Manager + Office only).
    is_csv = format == "csv"
    if is_csv and _user.role not in (Role.MANAGER, Role.OFFICE, Role.ADMIN):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="forbidden",
        )

    stmt = select(Item)
    if show == "active":
        stmt = stmt.where(Item.archived_at.is_(None))
    else:
        stmt = stmt.where(Item.archived_at.is_not(None))
    if node_id is not None:
        stmt = stmt.where(Item.taxonomy_node_id == node_id)
    if requires_checkout_filter:
        stmt = stmt.where(Item.requires_checkout.is_(True))
    stmt = stmt.order_by(_LIST_ORDER, Item.sku)

    items = list(db.execute(stmt).scalars().all())
    rows = [
        {
            "item": item,
            "category_label": _category_label(item, db),
        }
        for item in items
    ]

    if (
        resp := csv_branch(
            format,
            filename=f"items_{show}.csv",
            headers=_ITEMS_CSV_HEADERS,
            rows=_csv_rows_for_items(rows),
        )
    ) is not None:
        return resp

    return templates.TemplateResponse(
        request,
        "items_list.html",
        {
            "current_user": _user,
            "rows": rows,
            "show": show,
            "node_id": node_id,
            "requires_checkout_filter": requires_checkout_filter,
            "can_create": _user.role in (Role.MANAGER, Role.ADMIN),
            "can_archive": _user.role in (Role.MANAGER, Role.ADMIN),
            "can_edit_item": _can_save_item(_user),
            "can_csv": _user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN),
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_item_form(
    request: Request,
    node_id: int | None = None,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    form = _form_for_item(None)
    field_defs: list[TaxonomyFieldDef] = []
    leaf: TaxonomyNode | None = None
    if node_id is not None:
        # Pre-fill the category if the URL specified one; the form still
        # re-validates on POST so an archived/non-leaf id here just means the
        # user sees their pick rejected. Also fetch the leaf's active field
        # defs so the form renders custom inputs alongside the core fields,
        # and apply any per-leaf defaults the manager configured.
        form["taxonomy_node_id"] = str(node_id)
        field_defs = _get_active_field_defs(db, node_id)
        leaf = db.get(TaxonomyNode, node_id)
        _apply_leaf_defaults(form, leaf)
    form["custom"] = _form_for_custom_fields(field_defs, {})
    return templates.TemplateResponse(
        request,
        "items_form.html",
        {
            "current_user": _user,
            "item": None,
            "form": form,
            "title": "New item",
            "action": "/admin/items",
            "leaf_options": _leaf_options(db),
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
            "can_edit_thresholds": True,
            "can_save": True,
            "field_defs": field_defs,
        },
    )


@router.get("/_custom-fields", response_class=HTMLResponse)
def custom_fields_fragment(
    request: Request,
    taxonomy_node_id: str = "",
    include_defaults: str = "",
    _user: User = Depends(
        require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX fragment: custom-field inputs for the leaf identified by the picked
    category, plus optional out-of-band swap of per-leaf core defaults.

    Wired to the items form's ``<select name="taxonomy_node_id">`` via
    ``hx-get`` / ``hx-trigger="change"``. HTMX automatically includes the
    triggering element's ``name=value`` in the request query string, so this
    route accepts ``taxonomy_node_id`` (matching the form field).

    ``include_defaults=1`` (set via ``hx-vals`` on the create form, omitted
    on edit) tells the route to also emit ``hx-swap-oob`` elements for the
    7 core item fields (unit / tracking_mode / requires_checkout /
    reorder_threshold / reorder_qty / supplier_id / location_id) populated
    from the leaf's ``defaults_json``. The edit form omits the flag so a
    Manager re-classifying an item doesn't silently lose the existing
    item's typed values.

    Empty / unparseable / archived / non-leaf ids render the cf-container
    empty and emit no defaults — the user sees no inputs (nothing to fill
    in for an unselected category). The POST handler re-validates on
    submit, so a hostile id here can't sneak past.

    Same role gating as the edit form (Manager + Office + Workshop). Office
    and Workshop see the form read-only, but ``hx-trigger="change"`` won't
    fire on a disabled select anyway; the permissive gate is just so a
    future widening of the edit form's writable surface doesn't silently
    403 on the fragment.
    """
    field_defs: list[TaxonomyFieldDef] = []
    leaf: TaxonomyNode | None = None
    try:
        parsed_id = int(taxonomy_node_id)
    except (TypeError, ValueError):
        parsed_id = 0
    if parsed_id > 0:
        field_defs = _get_active_field_defs(db, parsed_id)
        leaf = db.get(TaxonomyNode, parsed_id)
    form = _form_for_item(None)
    # Compute the set of keys to OOB-swap. Only emit a swap for keys the
    # leaf actually sets a default for — otherwise an empty default would
    # wipe out a value the user already typed (HTMX swap fires async, after
    # the form is partially filled, so non-defaults must be left alone).
    oob_keys: set[str] = set()
    if include_defaults == "1":
        _apply_leaf_defaults(form, leaf)
        if leaf is not None and leaf.defaults_json:
            oob_keys = {
                k for k in _DEFAULT_KEYS_TO_FORM if k in leaf.defaults_json
            }
            if leaf.defaults_json.get("requires_checkout") is True:
                oob_keys.add("requires_checkout")
    form["custom"] = _form_for_custom_fields(field_defs, {})
    return templates.TemplateResponse(
        request,
        "items_form_custom_fields.html",
        {
            "field_defs": field_defs,
            "form": form,
            "ro": False,
            "oob_keys": oob_keys,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
        },
    )


@router.post("")
async def create_item(
    request: Request,
    sku: str = Form(""),
    name: str = Form(""),
    taxonomy_node_id: str = Form(""),
    unit: str = Form(""),
    tracking_mode: str = Form(TrackingMode.QTY.value),
    requires_checkout: bool = Form(False),
    reorder_threshold: str = Form(""),
    reorder_qty: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    qr_code: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    # Auto-generate SKU when the form omits it. The form input was removed
    # in the items-form-simplification slice; explicit POSTs (tests, the
    # public API surface) can still pass their own SKU and override.
    if not (sku or "").strip():
        try:
            leaf_int = int((taxonomy_node_id or "").strip())
        except ValueError:
            leaf_int = 0
        leaf = db.get(TaxonomyNode, leaf_int) if leaf_int > 0 else None
        sku = _generate_sku(db, leaf)
    fields = _normalise(
        db,
        sku=sku,
        name=name,
        taxonomy_node_id=taxonomy_node_id,
        unit=unit,
        tracking_mode=tracking_mode,
        requires_checkout=requires_checkout,
        reorder_threshold=reorder_threshold,
        reorder_qty=reorder_qty,
        supplier_id=supplier_id,
        location_id=location_id,
        qr_code=qr_code,
        notes=notes,
    )
    _check_sku_unique(db, fields["sku"])
    _check_qr_unique(db, fields["qr_code"])

    # Custom fields: read the full form (FastAPI caches it after the typed
    # ``Form()`` parameters above, so this is free) and parse against the
    # leaf's active schema. Required fields raise 400 here, preventing the
    # item row from being inserted at all.
    field_defs = _get_active_field_defs(db, fields["taxonomy_node_id"])
    form = await request.form()
    parsed_custom = _collect_custom_fields(form, field_defs)

    item = Item(
        sku=fields["sku"],
        name=fields["name"],
        taxonomy_node_id=fields["taxonomy_node_id"],
        unit=fields["unit"],
        tracking_mode=fields["tracking_mode"],
        requires_checkout=fields["requires_checkout"],
        reorder_threshold=fields["reorder_threshold"],
        reorder_qty=fields["reorder_qty"],
        supplier_id=fields["supplier_id"],
        location_id=fields["location_id"],
        qr_code=fields["qr_code"],
        notes=fields["notes"],
        current_qty=Decimal("0"),
    )
    db.add(item)
    db.flush()

    custom_audit = _persist_custom_field_values(
        db, item.id, field_defs, parsed_custom
    )

    audit_after: dict[str, Any] = {
        f: fields[f] for f in _FIELDS
    } | {"current_qty": item.current_qty}
    if custom_audit:
        audit_after["custom_fields"] = custom_audit

    record_audit(
        db,
        actor=user,
        action="item.created",
        entity_type="item",
        entity_id=item.id,
        before=None,
        after=audit_after,
    )
    db.commit()
    _flash(request, f"Item “{item.name}” created.")
    return RedirectResponse(
        url="/admin/items", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{item_id}/edit", response_class=HTMLResponse)
def edit_item_form(
    request: Request,
    item_id: int,
    _user: User = Depends(
        require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )
    field_defs = _get_active_field_defs(db, item.taxonomy_node_id)
    existing_rows = _load_custom_field_value_rows(db, item.id)
    form = _form_for_item(item)
    form["custom"] = _form_for_custom_fields(field_defs, existing_rows)
    can_save = _can_save_item(_user)
    return templates.TemplateResponse(
        request,
        "items_form.html",
        {
            "current_user": _user,
            "item": item,
            "form": form,
            "title": (
                f"Edit {item.name}" if can_save else f"View {item.name}"
            ),
            "action": f"/admin/items/{item.id}",
            "leaf_options": _leaf_options(db, current_id=item.taxonomy_node_id),
            "supplier_options": _supplier_options(
                db, current_id=item.supplier_id
            ),
            "location_options": _location_options(
                db, current_id=item.location_id
            ),
            "tracking_modes": [m.value for m in TrackingMode],
            "can_edit_thresholds": _can_edit_thresholds(_user),
            "can_save": can_save,
            "field_defs": field_defs,
        },
    )


@router.post("/{item_id}")
async def update_item(
    request: Request,
    item_id: int,
    sku: str = Form(""),
    name: str = Form(""),
    taxonomy_node_id: str = Form(""),
    unit: str = Form(""),
    tracking_mode: str = Form(TrackingMode.QTY.value),
    requires_checkout: bool = Form(False),
    reorder_threshold: str = Form(""),
    reorder_qty: str = Form(""),
    supplier_id: str = Form(""),
    location_id: str = Form(""),
    qr_code: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )

    # MISSION §3: Office cannot change reorder thresholds. Silently override
    # any inbound values with the existing row's values *before* validation,
    # so an Office user (or a tampered form) can't even fail on those fields.
    if not _can_edit_thresholds(user):
        reorder_threshold = str(item.reorder_threshold)
        reorder_qty = str(item.reorder_qty)

    fields = _normalise(
        db,
        sku=sku,
        name=name,
        taxonomy_node_id=taxonomy_node_id,
        unit=unit,
        tracking_mode=tracking_mode,
        requires_checkout=requires_checkout,
        reorder_threshold=reorder_threshold,
        reorder_qty=reorder_qty,
        supplier_id=supplier_id,
        location_id=location_id,
        qr_code=qr_code,
        notes=notes,
        current_node_id=item.taxonomy_node_id,
        current_supplier_id=item.supplier_id,
        current_location_id=item.location_id,
    )
    _check_sku_unique(db, fields["sku"], exclude_id=item.id)
    _check_qr_unique(db, fields["qr_code"], exclude_id=item.id)

    # Custom fields: parse against the *current* leaf's active schema.
    # Existing rows for archived defs (or for defs that no longer belong to
    # this leaf if the category just changed) are intentionally left alone —
    # MISSION §3 "existing items keep their stored values". Required-flag
    # validation runs here, so a 400 short-circuits before any item update
    # writes.
    field_defs = _get_active_field_defs(db, fields["taxonomy_node_id"])
    form = await request.form()
    parsed_custom = _collect_custom_fields(form, field_defs)
    existing_rows = _load_custom_field_value_rows(db, item.id)

    diff = _diff(item, fields)
    custom_before, custom_after = _diff_and_apply_custom_fields(
        db, item.id, field_defs, parsed_custom, existing_rows
    )
    if diff is not None or custom_before:
        if diff is not None:
            before, after = diff
            for f in _FIELDS:
                setattr(item, f, fields[f])
        else:
            before = {}
            after = {}
        if custom_before:
            before["custom_fields"] = custom_before
            after["custom_fields"] = custom_after
        record_audit(
            db,
            actor=user,
            action="item.updated",
            entity_type="item",
            entity_id=item.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Item “{item.name}” updated.")
    else:
        # No-op: don't write an audit row, but still 303 so POST-redirect-GET
        # completes cleanly. Matches the suppliers/locations/taxonomy pattern.
        db.rollback()

    return RedirectResponse(
        url="/admin/items", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (soft delete)
# ---------------------------------------------------------------------------


@router.post("/{item_id}/archive")
def archive_item(
    request: Request,
    item_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )

    if item.archived_at is None:
        item.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="item.archived",
            entity_type="item",
            entity_id=item.id,
            before={"archived_at": None},
            after={"archived_at": item.archived_at},
        )
        db.commit()
        _flash(request, f"Item “{item.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/items", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{item_id}/unarchive")
def unarchive_item(
    request: Request,
    item_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )

    if item.archived_at is not None:
        previous = item.archived_at
        item.archived_at = None
        record_audit(
            db,
            actor=user,
            action="item.unarchived",
            entity_type="item",
            entity_id=item.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Item “{item.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/items", status_code=status.HTTP_303_SEE_OTHER
    )
