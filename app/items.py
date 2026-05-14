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

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app import field_storage
from app.audit import record_audit
from app.auth import require_role
from app.cost_engine import record_receipt
from app.csv_export import csv_branch
from app.csv_import import (
    CsvUploadError,
    RowResult,
    check_required_and_known_headers,
    read_upload,
    row_to_dict,
)
from app.db import get_session
from app.field_catalog import CATALOG_BY_KEY, CatalogEntry
from app.models import (
    Archetype,
    CostLayer,
    CostLayerSource,
    Item,
    Location,
    MovementType,
    Role,
    StockMovement,
    Supplier,
    TaxonomyFieldDef,
    TaxonomyNode,
    TaxonomyStage,
    TrackingMode,
    User,
)
from app.sku import (
    allocate_sequence,
    ancestor_chain,
    compose_sku,
    create_unique_variant_leaf,
    effective_archetype,
    node_depth,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/items", tags=["items"])
# See ``app/suppliers.py`` for the rationale: the literal ``/upload`` route
# must be matched ahead of the dynamic ``/{item_id}`` routes.
upload_router = APIRouter(prefix="/admin/items", tags=["items"])


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
    "ring_size",
    "weight_grams",
    "stone_shape",
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


def _collect_descendant_node_ids(db: Session, node_id: int) -> list[int]:
    """Return ``[node_id]`` plus the ids of every descendant within two levels.

    Used by the items list filter so a query against a depth-1 sub-cat in a
    ``unique_variant`` tree matches every item living on its depth-2
    auto-leaves. Includes both active and archived descendants — archived
    rows still legally host items that show up in the archived items view.
    """
    ids: list[int] = [node_id]
    children = list(
        db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id == node_id)).scalars().all()
    )
    ids.extend(children)
    if children:
        grandchildren = list(
            db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id.in_(children)))
            .scalars()
            .all()
        )
        ids.extend(grandchildren)
    return ids


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


def _is_pickable(db: Session, node: TaxonomyNode) -> bool:
    """Is this node a valid item destination per the taxonomy refinement?

    A node is pickable if any of:
    - It is a leaf (no active children) AND its effective archetype is
      ``bulk`` / ``unique``. The item lands directly on this leaf.
    - It is a depth-1 sub-category AND its effective archetype is
      ``unique_variant``. The item lands on a freshly-allocated auto-leaf
      below; the sub-cat itself looks like a container but acts as a leaf
      for item-create purposes. See ``docs/taxonomy-refinement-plan.md``
      §0 for the rationale.

    Depth-0 nodes under a ``unique_variant`` archetype are NOT pickable
    (the spec requires a 3-level path: root → sub-cat → auto-leaf). Depth-2
    auto-leaves are also not pickable directly — each one holds exactly one
    item and is full.
    """

    archetype = effective_archetype(db, node)
    if archetype == Archetype.UNIQUE_VARIANT:
        # Unique-variant items land on auto-leaves under a depth-1 sub-cat.
        # The sub-cat is the picker target.
        return node_depth(db, node) == 1
    # bulk + unique: any leaf node is pickable.
    return _is_leaf(db, node)


def _resolve_leaf_node(
    db: Session, raw_node_id: str, *, current_id: int | None = None
) -> TaxonomyNode:
    """Load a taxonomy node by id and verify it's a non-archived pickable destination.

    Post-refinement "pickable" generalises the previous "leaf" rule (see
    ``_is_pickable``): for ``unique_variant`` trees a depth-1 sub-cat is
    pickable even though it has children.

    ``current_id`` is the item's existing ``taxonomy_node_id`` on edit (None
    on create). If the user submits the same id and that node is archived,
    accept the unchanged assignment so editing other fields doesn't force a
    category change. Switching to any *different* archived id still 400s.

    The pickable-rule check applies regardless: a top-level node with
    active children is rejected even when it's the current value, but that
    state is unreachable under the route's other guards.
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
    if not _is_pickable(db, node):
        archetype = effective_archetype(db, node)
        if archetype == Archetype.UNIQUE_VARIANT and node_depth(db, node) == 0:
            detail = (
                "unique-variant trees require a sub-category — pick one of "
                "this category's sub-categories instead"
            )
        elif archetype == Archetype.UNIQUE_VARIANT and node_depth(db, node) == 2:
            detail = (
                "this depth-2 auto-leaf is system-managed and already holds "
                "its item — pick a sub-category to create another"
            )
        else:
            detail = "category has sub-categories — pick one of its sub-categories instead"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail,
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
    ring_size: str = "",
    weight_grams: str = "",
    stone_shape: str = "",
    current_node_id: int | None = None,
    current_supplier_id: int | None = None,
    current_location_id: int | None = None,
    visibility: dict[str, str] | None = None,
    leaf_defaults: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Strip / parse / validate every form field. Returns the value-shape stored on the row.

    Raises ``HTTPException(400)`` on any validation error. Uniqueness checks
    against the DB (sku / qr_code) live in their own callers because they need
    the per-route ``exclude_id``.

    The ``current_*`` ids are the existing item's FK values on edit (all
    ``None`` on create). Used by the FK resolvers to keep an unchanged
    archived FK assignment without 400ing.

    ``visibility`` is the per-leaf built-in field visibility map (see
    ``app.field_visibility``). Hidden fields ignore the submitted value
    (treated as empty). Required-state ``name`` / ``unit`` still 400 on
    blanks. Non-required ``name`` left blank returns ``""`` here — callers
    fill it post-SKU-allocation (create) or from the existing row (update).
    Non-required ``unit`` left blank falls back to ``leaf_defaults["unit"]``
    or ``"ea"``.
    """
    vis = visibility or {}

    def _is(field: str, state: str) -> bool:
        return vis.get(field) == state

    def _input(field: str, raw: str) -> str:
        return "" if _is(field, "hidden") else raw

    clean_sku = (sku or "").strip()
    if not clean_sku:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SKU is required")

    clean_name = _input("name", name).strip()
    if not clean_name and _is("name", "required"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")

    clean_unit = _input("unit", unit).strip()
    if not clean_unit:
        if _is("unit", "required"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unit is required",
            )
        fallback_unit = (leaf_defaults or {}).get("unit") or ""
        clean_unit = str(fallback_unit).strip() or "ea"

    node = _resolve_leaf_node(db, taxonomy_node_id, current_id=current_node_id)
    mode_raw = _input("tracking_mode", tracking_mode)
    mode = _coerce_tracking_mode(mode_raw) if mode_raw else _coerce_tracking_mode(tracking_mode)

    threshold = _parse_decimal(
        _input("reorder_threshold", reorder_threshold), field_name="reorder threshold"
    )
    qty = _parse_decimal(_input("reorder_qty", reorder_qty), field_name="reorder quantity")

    sup_id = _resolve_optional_supplier(
        db, _input("supplier_id", supplier_id), current_id=current_supplier_id
    )
    loc_id = _resolve_optional_location(
        db, _input("location_id", location_id), current_id=current_location_id
    )

    clean_qr = (_input("qr_code", qr_code) or "").strip() or None
    clean_notes = (notes or "").strip() or None

    clean_ring_size = (_input("ring_size", ring_size) or "").strip() or None
    weight_grams_raw = (_input("weight_grams", weight_grams) or "").strip()
    clean_weight_grams: Decimal | None
    if weight_grams_raw:
        try:
            parsed_weight = Decimal(weight_grams_raw)
        except InvalidOperation as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="weight (g) must be a number",
            ) from exc
        if parsed_weight < 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="weight (g) cannot be negative",
            )
        clean_weight_grams = parsed_weight
    else:
        clean_weight_grams = None
    clean_stone_shape = (_input("stone_shape", stone_shape) or "").strip() or None

    return {
        "sku": clean_sku,
        "name": clean_name,
        "taxonomy_node_id": node.id,
        "unit": clean_unit,
        "tracking_mode": mode,
        "requires_checkout": False
        if _is("requires_checkout", "hidden")
        else bool(requires_checkout),
        "reorder_threshold": threshold,
        "reorder_qty": qty,
        "supplier_id": sup_id,
        "location_id": loc_id,
        "qr_code": clean_qr,
        "notes": clean_notes,
        "ring_size": clean_ring_size,
        "weight_grams": clean_weight_grams,
        "stone_shape": clean_stone_shape,
    }


def _check_sku_unique(db: Session, sku: str, *, exclude_id: int | None = None) -> None:
    stmt = select(Item.id).where(Item.sku == sku)
    if exclude_id is not None:
        stmt = stmt.where(Item.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="an item with that SKU already exists",
        )


def _check_qr_unique(db: Session, qr_code: str | None, *, exclude_id: int | None = None) -> None:
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


def _diff(item: Item, new: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
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
            "ring_size": "",
            "weight_grams": "",
            "stone_shape": "",
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
        "ring_size": item.ring_size or "",
        "weight_grams": str(item.weight_grams) if item.weight_grams is not None else "",
        "stone_shape": item.stone_shape or "",
    }


_DEFAULT_KEYS_TO_FORM: dict[str, str] = {
    "unit": "unit",
    "tracking_mode": "tracking_mode",
    "reorder_threshold": "reorder_threshold",
    "reorder_qty": "reorder_qty",
    "supplier_id": "supplier_id",
    "location_id": "location_id",
}


def _apply_leaf_defaults(form: dict[str, Any], leaf: TaxonomyNode | None) -> None:
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


def _allocate_sku(db: Session, leaf: TaxonomyNode) -> tuple[str, int, TaxonomyNode]:
    """Allocate a server-owned SKU + sequence for a new item on ``leaf``.

    Returns ``(sku, sequence, destination_leaf)``. The destination leaf is
    either the user-picked leaf itself (for ``bulk`` / ``unique`` items) or
    a freshly-created depth-2 auto-leaf (for ``unique_variant`` items). The
    caller attaches the item to the returned ``destination_leaf``.

    Behaviour by effective archetype:

    - ``bulk`` / ``unique`` — atomically increment ``leaf.next_sequence``
      and compose ``<ancestor-prefixes>-<NNNN>``. The item lives on
      ``leaf``.
    - ``unique_variant`` — atomically increment the depth-1 sub-cat's
      ``next_sequence``, mint a depth-2 auto-leaf named ``"{seq:03d}"``,
      and compose ``<root>-<sub>-<NNN>``. The item lives on the new leaf.

    Raises ``HTTPException(400)`` if the picked node is not a valid
    destination for its archetype (e.g. a unique-variant item submitted
    against a depth-0 node). ``_resolve_leaf_node`` should already have
    rejected those, but this re-check is defence-in-depth.
    """

    archetype = effective_archetype(db, leaf)
    if archetype is None:
        # Orphaned tree — defensive. ``effective_archetype`` returns None
        # only when the parent chain is broken; the route layer guarantees
        # we never get here under normal use.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="category is missing an archetype (data integrity)",
        )

    if archetype == Archetype.UNIQUE_VARIANT:
        if node_depth(db, leaf) != 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "unique-variant items require a 2-level path: pick a "
                    "sub-category under a unique-variant top-level"
                ),
            )
        seq = allocate_sequence(db, leaf)
        auto_leaf = create_unique_variant_leaf(db, leaf, seq)
        chain = ancestor_chain(db, auto_leaf)
        prefixes = [n.sku_prefix for n in chain]
        return compose_sku(prefixes, seq, archetype), seq, auto_leaf

    # bulk / unique
    seq = allocate_sequence(db, leaf)
    chain = ancestor_chain(db, leaf)
    prefixes = [n.sku_prefix for n in chain]
    return compose_sku(prefixes, seq, archetype), seq, leaf


def _tracking_mode_for(archetype: Archetype) -> TrackingMode:
    """Map an effective archetype to the operational tracking mode.

    Bulk items are quantity-tracked; unique + unique-variant items are
    per-unit-tracked. The column stays writable on edit (Office may need to
    correct an entry mistake) but the create flow forces this value so a
    bulk item never ships with ``tracking_mode=unique``.
    """
    if archetype == Archetype.BULK:
        return TrackingMode.QTY
    return TrackingMode.UNIQUE


def _node_breadcrumb(db: Session, node: TaxonomyNode) -> str:
    """Compose a top-down breadcrumb label for a node.

    Depth 0: ``"Tools"``. Depth 1: ``"Raw Materials / Silver"``. Depth 2:
    ``"Raw Materials / Silver / 925"``. Defensive against orphaned parent
    chains: a missing ancestor renders as ``"?"`` rather than 500ing.
    """
    chain = ancestor_chain(db, node)
    return " / ".join(n.name for n in chain)


def _pickable_options(db: Session, *, current_id: int | None = None) -> list[dict[str, Any]]:
    """Item-destination options for the create / edit form's category picker.

    Returns a flat list of ``{id, label, breadcrumb, archetype, sku_prefix,
    is_group}`` dicts. The picker UI (Agent 4) will consume the
    ``breadcrumb`` field for display + filtering. ``is_group`` stays in the
    output so legacy templates that iterate the list don't break.

    Pickable means (see ``_is_pickable``):
    - leaf with bulk / unique archetype (depth 0, 1, or 2).
    - depth-1 sub-cat under a unique_variant top-level.

    If ``current_id`` references an archived row or one no longer
    pickable, it is appended with an explanatory suffix so the edit form
    can keep the existing assignment without dropping the option.
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

    options: list[dict[str, Any]] = []
    rendered_ids: set[int] = set()
    for node in rows:
        if not _is_pickable(db, node):
            continue
        archetype = effective_archetype(db, node)
        options.append(
            {
                "id": node.id,
                "label": _node_breadcrumb(db, node),
                "breadcrumb": _node_breadcrumb(db, node),
                "archetype": archetype.value if archetype else None,
                "sku_prefix": node.sku_prefix,
                "is_group": False,
            }
        )
        rendered_ids.add(node.id)

    if current_id is not None and current_id not in rendered_ids:
        cur = db.get(TaxonomyNode, current_id)
        if cur is not None:
            archived = cur.archived_at is not None
            suffix = " (archived)" if archived else " (no longer a pickable destination)"
            archetype = effective_archetype(db, cur)
            options.append(
                {
                    "id": cur.id,
                    "label": _node_breadcrumb(db, cur) + suffix,
                    "breadcrumb": _node_breadcrumb(db, cur) + suffix,
                    "archetype": archetype.value if archetype else None,
                    "sku_prefix": cur.sku_prefix,
                    "is_group": False,
                }
            )
    return options


# Backwards-compatible alias retained for callers that still expect
# ``_leaf_options``. New code should call ``_pickable_options``.
_leaf_options = _pickable_options


def _breadcrumb_for_form(db: Session, taxonomy_node_id: str | int | None) -> str:
    """Resolve a node id to its breadcrumb for the items form picker.

    Used by the form template to pre-populate the visible
    ``#taxonomy_node_search`` input on edit / re-render. Empty / unparseable
    / missing ids return an empty string so the picker starts blank.
    """
    if taxonomy_node_id is None or taxonomy_node_id == "":
        return ""
    try:
        parsed = int(taxonomy_node_id)
    except (TypeError, ValueError):
        return ""
    if parsed <= 0:
        return ""
    node = db.get(TaxonomyNode, parsed)
    if node is None:
        return ""
    return _node_breadcrumb(db, node)


def _compose_sku_preview(db: Session, picked: TaxonomyNode) -> str:
    """Compose a non-allocating SKU preview for the items form.

    Mirrors ``_allocate_sku`` but reads ``next_sequence`` directly without
    incrementing — the preview is a guess shown to the user before submit,
    not an allocation. The actual SKU is allocated atomically inside the
    POST handler's transaction.

    Returns ``""`` if the node is missing an archetype (data integrity) or
    is not a valid destination for its archetype. The caller maps the empty
    string to "no preview" in the template.
    """
    archetype = effective_archetype(db, picked)
    if archetype is None:
        return ""
    if archetype == Archetype.UNIQUE_VARIANT:
        # Allocator lives on the depth-1 sub-cat; depth-2 auto-leaf does not
        # exist yet at preview time. Mirror ``_allocate_sku``'s shape: chain
        # is [root, sub] then synthesise the leaf's ``f"{seq:03d}"`` prefix.
        if node_depth(db, picked) != 1:
            return ""
        seq = picked.next_sequence
        chain = ancestor_chain(db, picked)
        prefixes = [n.sku_prefix for n in chain] + [f"{seq:03d}"]
        return compose_sku(prefixes, seq, archetype)
    # bulk / unique: peek at the leaf's next_sequence and compose.
    seq = picked.next_sequence
    chain = ancestor_chain(db, picked)
    prefixes = [n.sku_prefix for n in chain]
    return compose_sku(prefixes, seq, archetype)


def _sku_preview_for_form(db: Session, taxonomy_node_id: str | int | None) -> str:
    """Render-ready SKU preview string for the items form.

    Returns ``""`` when no node is picked yet (so the ``<output>`` element
    starts empty). Otherwise returns ``"Next SKU: <sku>"`` for the picker's
    selected node.
    """
    if taxonomy_node_id is None or taxonomy_node_id == "":
        return ""
    try:
        parsed = int(taxonomy_node_id)
    except (TypeError, ValueError):
        return ""
    if parsed <= 0:
        return ""
    node = db.get(TaxonomyNode, parsed)
    if node is None:
        return ""
    composed = _compose_sku_preview(db, node)
    if not composed:
        return ""
    return f"Next SKU: {composed}"


def _supplier_options(db: Session, *, current_id: int | None = None) -> list[dict[str, Any]]:
    """Active suppliers + the assigned archived row (with "(archived)" suffix) if any."""
    rows = list(
        db.execute(select(Supplier).where(Supplier.archived_at.is_(None)).order_by(Supplier.name))
        .scalars()
        .all()
    )
    options: list[dict[str, Any]] = [{"id": s.id, "label": s.name} for s in rows]
    if current_id is not None and not any(opt["id"] == current_id for opt in options):
        cur = db.get(Supplier, current_id)
        if cur is not None:
            options.append({"id": cur.id, "label": f"{cur.name} (archived)"})
    return options


def _location_options(db: Session, *, current_id: int | None = None) -> list[dict[str, Any]]:
    """Active locations + the assigned archived row (with "(archived)" suffix) if any."""
    rows = list(
        db.execute(select(Location).where(Location.archived_at.is_(None)).order_by(Location.name))
        .scalars()
        .all()
    )
    options: list[dict[str, Any]] = [{"id": loc.id, "label": loc.name} for loc in rows]
    if current_id is not None and not any(opt["id"] == current_id for opt in options):
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

# Field-value persistence (the ``ItemFieldValue.value_*`` column dispatch)
# lives in ``app.field_storage`` so slice 6's column-backed catalog entries
# can extend the abstraction in one place.


def _ancestor_chain_ids(db: Session, node_id: int) -> list[int]:
    """Return the id chain ``[root, …, node_id]`` by walking ``parent_id`` upward.

    Returns ``[]`` if the node doesn't exist. Top-level nodes return their own
    id only. Cap at the taxonomy's natural depth (3) plus a safety margin to
    defend against a cycle from a corrupt parent_id chain.
    """
    chain: list[int] = []
    cursor: TaxonomyNode | None = db.get(TaxonomyNode, node_id)
    seen: set[int] = set()
    while cursor is not None and cursor.id not in seen:
        chain.append(cursor.id)
        seen.add(cursor.id)
        if cursor.parent_id is None:
            break
        cursor = db.get(TaxonomyNode, cursor.parent_id)
    chain.reverse()
    return chain


def _get_active_field_defs(db: Session, node_id: int) -> list[TaxonomyFieldDef]:
    """Active field defs effective for ``node_id``, including inherited from ancestors.

    Walks up the parent chain and collects field defs from every node in
    the chain. Order: root first (most general), then descendants (most
    specific); within each level, by ``sort_order`` then ``key``. Item
    forms render inherited fields above the leaf's own.
    """
    chain = _ancestor_chain_ids(db, node_id)
    if not chain:
        return []
    stmt = (
        select(TaxonomyFieldDef)
        .where(TaxonomyFieldDef.node_id.in_(chain))
    )
    rows = list(db.execute(stmt).scalars().all())
    chain_position = {nid: idx for idx, nid in enumerate(chain)}
    rows.sort(key=lambda r: (chain_position[r.node_id], r.sort_order, r.key))
    return rows


def _picked_built_in_keys(db: Session, node_id: int | None) -> set[str]:
    """Catalog keys picked on ``node_id`` or any ancestor.

    The items form gates each input on this set — fields the category did
    not pick are hidden and auto-filled server-side (name → SKU, unit →
    "ea", others left null) so unrelated category metadata doesn't clutter
    the Add Item screen.
    """

    if node_id is None:
        return set()
    out: set[str] = set()
    for fd in _get_active_field_defs(db, node_id):
        if fd.key in CATALOG_BY_KEY:
            out.add(fd.key)
    return out


_BUILT_IN_REQUIRED_WHEN_PICKED: frozenset[str] = frozenset({"name", "unit"})


def _built_in_visibility_from_picks(picked: set[str]) -> dict[str, str]:
    """Derive the legacy visibility dict from the catalog picks.

    Keeps the ``"required" | "optional" | "hidden"`` vocabulary the items
    form template + form-validator already speak. Unpicked fields are
    ``"hidden"`` (server auto-fills on save). Picked ``name`` / ``unit``
    are ``"required"`` because the underlying columns are NOT NULL —
    rejecting blanks here yields a clearer 400 than letting the DB
    constraint fire. Every other picked field is ``"optional"``.
    Slice 6 removed the per-leaf override stored in
    ``TaxonomyNode.field_visibility_json``; the dict now derives wholly
    from the catalog picks.
    """

    from app.field_visibility import BUILT_IN_FIELDS

    out: dict[str, str] = {}
    for key in BUILT_IN_FIELDS:
        if key not in picked:
            out[key] = "hidden"
        elif key in _BUILT_IN_REQUIRED_WHEN_PICKED:
            out[key] = "required"
        else:
            out[key] = "optional"
    return out


def _filter_category_options(db: Session) -> list[dict[str, Any]]:
    """Return all non-archived taxonomy nodes as filter dropdown options.

    Each entry is ``{id, label}`` where ``label`` is the breadcrumb path
    (``"Parent / Child / Grandchild"``). Used by the items list filter so the
    user can scope to a top-level branch (any node), not just leaves —
    ``_collect_descendant_node_ids`` resolves a non-leaf pick to the full set
    of underlying leaves at query time.
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
    options = [{"id": n.id, "label": _node_breadcrumb(db, n)} for n in rows]
    # Sort by breadcrumb so ancestors group with their descendants and the
    # dropdown reads top-down (e.g. "Raw Materials" before "Raw Materials /
    # Silver"). Stable Python sort.
    options.sort(key=lambda o: str(o["label"]).casefold())
    return options


def _apply_field_def_filters(
    stmt: Any,
    db: Session,
    field_defs: list[TaxonomyFieldDef],
    raw: dict[str, str],
) -> tuple[Any, dict[str, str]]:
    """Filter ``stmt`` by per-catalog-key search criteria pulled from ``raw``.

    Every catalog entry maps to an ``Item`` column (post-0024), so each
    filter is a WHERE on the column directly. Query-string keys carry the
    bare catalog ``key`` (e.g. ``ring_size=j 1/2``). Type dispatch is
    driven by the catalog entry, not by ``TaxonomyFieldDef`` metadata.

    Invalid / unparseable values are silently ignored so a hand-typed URL
    never 500s. Returns the patched ``stmt`` plus the dict of applied
    ``{<key>: <raw>}`` echoed back to the template for input echoes.
    """
    applied: dict[str, str] = {}
    for fd in field_defs:
        key = fd.key
        value = (raw.get(key) or "").strip()
        if not value:
            continue
        entry = CATALOG_BY_KEY.get(key)
        if entry is None:
            continue
        col = getattr(Item, entry.column, None)
        if col is None:
            continue
        applied[key] = value
        type_value = entry.type.value
        if type_value in ("text", "select"):
            stmt = stmt.where(col.ilike(f"%{value}%"))
        elif type_value == "multiselect":
            stmt = stmt.where(func.cast(col, sa.String).ilike(f"%{value}%"))
        elif type_value == "boolean":
            normalised = value.lower()
            if normalised in ("yes", "true", "1"):
                stmt = stmt.where(col.is_(True))
            elif normalised in ("no", "false", "0"):
                stmt = stmt.where(col.is_(False))
            else:
                applied.pop(key, None)
        elif type_value == "number":
            try:
                parsed_int = int(value)
            except ValueError:
                applied.pop(key, None)
                continue
            stmt = stmt.where(col == parsed_int)
        elif type_value == "decimal":
            try:
                parsed_dec = Decimal(value)
            except InvalidOperation:
                applied.pop(key, None)
                continue
            stmt = stmt.where(col == parsed_dec)
        elif type_value == "date":
            try:
                parsed_date = datetime.fromisoformat(value).date()
            except ValueError:
                applied.pop(key, None)
                continue
            stmt = stmt.where(col == parsed_date)
        else:
            applied.pop(key, None)
    return stmt, applied




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


def _initial_stage_id_for_leaf(db: Session, leaf: TaxonomyNode) -> int | None:
    """Return the id of the ``is_initial`` active stage on the leaf's top-level node.

    Returns ``None`` if the top-level category has no stages, no stage is
    marked initial, or only archived stages match. Items in those cases keep
    ``current_stage_id = NULL`` — the column's legitimate default state for a
    category that doesn't model lifecycle.
    """
    chain = ancestor_chain(db, leaf)
    if not chain:
        return None
    top_level = chain[0]
    row_id = db.execute(
        select(TaxonomyStage.id)
        .where(TaxonomyStage.top_level_node_id == top_level.id)
        .where(TaxonomyStage.is_initial.is_(True))
        .where(TaxonomyStage.archived_at.is_(None))
    ).scalar()
    return int(row_id) if row_id is not None else None


def _stage_label(item: Item, db: Session) -> str:
    """Display label for an item's current lifecycle stage; ``""`` if unset."""
    if item.current_stage_id is None:
        return ""
    stage = db.get(TaxonomyStage, item.current_stage_id)
    return stage.name if stage is not None else ""


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
    "stage",
    "unit",
    "tracking_mode",
    "current_qty",
    "reorder_threshold",
    "reorder_qty",
    "requires_checkout",
    "unit_cost",
]


def _csv_rows_for_items(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Map view-shaped item rows to CSV cell values.

    The ``requires_checkout`` cell renders as the literal string ``"yes"`` /
    ``"no"`` rather than ``"True"`` / ``"False"`` — same posture as the PO
    list's ``supplier_archived`` cell. Spreadsheet receivers find yes/no
    easier to filter on.

    The ``category`` cell uses the full top-down breadcrumb (e.g.
    ``"Raw Materials / Silver / 925"``) — *not* the HTML's two-segment
    ``_category_label`` — so the CSV is round-trippable through
    ``upload_items`` (which resolves the slash-path from depth-0 down).

    The ``unit_cost`` cell is populated for unique / unique-variant items
    only and carries the open FIFO layer's unit cost (the per-row dict's
    ``unit_cost`` key set by ``list_items``). BULK rows render blank — the
    cell is meaningless across multiple FIFO layers and would mislead.
    """
    return [
        [
            r["item"].id,
            r["item"].sku,
            r["item"].name,
            r["category_path"],
            r.get("stage_label", ""),
            r["item"].unit,
            r["item"].tracking_mode.value,
            r["item"].current_qty,
            r["item"].reorder_threshold,
            r["item"].reorder_qty,
            "yes" if r["item"].requires_checkout else "no",
            r.get("unit_cost", ""),
        ]
        for r in rows
    ]


def _catalog_columns_for(
    db: Session, node_id: int
) -> list[tuple[TaxonomyFieldDef, CatalogEntry]]:
    """Effective catalog-backed columns for the items list, in display order.

    Walks ``_get_active_field_defs`` (inheritance-aware) and pairs each def
    with its catalog entry. Defs whose ``key`` doesn't resolve to a live
    catalog entry are skipped — they have no column header or cell
    formatter without a catalog entry.
    """

    columns: list[tuple[TaxonomyFieldDef, CatalogEntry]] = []
    for fd in _get_active_field_defs(db, node_id):
        entry = CATALOG_BY_KEY.get(fd.key)
        if entry is None:
            continue
        columns.append((fd, entry))
    return columns




@router.get("")
def list_items(
    request: Request,
    show: str = "active",
    node_id: int | None = None,
    requires_checkout: str = "",
    format: str = "",
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)),
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
        # Match items whose ``taxonomy_node_id`` is the given node OR any
        # descendant of it (inclusive). The taxonomy is at most 3 levels,
        # so a single down-walk through children + grandchildren is enough
        # — no recursive CTE needed. This makes the unique-variant case
        # work: items live on depth-2 auto-leaves, but a filter on the
        # depth-1 sub-cat must surface them.
        descendant_ids = _collect_descendant_node_ids(db, node_id)
        stmt = stmt.where(Item.taxonomy_node_id.in_(descendant_ids))
    if requires_checkout_filter:
        stmt = stmt.where(Item.requires_checkout.is_(True))

    # Per-leaf custom-field filters. Only applied when ``node_id`` points at
    # an active leaf with field defs — the filter inputs that produced the
    # ``cf_<key>`` query params are only rendered then, so this is the
    # contract the template enforces.
    filter_field_defs: list[TaxonomyFieldDef] = []
    cf_filter_values: dict[str, str] = {}
    if node_id is not None:
        filter_field_defs = _get_active_field_defs(db, node_id)
        if filter_field_defs:
            stmt, cf_filter_values = _apply_field_def_filters(
                stmt,
                db,
                filter_field_defs,
                {k: v for k, v in request.query_params.items() if isinstance(v, str)},
            )

    stmt = stmt.order_by(_LIST_ORDER, Item.sku)

    items = list(db.execute(stmt).scalars().all())
    rows = []
    for item in items:
        leaf_node = db.get(TaxonomyNode, item.taxonomy_node_id)
        # ``unit_cost`` cell: open-FIFO-layer unit cost for unique /
        # unique-variant items, blank otherwise. There's at most one open
        # layer for a unique-tracked item (qty 0/1; consumed → no open
        # layers; received → one open layer). Picks the most recent.
        unit_cost_cell: Any = ""
        if item.tracking_mode is TrackingMode.UNIQUE:
            open_layer = db.execute(
                select(CostLayer.unit_cost)
                .where(CostLayer.item_id == item.id)
                .where(CostLayer.qty_remaining > 0)
                .order_by(CostLayer.received_at.desc(), CostLayer.id.desc())
                .limit(1)
            ).scalar()
            if open_layer is not None:
                unit_cost_cell = open_layer
        rows.append(
            {
                "item": item,
                "category_label": _category_label(item, db),
                # CSV-friendly full breadcrumb (round-trips through the
                # upload's slash-path resolver). See ``_csv_rows_for_items``.
                "category_path": _node_breadcrumb(db, leaf_node) if leaf_node else "",
                "stage_label": _stage_label(item, db),
                "unit_cost": unit_cost_cell,
            }
        )

    # Catalog-driven columns: computed only when a node is picked. Empty list
    # otherwise. Every catalog entry is column-backed post-0024 so reads go
    # straight to ``Item.<column>`` via ``read_catalog_value``.
    catalog_columns: list[tuple[TaxonomyFieldDef, CatalogEntry]] = []
    catalog_cells_by_item: dict[int, list[str]] = {}
    if node_id is not None:
        catalog_columns = _catalog_columns_for(db, node_id)
        for item in items:
            catalog_cells_by_item[item.id] = [
                field_storage.format_for_display(entry, field_storage.read_catalog_value(item, entry))
                for _, entry in catalog_columns
            ]

    if (
        resp := csv_branch(
            format,
            filename=f"items_{show}.csv",
            headers=list(_ITEMS_CSV_HEADERS),
            rows=_csv_rows_for_items(rows),
        )
    ) is not None:
        return resp

    # Items table is gated behind category selection (slice 5 of the
    # catalog-driven taxonomy refactor): the user picks a category from the
    # filtered dropdown first; the table then shows that category's items
    # with both the fixed columns and the catalog-specific columns.
    #
    # Post-0024: every Item has every standard column. The dropdown shows
    # *all* categories so the user can always navigate. The "Pick a
    # category" gate stays in place only while at least one category has
    # picked fields somewhere; otherwise items render unconditionally.
    eligible_categories = _filter_category_options(db)
    any_picks_exist = (
        db.execute(select(TaxonomyFieldDef.id).limit(1)).first() is not None
    )
    show_items = node_id is not None or not any_picks_exist

    # Filter inputs render directly from the catalog (label, type, options).
    filter_specs = [
        {
            "key": fd.key,
            "label": entry.label,
            "type": entry.type.value,
            "options": list(entry.options),
        }
        for fd, entry in (
            (fd, CATALOG_BY_KEY[fd.key])
            for fd in filter_field_defs
            if fd.key in CATALOG_BY_KEY
        )
    ]
    return templates.TemplateResponse(
        request,
        "items_list.html",
        {
            "current_user": _user,
            "rows": rows,
            "show": show,
            "node_id": node_id,
            "requires_checkout_filter": requires_checkout_filter,
            "category_options": eligible_categories,
            "all_category_options": _filter_category_options(db),
            "filter_field_defs": filter_specs,
            "cf_filter_values": cf_filter_values,
            "catalog_columns": [
                {"key": fd.key, "label": entry.label, "type": entry.type.value}
                for fd, entry in catalog_columns
            ],
            "catalog_cells_by_item": catalog_cells_by_item,
            "show_items": show_items,
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
    leaf: TaxonomyNode | None = None
    if node_id is not None:
        # Pre-fill the category if the URL specified one; the form still
        # re-validates on POST so an archived/non-leaf id here just means the
        # user sees their pick rejected.
        form["taxonomy_node_id"] = str(node_id)
        leaf = db.get(TaxonomyNode, node_id)
        _apply_leaf_defaults(form, leaf)
    picked_columns = _picked_built_in_keys(db, node_id)
    return templates.TemplateResponse(
        request,
        "items_form.html",
        {
            "current_user": _user,
            "item": None,
            "form": form,
            "title": "New item",
            "action": "/admin/items",
            "leaf_options": _pickable_options(db),
            "pickable_options": _pickable_options(db),
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
            "can_edit_thresholds": True,
            "can_save": True,
            "field_visibility": _built_in_visibility_from_picks(picked_columns),
            "picked_column_keys": picked_columns,
            "form_taxonomy_breadcrumb": _breadcrumb_for_form(db, form["taxonomy_node_id"]),
            "form_sku_preview": _sku_preview_for_form(db, form["taxonomy_node_id"]),
        },
    )


@router.get("/_custom-fields", response_class=HTMLResponse)
def custom_fields_fragment(
    request: Request,
    taxonomy_node_id: str = "",
    include_defaults: str = "",
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX fragment: the full post-category content of the items form.

    Wired to the form's hidden ``#taxonomy_node_id`` via ``hx-get`` /
    ``hx-trigger="change"``. On change the response replaces the
    ``#item-fields-after-category`` container with the field sections
    rendered for the newly-picked leaf (built-in fields with the leaf's
    visibility map applied + custom fields). A ``#sku-preview`` OOB swap
    rides along so the SKU preview caption (outside the container) stays
    in sync with the picked leaf's next SKU.

    ``include_defaults=1`` (set via ``hx-vals`` on the create form, omitted
    on edit) tells the route to populate the form with the leaf's
    ``defaults_json`` values so a fresh pick brings the manager-configured
    defaults along. The edit form omits the flag so a Manager re-classifying
    an item doesn't silently lose the existing item's typed values — they
    re-render in place with whatever was previously stored.

    Empty / unparseable / archived / non-leaf ids render the container with
    a "pick a category" prompt and emit no preview. The POST handler
    re-validates on submit, so a hostile id here can't sneak past.

    Same role gating as the edit form (Manager + Office + Workshop). Office
    and Workshop see the form read-only, but ``hx-trigger="change"`` won't
    fire on a disabled input anyway; the permissive gate is just so a
    future widening of the edit form's writable surface doesn't silently
    403 on the fragment.
    """
    leaf: TaxonomyNode | None = None
    try:
        parsed_id = int(taxonomy_node_id)
    except (TypeError, ValueError):
        parsed_id = 0
    if parsed_id > 0:
        leaf = db.get(TaxonomyNode, parsed_id)
    form = _form_for_item(None)
    # The picked node id is what the partial conditional renders on. If the
    # leaf is invalid / archived / non-leaf the parsed_id was 0; show the
    # "pick a category" prompt rather than half-rendered fields.
    if leaf is not None:
        form["taxonomy_node_id"] = str(leaf.id)
    if include_defaults == "1":
        _apply_leaf_defaults(form, leaf)
    sku_preview_caption = ""
    if include_defaults == "1" and leaf is not None:
        composed = _compose_sku_preview(db, leaf)
        if composed:
            sku_preview_caption = f"Next SKU: {composed}"
    picked_columns = _picked_built_in_keys(db, leaf.id if leaf is not None else None)
    return templates.TemplateResponse(
        request,
        "items_form_fields.html",
        {
            "form": form,
            "ro": False,
            "item": None,
            "can_edit_thresholds": True,
            "field_visibility": _built_in_visibility_from_picks(picked_columns),
            "picked_column_keys": picked_columns,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
            "sku_preview_oob": include_defaults == "1",
            "sku_preview_caption": sku_preview_caption,
        },
    )


_PICKER_RESULTS_LIMIT = 20


@router.get("/_category-search", response_class=HTMLResponse)
def category_search(
    request: Request,
    q: str = "",
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """HTMX fragment: filtered pickable-options list for the items form picker.

    The leaf-only searchable picker (taxonomy refinement, Agent 4) replaces
    the previous ``<select>``-based category dropdown. As the user types in
    ``#taxonomy_node_search`` the JS dispatches an HTMX request here; this
    route returns the matching ``<li>`` rows that swap into
    ``#taxonomy_node_results``.

    Matching is a case-insensitive substring against the breadcrumb (e.g.
    ``"emma"`` matches ``"RTS Rings / Emma"``). Empty query returns the
    first ``_PICKER_RESULTS_LIMIT`` options in sort order.

    Same permissive role gating as ``_custom-fields`` (Manager + Office +
    Workshop): the picker shows up on the read-only Workshop view too;
    submit-time auth is enforced by the POST handlers.
    """
    query = (q or "").strip().lower()
    options = _pickable_options(db)
    if query:
        options = [opt for opt in options if query in str(opt["breadcrumb"]).lower()]
    options = options[:_PICKER_RESULTS_LIMIT]
    return templates.TemplateResponse(
        request,
        "items_category_options_partial.html",
        {"pickable_options": options},
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
    ring_size: str = Form(""),
    weight_grams: str = Form(""),
    stone_shape: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    # ``request.form()`` was previously needed to echo custom-field values
    # back on validation failure. Post-0024 every standard field flows
    # through a typed ``Form(...)`` parameter, so we no longer touch the
    # raw form. Kept the call in case the re-render path grows shape later.

    def _re_render(error: str) -> Response:
        # Validation failed — re-render the create form with the typed
        # values + the error message rather than letting HTTPException
        # bubble out as raw JSON.
        try:
            leaf_id_for_view = int((taxonomy_node_id or "").strip())
        except ValueError:
            leaf_id_for_view = 0
        form_view: dict[str, Any] = {
            "sku": (sku or "").strip(),
            "name": (name or "").strip(),
            "taxonomy_node_id": (taxonomy_node_id or "").strip(),
            "unit": (unit or "").strip(),
            "tracking_mode": tracking_mode or TrackingMode.QTY.value,
            "requires_checkout": bool(requires_checkout),
            "reorder_threshold": (reorder_threshold or "").strip(),
            "reorder_qty": (reorder_qty or "").strip(),
            "supplier_id": (supplier_id or "").strip(),
            "location_id": (location_id or "").strip(),
            "qr_code": (qr_code or "").strip(),
            "notes": (notes or "").strip(),
            "ring_size": (ring_size or "").strip(),
            "weight_grams": (weight_grams or "").strip(),
            "stone_shape": (stone_shape or "").strip(),
        }
        leaf_id_for_picks = leaf_id_for_view if leaf_id_for_view > 0 else None
        view_picked_columns = _picked_built_in_keys(db, leaf_id_for_picks)
        return templates.TemplateResponse(
            request,
            "items_form.html",
            {
                "current_user": user,
                "item": None,
                "form": form_view,
                "title": "New item",
                "action": "/admin/items",
                "leaf_options": _pickable_options(db),
                "pickable_options": _pickable_options(db),
                "supplier_options": _supplier_options(db),
                "location_options": _location_options(db),
                "tracking_modes": [m.value for m in TrackingMode],
                "can_edit_thresholds": True,
                "can_save": True,
                "field_visibility": _built_in_visibility_from_picks(view_picked_columns),
                "picked_column_keys": view_picked_columns,
                "error": error,
                "form_taxonomy_breadcrumb": _breadcrumb_for_form(db, form_view["taxonomy_node_id"]),
                "form_sku_preview": _sku_preview_for_form(db, form_view["taxonomy_node_id"]),
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # Resolve the picker destination up front so the archetype-aware
        # SKU + tracking_mode derivation has the node it needs. The
        # client-supplied ``sku`` form field is intentionally ignored on
        # create — the server owns SKU allocation under the taxonomy
        # refinement.
        picked = _resolve_leaf_node(db, taxonomy_node_id)
        archetype = effective_archetype(db, picked)
        if archetype is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category is missing an archetype (data integrity)",
            )
        derived_tracking_mode = _tracking_mode_for(archetype)

        # Per-leaf visibility for built-in fields. Hidden fields ignore any
        # submitted value (server auto-fills); optional fields skip required
        # validation. ``name`` left blank after _normalise is filled with
        # the allocated SKU below; ``unit`` fallback is handled inside
        # _normalise itself.
        visibility = _built_in_visibility_from_picks(_picked_built_in_keys(db, picked.id))
        leaf_defaults = picked.defaults_json or {}

        # Normalise the remaining fields against the *picked* leaf id
        # (which has already passed ``_resolve_leaf_node``). We feed a
        # placeholder ``sku`` so ``_normalise`` doesn't 400 on the
        # client-omitted value; the real SKU is allocated below and
        # overwrites the placeholder before the row hits the DB.
        fields = _normalise(
            db,
            sku="__pending__",  # placeholder; overwritten post-allocation
            name=name,
            taxonomy_node_id=str(picked.id),
            unit=unit,
            tracking_mode=derived_tracking_mode.value,
            requires_checkout=requires_checkout,
            reorder_threshold=reorder_threshold,
            reorder_qty=reorder_qty,
            supplier_id=supplier_id,
            location_id=location_id,
            qr_code=qr_code,
            notes=notes,
            ring_size=ring_size,
            weight_grams=weight_grams,
            stone_shape=stone_shape,
            visibility=visibility,
            leaf_defaults=leaf_defaults,
        )
        _check_qr_unique(db, fields["qr_code"])

        # Allocate SKU + sequence + destination leaf. For unique-variant
        # this also mints the auto-leaf the item will live on. Allocation
        # mutates ``next_sequence`` on the allocator row; a downstream
        # validation failure rolls everything back because we never call
        # ``db.commit`` until the very end of the route.
        allocated_sku, allocated_seq, dest_leaf = _allocate_sku(db, picked)
        fields["sku"] = allocated_sku
        fields["taxonomy_node_id"] = dest_leaf.id
        # If name was hidden or optional and left blank, fall back to the
        # SKU so the NOT-NULL ``name`` column always has a value. Items
        # surfaced in lists/search still get a stable identifier.
        if not fields["name"]:
            fields["name"] = allocated_sku
        _check_sku_unique(db, fields["sku"])
    except HTTPException as exc:
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        return _re_render(str(exc.detail))

    initial_stage_id = _initial_stage_id_for_leaf(db, dest_leaf)

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
        ring_size=fields["ring_size"],
        weight_grams=fields["weight_grams"],
        stone_shape=fields["stone_shape"],
        current_qty=Decimal("0"),
        assigned_sequence=allocated_seq,
        current_stage_id=initial_stage_id,
    )
    db.add(item)
    db.flush()

    audit_after: dict[str, Any] = {f: fields[f] for f in _FIELDS} | {
        "current_qty": item.current_qty,
        "assigned_sequence": allocated_seq,
    }

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
    # Redirect back to the items list scoped to the created item's category
    # so the user lands on a page that actually shows the new row (the list
    # is per-category since slice 5).
    return RedirectResponse(
        url=f"/admin/items?node_id={item.taxonomy_node_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{item_id}/edit", response_class=HTMLResponse)
def edit_item_form(
    request: Request,
    item_id: int,
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE, Role.WORKSHOP)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")
    form = _form_for_item(item)
    can_save = _can_save_item(_user)
    picked_columns = _picked_built_in_keys(db, item.taxonomy_node_id)
    return templates.TemplateResponse(
        request,
        "items_form.html",
        {
            "current_user": _user,
            "item": item,
            "form": form,
            "title": (f"Edit {item.name}" if can_save else f"View {item.name}"),
            "action": f"/admin/items/{item.id}",
            "leaf_options": _pickable_options(db, current_id=item.taxonomy_node_id),
            "pickable_options": _pickable_options(db, current_id=item.taxonomy_node_id),
            "supplier_options": _supplier_options(db, current_id=item.supplier_id),
            "location_options": _location_options(db, current_id=item.location_id),
            "tracking_modes": [m.value for m in TrackingMode],
            "can_edit_thresholds": _can_edit_thresholds(_user),
            "can_save": can_save,
            "field_visibility": _built_in_visibility_from_picks(picked_columns),
            "picked_column_keys": picked_columns,
            "form_taxonomy_breadcrumb": _breadcrumb_for_form(db, item.taxonomy_node_id),
            # Edit form does not preview a "next SKU" — SKU is already
            # allocated and immutable.
            "form_sku_preview": "",
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
    ring_size: str = Form(""),
    weight_grams: str = Form(""),
    stone_shape: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")

    # MISSION §3: Office cannot change reorder thresholds. Silently override
    # any inbound values with the existing row's values *before* validation,
    # so an Office user (or a tampered form) can't even fail on those fields.
    if not _can_edit_thresholds(user):
        reorder_threshold = str(item.reorder_threshold)
        reorder_qty = str(item.reorder_qty)

    def _re_render(error: str) -> Response:
        # Validation failed — re-render the edit form preserving the user's
        # typed values + error rather than letting HTTPException bubble out
        # as raw JSON. The "current_id" args on the option helpers keep an
        # archived FK assignment present in the dropdown.
        try:
            leaf_id_for_view = int((taxonomy_node_id or "").strip())
        except ValueError:
            leaf_id_for_view = item.taxonomy_node_id
        form_view: dict[str, Any] = {
            "sku": (sku or "").strip() or item.sku,
            "name": (name or "").strip(),
            "taxonomy_node_id": ((taxonomy_node_id or "").strip() or str(item.taxonomy_node_id)),
            "unit": (unit or "").strip(),
            "tracking_mode": tracking_mode or item.tracking_mode.value,
            "requires_checkout": bool(requires_checkout),
            "reorder_threshold": (reorder_threshold or "").strip(),
            "reorder_qty": (reorder_qty or "").strip(),
            "supplier_id": (supplier_id or "").strip(),
            "location_id": (location_id or "").strip(),
            "qr_code": (qr_code or "").strip(),
            "notes": (notes or "").strip(),
            "ring_size": (ring_size or "").strip(),
            "weight_grams": (weight_grams or "").strip(),
            "stone_shape": (stone_shape or "").strip(),
        }
        can_save = _can_save_item(user)
        view_picked_columns = _picked_built_in_keys(db, leaf_id_for_view)
        return templates.TemplateResponse(
            request,
            "items_form.html",
            {
                "current_user": user,
                "item": item,
                "form": form_view,
                "title": (f"Edit {item.name}" if can_save else f"View {item.name}"),
                "action": f"/admin/items/{item.id}",
                "leaf_options": _pickable_options(db, current_id=item.taxonomy_node_id),
                "pickable_options": _pickable_options(db, current_id=item.taxonomy_node_id),
                "supplier_options": _supplier_options(db, current_id=item.supplier_id),
                "location_options": _location_options(db, current_id=item.location_id),
                "tracking_modes": [m.value for m in TrackingMode],
                "can_edit_thresholds": _can_edit_thresholds(user),
                "can_save": can_save,
                "field_visibility": _built_in_visibility_from_picks(view_picked_columns),
                "picked_column_keys": view_picked_columns,
                "error": error,
                "form_taxonomy_breadcrumb": _breadcrumb_for_form(db, form_view["taxonomy_node_id"]),
                "form_sku_preview": "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
        # Resolve the (potentially-changed) leaf so we can fetch its
        # visibility map before normalising. ``_resolve_leaf_node`` runs
        # again inside ``_normalise``; the redundant lookup is cheap and
        # keeps the helper interface unchanged.
        edit_leaf = _resolve_leaf_node(db, taxonomy_node_id, current_id=item.taxonomy_node_id)
        visibility = _built_in_visibility_from_picks(_picked_built_in_keys(db, edit_leaf.id))
        leaf_defaults = edit_leaf.defaults_json or {}

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
            ring_size=ring_size,
            weight_grams=weight_grams,
            stone_shape=stone_shape,
            current_node_id=item.taxonomy_node_id,
            current_supplier_id=item.supplier_id,
            current_location_id=item.location_id,
            visibility=visibility,
            leaf_defaults=leaf_defaults,
        )
        # On edit, a blank-after-normalise ``name`` (visibility hidden /
        # optional, no input) means "keep the existing value" rather than
        # overwrite with the SKU.
        if not fields["name"]:
            fields["name"] = item.name
        _check_sku_unique(db, fields["sku"], exclude_id=item.id)
        _check_qr_unique(db, fields["qr_code"], exclude_id=item.id)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        return _re_render(str(exc.detail))

    diff = _diff(item, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(item, f, fields[f])
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

    return RedirectResponse(url="/admin/items", status_code=status.HTTP_303_SEE_OTHER)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")

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

    return RedirectResponse(url="/admin/items", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{item_id}/unarchive")
def unarchive_item(
    request: Request,
    item_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="item not found")

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

    return RedirectResponse(url="/admin/items", status_code=status.HTTP_303_SEE_OTHER)


# ===========================================================================
# CSV upload (items)
# ===========================================================================
#
# Bulk-create items from a CSV mirroring the download. Server-allocated
# SKU; ``current_qty`` ignored; ``tracking_mode`` forced from archetype.
# Custom-field columns ``cf_<key>`` are accepted when the key resolves to an
# active field def on the row's leaf (or any ancestor). See
# ``CSV uploads spec.md`` for the full contract.

_ITEMS_UPLOAD_KNOWN: set[str] = {
    "id",
    "sku",
    "name",
    "category",
    "stage",
    "unit",
    "tracking_mode",
    "current_qty",
    "reorder_threshold",
    "reorder_qty",
    "requires_checkout",
    "unit_cost",
    "ring_size",
    "weight_grams",
    "stone_shape",
}
_ITEMS_UPLOAD_REQUIRED: set[str] = {"category"}
_ITEMS_UPLOAD_COLUMNS = [
    {"name": "id", "required": False, "note": "blank = create; matching id = skip"},
    {
        "name": "sku",
        "required": False,
        "note": "ignored on create — server allocates; row warning if non-blank",
    },
    {
        "name": "name",
        "required": False,
        "note": "required unless leaf hides the name column",
    },
    {
        "name": "category",
        "required": True,
        "note": "numeric id OR slash-path like 'Rings / Silver / 925'",
    },
    {
        "name": "stage",
        "required": False,
        "note": "stage name on the leaf's top-level; defaults to is_initial if blank",
    },
    {
        "name": "unit",
        "required": False,
        "note": "required unless leaf hides the unit column; default 'ea'",
    },
    {
        "name": "tracking_mode",
        "required": False,
        "note": "ignored — derived from archetype (BULK→qty, UNIQUE/UV→unique)",
    },
    {
        "name": "current_qty",
        "required": False,
        "note": "ignored — items always start at 0 (stock-in is a separate movement)",
    },
    {"name": "reorder_threshold", "required": False, "note": "decimal; default 0"},
    {"name": "reorder_qty", "required": False, "note": "decimal; default 0"},
    {
        "name": "requires_checkout",
        "required": False,
        "note": "yes / no; default no",
    },
    {
        "name": "unit_cost",
        "required": False,
        "note": (
            "for unique / unique_variant items only; non-blank → synthesises a "
            "stock-in of qty=1 at that cost (creates a FIFO cost layer + a "
            "manual_in movement). Ignored on BULK rows. Blank → item starts at qty=0."
        ),
    },
    {
        "name": "ring_size",
        "required": False,
        "note": "free text; required if the leaf marks ring_size required",
    },
    {
        "name": "weight_grams",
        "required": False,
        "note": "decimal; required if the leaf marks weight_grams required",
    },
    {
        "name": "stone_shape",
        "required": False,
        "note": "free text; required if the leaf marks stone_shape required",
    },
]


def _resolve_category_for_upload(
    db: Session, raw: str
) -> tuple[TaxonomyNode | None, str | None]:
    """Resolve a category cell to a pickable ``TaxonomyNode`` (or error).

    Accepts:
    - numeric id (``"42"``)
    - slash-path (``"Rings / Silver / 925"``) — leading/trailing slashes
      ignored; segment names case-sensitive; resolves the deepest match.

    Returns ``(node, None)`` on success, ``(None, error)`` otherwise. The
    upload-row builder converts the error into a tagged ``RowResult``.
    """
    text = (raw or "").strip()
    if not text:
        return None, "category is required"

    # Numeric path: must be a non-archived, pickable node.
    if text.isdigit():
        try:
            node = db.get(TaxonomyNode, int(text))
        except ValueError:
            return None, f"category id {text!r} is not a whole number"
        if node is None:
            return None, f"category id {text} not found"
        if node.archived_at is not None:
            return None, "category is archived"
        if not _is_pickable(db, node):
            return None, (
                "category has sub-categories — pick a sub-category instead"
            )
        return node, None

    # Slash-path: walk down from depth 0.
    segments = [s.strip() for s in text.split("/")]
    segments = [s for s in segments if s]
    if not segments:
        return None, "category path is empty"

    current_parent: TaxonomyNode | None = None
    current_parent_id: int | None = None
    node = None
    for i, seg in enumerate(segments):
        stmt = (
            select(TaxonomyNode)
            .where(TaxonomyNode.name == seg)
            .where(TaxonomyNode.archived_at.is_(None))
        )
        if current_parent_id is None:
            stmt = stmt.where(TaxonomyNode.parent_id.is_(None))
        else:
            stmt = stmt.where(TaxonomyNode.parent_id == current_parent_id)
        matches = list(db.execute(stmt).scalars().all())
        if not matches:
            # Helpful message for the common UV-round-trip mistake: the user
            # downloads an item whose category cell reads
            # ``"RTS Rings / Emma / 003"`` (the auto-leaf path), strips the
            # id to "create a new item", and re-uploads. The trailing segment
            # never matches because UV auto-leaves are server-minted at
            # item-create time, not user-typed. Steer them at the parent
            # sub-cat — the upload accepts that and the server will mint a
            # fresh auto-leaf.
            if (
                current_parent is not None
                and i == len(segments) - 1
                and node_depth(db, current_parent) == 1
                and effective_archetype(db, current_parent) == Archetype.UNIQUE_VARIANT
            ):
                parent_path = " / ".join(segments[:i])
                return None, (
                    f"{seg!r} looks like a unique-variant auto-leaf; those are "
                    f"created automatically when items are added. Use the parent "
                    f"sub-category ({parent_path}) and leave the trailing segment off."
                )
            return None, f"no category matches segment {seg!r} in {text!r}"
        if len(matches) > 1:
            return None, (
                f"multiple categories match segment {seg!r} in {text!r}"
            )
        node = matches[0]
        current_parent = node
        current_parent_id = node.id

    assert node is not None
    if not _is_pickable(db, node):
        # Differentiate UV depth-2 auto-leaves (server-managed) from the
        # generic "has sub-categories" case so the user sees a fixable
        # instruction, not a dead end.
        if (
            node_depth(db, node) == 2
            and effective_archetype(db, node) == Archetype.UNIQUE_VARIANT
        ):
            parent = db.get(TaxonomyNode, node.parent_id) if node.parent_id else None
            parent_path = _node_breadcrumb(db, parent) if parent else "(parent)"
            return None, (
                f"{node.name!r} is a unique-variant auto-leaf and can't be picked "
                f"directly. Use the parent sub-category ({parent_path}) and the "
                f"server will allocate a new leaf."
            )
        return None, (
            "category has sub-categories — pick a sub-category instead"
        )
    return node, None


def _resolve_stage_for_upload(
    db: Session, leaf: TaxonomyNode, raw: str
) -> tuple[int | None, str | None]:
    """Resolve a stage name on the leaf's top-level. Blank → default-or-none."""
    text = (raw or "").strip()
    if not text:
        return _initial_stage_id_for_leaf(db, leaf), None
    chain = ancestor_chain(db, leaf)
    if not chain:
        return None, "category has no ancestor chain (data integrity)"
    top_level = chain[0]
    stage = db.execute(
        select(TaxonomyStage)
        .where(TaxonomyStage.top_level_node_id == top_level.id)
        .where(TaxonomyStage.name == text)
        .where(TaxonomyStage.archived_at.is_(None))
    ).scalar_one_or_none()
    if stage is None:
        return None, (
            f"stage {text!r} not found on category {top_level.name!r}"
        )
    return stage.id, None


def _parse_yes_no(raw: str, *, field_name: str) -> tuple[bool, str | None]:
    """Coerce a CSV yes/no cell. Blank → False (default)."""
    text = (raw or "").strip().lower()
    if text in ("", "no", "n", "false", "0"):
        return False, None
    if text in ("yes", "y", "true", "1"):
        return True, None
    return False, f"{field_name} must be yes or no (got {raw!r})"


def _parse_decimal_safe(
    raw: str, *, field_name: str
) -> tuple[Decimal, str | None]:
    """Like ``_parse_decimal`` but returns ``(value, error)`` instead of raising."""
    text = (raw or "").strip()
    if text == "":
        return Decimal("0"), None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return Decimal("0"), f"{field_name} must be a number"
    if value < 0:
        return Decimal("0"), f"{field_name} cannot be negative"
    return value, None


def _build_item_update_result(
    db: Session,
    row_number: int,
    raw: dict[str, str],
    existing: Item,
) -> RowResult:
    """Diff CSV cells against an existing item row.

    Updatable: ``name``, ``unit``, ``requires_checkout``, ``reorder_threshold``,
    ``reorder_qty``, ``stage``, ``ring_size``, ``weight_grams``,
    ``stone_shape``. Blank cells = no change.

    Locked (warn if the CSV tries to change them): ``sku``, ``current_qty``,
    ``tracking_mode``, ``category``, ``unit_cost``. The CSV is a metadata
    surface; SKU / qty / mode / cost / leaf membership all flow through
    dedicated paths.
    """
    changes: dict[str, Any] = {}
    before: dict[str, Any] = {}
    warnings: list[str] = []

    def _diff(field: str, new_val: Any) -> None:
        old_val = getattr(existing, field)
        if (old_val or None) != (new_val or None):
            changes[field] = new_val
            before[field] = old_val

    # --- Locked columns: warn if the CSV cell tries to change them. ---
    sku_raw = (raw.get("sku") or "").strip()
    if sku_raw and sku_raw != existing.sku:
        warnings.append("sku ignored on update — SKU is server-managed and immutable")
    qty_raw = (raw.get("current_qty") or "").strip()
    if qty_raw:
        try:
            if Decimal(qty_raw) != existing.current_qty:
                warnings.append(
                    "current_qty ignored on update — driven by stock movements"
                )
        except InvalidOperation:
            warnings.append("current_qty value unparseable; ignored on update")
    tracking_mode_raw = (raw.get("tracking_mode") or "").strip()
    if tracking_mode_raw and tracking_mode_raw != existing.tracking_mode.value:
        warnings.append(
            "tracking_mode ignored on update — derived from archetype"
        )
    unit_cost_raw = (raw.get("unit_cost") or "").strip()
    if unit_cost_raw:
        warnings.append(
            "unit_cost ignored on update — FIFO cost layers are append-only"
        )
    # Category locked on update: warn if the cell points elsewhere.
    cat_raw = (raw.get("category") or "").strip()
    if cat_raw:
        current_leaf = db.get(TaxonomyNode, existing.taxonomy_node_id)
        current_path = (
            _node_breadcrumb(db, current_leaf) if current_leaf is not None else ""
        )
        if cat_raw != current_path and cat_raw != str(existing.taxonomy_node_id):
            warnings.append(
                "category change via CSV not supported — use the per-item form"
            )

    # --- Editable columns ---
    # name (blank = leave alone).
    name_raw = (raw.get("name") or "").strip()
    if name_raw:
        _diff("name", name_raw)

    unit_raw = (raw.get("unit") or "").strip()
    if unit_raw:
        _diff("unit", unit_raw)

    rc_raw = (raw.get("requires_checkout") or "").strip()
    if rc_raw:
        rc_val, err = _parse_yes_no(rc_raw, field_name="requires_checkout")
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="requires_checkout", error_message=err,
            )
        _diff("requires_checkout", rc_val)

    threshold_raw = (raw.get("reorder_threshold") or "").strip()
    if threshold_raw:
        threshold, err = _parse_decimal_safe(threshold_raw, field_name="reorder_threshold")
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="reorder_threshold", error_message=err,
            )
        _diff("reorder_threshold", threshold)

    qty_o_raw = (raw.get("reorder_qty") or "").strip()
    if qty_o_raw:
        qty_val, err = _parse_decimal_safe(qty_o_raw, field_name="reorder_qty")
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="reorder_qty", error_message=err,
            )
        _diff("reorder_qty", qty_val)

    # Stage — resolve against the item's *current* leaf since category is
    # locked on update. Blank cell means "no change"; non-blank "" cell
    # means "clear stage" (we treat the same as blank — only an explicit
    # stage name updates).
    stage_raw = (raw.get("stage") or "").strip()
    if stage_raw:
        current_leaf = db.get(TaxonomyNode, existing.taxonomy_node_id)
        if current_leaf is None:  # pragma: no cover
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="stage",
                error_message="item's category is missing (data integrity)",
            )
        stage_id, err = _resolve_stage_for_upload(db, current_leaf, stage_raw)
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="stage", error_message=err,
            )
        _diff("current_stage_id", stage_id)

    ring_size_raw = (raw.get("ring_size") or "").strip()
    if ring_size_raw:
        _diff("ring_size", ring_size_raw)

    weight_raw = (raw.get("weight_grams") or "").strip()
    if weight_raw:
        weight_val, err = _parse_decimal_safe(weight_raw, field_name="weight_grams")
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="weight_grams", error_message=err,
            )
        _diff("weight_grams", weight_val)

    stone_raw = (raw.get("stone_shape") or "").strip()
    if stone_raw:
        _diff("stone_shape", stone_raw)

    if not changes:
        return RowResult(
            row_number=row_number, raw=raw, tag="skip",
            error_field="id",
            error_message=f"no changes (id={existing.id})",
            warnings=warnings,
        )
    return RowResult(
        row_number=row_number, raw=raw, tag="update",
        payload={"existing_id": existing.id, "changes": changes, "before": before},
        warnings=warnings,
    )


def _build_item_row_result(
    db: Session,
    row_number: int,
    raw: dict[str, str],
    *,
    existing_ids: set[int],
) -> RowResult:
    """Validate one row; return a ``RowResult`` ready for preview or commit."""
    # id handling first (skip / unknown / error).
    raw_id = (raw.get("id") or "").strip()
    if raw_id:
        try:
            id_int = int(raw_id)
        except ValueError:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="id", error_message="id must be a whole number",
            )
        if id_int in existing_ids:
            existing_item = db.get(Item, id_int)
            if existing_item is None:  # pragma: no cover — defensive
                return RowResult(
                    row_number=row_number, raw=raw, tag="error",
                    error_field="id",
                    error_message=f"item {id_int} not found",
                )
            return _build_item_update_result(db, row_number, raw, existing_item)
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="id",
            error_message=f"unknown id {id_int} — don't reuse ids from another database",
        )

    # Category: must resolve to a pickable node.
    node, err = _resolve_category_for_upload(db, raw.get("category", ""))
    if err is not None:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="category", error_message=err,
        )
    assert node is not None
    # SKU validation, custom fields, stage all hang off this node's visibility
    # + schema.
    visibility = _built_in_visibility_from_picks(_picked_built_in_keys(db, node.id))

    def _vis(field: str, state: str) -> bool:
        return visibility.get(field) == state

    # name: required iff picked + not hidden.
    name_raw = (raw.get("name") or "").strip()
    name: str | None
    if _vis("name", "hidden"):
        name = None  # auto-fill to SKU at commit
    else:
        if _vis("name", "required") and not name_raw:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="name", error_message="name is required",
            )
        name = name_raw or None

    # unit: required iff picked + not hidden; else fall back to leaf default
    # or 'ea'.
    unit_raw = (raw.get("unit") or "").strip()
    if _vis("unit", "hidden"):
        unit = (node.defaults_json or {}).get("unit") or "ea"
    else:
        if _vis("unit", "required") and not unit_raw:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="unit", error_message="unit is required",
            )
        unit = unit_raw or ((node.defaults_json or {}).get("unit") or "ea")

    threshold, err = _parse_decimal_safe(
        raw.get("reorder_threshold", ""), field_name="reorder_threshold"
    )
    if err is not None:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="reorder_threshold", error_message=err,
        )
    qty, err = _parse_decimal_safe(
        raw.get("reorder_qty", ""), field_name="reorder_qty"
    )
    if err is not None:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="reorder_qty", error_message=err,
        )

    requires_checkout, err = _parse_yes_no(
        raw.get("requires_checkout", ""), field_name="requires_checkout"
    )
    if err is not None:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="requires_checkout", error_message=err,
        )

    stage_id, err = _resolve_stage_for_upload(db, node, raw.get("stage", ""))
    if err is not None:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="stage", error_message=err,
        )

    # ``unit_cost`` is the per-row override for the FIFO cost layer +
    # synthetic stock-in movement that auto-receives qty=1 for unique /
    # unique-variant items. Blank → no auto-receive (item lands at qty=0,
    # current behaviour). For BULK items this column is ignored with a
    # warning if non-blank — bulk receipts have a separate flow.
    unit_cost_raw = (raw.get("unit_cost") or "").strip()
    unit_cost_for_receipt: Decimal | None = None
    archetype_for_row = effective_archetype(db, node)
    if unit_cost_raw:
        unit_cost_decimal, err = _parse_decimal_safe(
            unit_cost_raw, field_name="unit_cost"
        )
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="unit_cost", error_message=err,
            )
        if archetype_for_row == Archetype.BULK:
            # Don't synthesise a receipt for BULK — bulk stock-in has its own
            # flow (POs, manual stock-in). Warning surfaces but doesn't block.
            pass  # warning added below
        else:
            unit_cost_for_receipt = unit_cost_decimal

    # Promoted standard fields (post-0024). Each maps to an ``Item`` column.
    # Required-ness comes from the per-category visibility map.
    ring_size_raw = (raw.get("ring_size") or "").strip()
    if _vis("ring_size", "required") and not ring_size_raw:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="ring_size", error_message="ring_size is required",
        )
    weight_raw = (raw.get("weight_grams") or "").strip()
    weight_value: Decimal | None
    if weight_raw:
        weight_dec, err = _parse_decimal_safe(weight_raw, field_name="weight_grams")
        if err is not None:
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="weight_grams", error_message=err,
            )
        weight_value = weight_dec
    else:
        if _vis("weight_grams", "required"):
            return RowResult(
                row_number=row_number, raw=raw, tag="error",
                error_field="weight_grams", error_message="weight_grams is required",
            )
        weight_value = None
    stone_shape_raw = (raw.get("stone_shape") or "").strip()
    if _vis("stone_shape", "required") and not stone_shape_raw:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="stone_shape", error_message="stone_shape is required",
        )

    warnings: list[str] = []
    # User-supplied SKU on create: accepted for unique / unique_variant
    # items (each ring/variant often has its own pre-printed SKU); ignored
    # for BULK (server always allocates).
    user_sku = (raw.get("sku") or "").strip()
    user_sku_for_create: str | None = None
    if user_sku:
        if archetype_for_row == Archetype.BULK:
            warnings.append(
                "sku column ignored on BULK create — server allocates from leaf prefix"
            )
        else:
            # Verify uniqueness against existing SKUs (active + archived).
            from sqlalchemy import select as _select  # local alias

            collide = db.execute(
                _select(Item.id).where(Item.sku == user_sku).limit(1)
            ).first()
            if collide is not None:
                return RowResult(
                    row_number=row_number, raw=raw, tag="error",
                    error_field="sku",
                    error_message=f"sku {user_sku!r} already in use",
                )
            user_sku_for_create = user_sku
    if (raw.get("current_qty") or "").strip():
        warnings.append(
            "current_qty ignored — items always start at 0 (use stock-in to add)"
        )
    if (raw.get("tracking_mode") or "").strip():
        warnings.append(
            "tracking_mode ignored — derived from archetype on create"
        )
    if unit_cost_raw and archetype_for_row == Archetype.BULK:
        warnings.append(
            "unit_cost ignored on BULK items — use stock-in to receive qty + cost"
        )

    return RowResult(
        row_number=row_number,
        raw=raw,
        tag="new",
        payload={
            "category_node_id": node.id,
            "name": name,
            "unit": unit,
            "reorder_threshold": threshold,
            "reorder_qty": qty,
            "requires_checkout": requires_checkout,
            "stage_id": stage_id,
            "ring_size": ring_size_raw or None,
            "weight_grams": weight_value,
            "stone_shape": stone_shape_raw or None,
            "unit_cost_for_receipt": unit_cost_for_receipt,
            "user_sku_for_create": user_sku_for_create,
        },
        warnings=warnings,
    )


def _validate_items_upload(
    db: Session, headers: list[str], body: list[list[str]]
) -> list[RowResult]:
    check_required_and_known_headers(
        headers,
        known=_ITEMS_UPLOAD_KNOWN,
        required=_ITEMS_UPLOAD_REQUIRED,
    )
    existing_ids = {row.id for row in db.execute(select(Item.id)).all()}
    results: list[RowResult] = []
    for offset, row in enumerate(body):
        row_number = offset + 2
        raw = row_to_dict(headers, row)
        results.append(
            _build_item_row_result(
                db, row_number, raw,
                existing_ids=existing_ids,
            )
        )
    return results


def _summarise_item_row(r: RowResult) -> str:
    if r.payload is None:
        return ""
    if r.tag == "update":
        return "changed: " + ", ".join(sorted(r.payload.get("changes", {})))
    name = r.payload.get("name") or "(auto)"
    return f"name={name}"


@upload_router.get("/upload", response_class=HTMLResponse)
def upload_items_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "csv_upload_form.html",
        {
            "current_user": _user,
            "title": "Upload items CSV",
            "subtitle": (
                "Bulk-create items from a CSV. SKUs are allocated by the "
                "server; current_qty is ignored. Use stock-in to add stock."
            ),
            "intro_html": "",
            "action": "/admin/items/upload",
            "cancel_url": "/admin/items",
            "download_url": "/admin/items?format=csv&show=active",
            "expected_columns": _ITEMS_UPLOAD_COLUMNS,
        },
    )


@upload_router.post("/upload")
async def upload_items(
    request: Request,
    file: UploadFile = File(...),
    dry_run: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    raw_bytes = await file.read()
    is_dry_run = dry_run == "1"

    preview_ctx: dict[str, Any] = {
        "current_user": user,
        "title": "Items CSV — preview",
        "subtitle": "",
        "upload_url": "/admin/items/upload",
        "cancel_url": "/admin/items",
        "rows": [],
        "headers": [],
        "new_count": 0,
        "update_count": 0,
        "skip_count": 0,
        "error_count": 0,
        "top_level_error": None,
        "committed": False,
    }

    try:
        file_sha256, headers, body = read_upload(raw_bytes, filename=file.filename)
        results = _validate_items_upload(db, headers, body)
    except CsvUploadError as exc:
        preview_ctx["top_level_error"] = str(exc)
        return templates.TemplateResponse(request, "csv_upload_preview.html", preview_ctx)

    new_count = sum(1 for r in results if r.tag == "new")
    update_count = sum(1 for r in results if r.tag == "update")
    skip_count = sum(1 for r in results if r.tag == "skip")
    error_count = sum(1 for r in results if r.tag == "error")
    preview_ctx.update(
        {
            "headers": headers,
            "rows": [
                {
                    "row_number": r.row_number,
                    "tag": r.tag,
                    "error_field": r.error_field,
                    "error_message": r.error_message,
                    "warnings": r.warnings,
                    "summary": _summarise_item_row(r),
                }
                for r in results
            ],
            "new_count": new_count,
            "update_count": update_count,
            "skip_count": skip_count,
            "error_count": error_count,
        }
    )

    if is_dry_run or error_count > 0 or (new_count == 0 and update_count == 0):
        return templates.TemplateResponse(request, "csv_upload_preview.html", preview_ctx)

    # Commit pass: insert ``new`` rows + apply ``update`` rows + audit each.
    for r in results:
        if r.payload is None:
            continue
        if r.tag == "update":
            p = r.payload
            existing_item = db.get(Item, p["existing_id"])
            if existing_item is None:  # pragma: no cover
                continue
            for field, new_val in p["changes"].items():
                setattr(existing_item, field, new_val)
            json_before = {
                k: (str(v) if isinstance(v, Decimal) else v)
                for k, v in p["before"].items()
            }
            json_after = {
                k: (str(v) if isinstance(v, Decimal) else v)
                for k, v in p["changes"].items()
            }
            record_audit(
                db,
                actor=user,
                action="item.updated",
                entity_type="item",
                entity_id=existing_item.id,
                before=json_before,
                after=json_after,
            )
            continue
        if r.tag != "new":
            continue
        p = r.payload
        picked = db.get(TaxonomyNode, p["category_node_id"])
        if picked is None:  # pragma: no cover — defensive only
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category disappeared mid-upload",
            )
        archetype = effective_archetype(db, picked)
        if archetype is None:  # pragma: no cover — same as create_item
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="category is missing an archetype (data integrity)",
            )
        allocated_sku, allocated_seq, dest_leaf = _allocate_sku(db, picked)
        # User-supplied SKU overrides the server-allocated one for unique
        # / unique_variant items (the sequence + auto-leaf are still
        # server-minted; only the displayed SKU is the user's).
        user_sku = p.get("user_sku_for_create")
        if user_sku is not None:
            allocated_sku = user_sku
        item_name = p["name"] or allocated_sku
        # Re-derive stage from the *destination* leaf — for UV trees the
        # auto-leaf differs from the picked node, but the top-level (where
        # stages live) is the same, so this stays consistent.
        stage_id = p["stage_id"]
        if stage_id is None:
            stage_id = _initial_stage_id_for_leaf(db, dest_leaf)
        item = Item(
            sku=allocated_sku,
            name=item_name,
            taxonomy_node_id=dest_leaf.id,
            unit=p["unit"],
            tracking_mode=_tracking_mode_for(archetype),
            requires_checkout=p["requires_checkout"],
            reorder_threshold=p["reorder_threshold"],
            reorder_qty=p["reorder_qty"],
            ring_size=p["ring_size"],
            weight_grams=p["weight_grams"],
            stone_shape=p["stone_shape"],
            current_qty=Decimal("0"),
            assigned_sequence=allocated_seq,
            current_stage_id=stage_id,
        )
        db.add(item)
        db.flush()

        audit_after: dict[str, Any] = {
            "sku": allocated_sku,
            "name": item_name,
            "taxonomy_node_id": dest_leaf.id,
            "unit": p["unit"],
            "tracking_mode": _tracking_mode_for(archetype).value,
            "requires_checkout": p["requires_checkout"],
            "reorder_threshold": p["reorder_threshold"],
            "reorder_qty": p["reorder_qty"],
            "ring_size": p["ring_size"],
            "weight_grams": p["weight_grams"],
            "stone_shape": p["stone_shape"],
            "current_qty": Decimal("0"),
            "assigned_sequence": allocated_seq,
        }
        record_audit(
            db,
            actor=user,
            action="item.created",
            entity_type="item",
            entity_id=item.id,
            before=None,
            after=audit_after,
        )

        # Optional auto-receive for unique / unique-variant items when the
        # row carried a ``unit_cost`` value. Synthesises the same shape as
        # ``record_stock_in`` (movements.py): a ``StockMovement(IN qty=1)``
        # + a backing ``CostLayer``. Bumps ``item.current_qty`` to 1 and
        # writes a ``stock_movement.in`` audit row alongside the per-item
        # ``item.created`` row.
        cost_for_receipt = p["unit_cost_for_receipt"]
        if cost_for_receipt is not None:
            received_at = datetime.now(UTC)
            movement = StockMovement(
                item_id=item.id,
                type=MovementType.IN,
                qty=Decimal("1"),
                user_id=user.id,
                reason="csv_upload",
                note=f"auto-receive on items CSV upload (file_sha256={file_sha256[:12]}…)",
            )
            db.add(movement)
            db.flush()
            record_receipt(
                db,
                item=item,
                qty=Decimal("1"),
                unit_cost=cost_for_receipt,
                source=CostLayerSource.MANUAL_IN,
                movement=movement,
                received_at=received_at,
            )
            record_audit(
                db,
                actor=user,
                action="stock_movement.in",
                entity_type="stock_movement",
                entity_id=movement.id,
                before=None,
                after={
                    "item_id": item.id,
                    "qty": "1",
                    "unit_cost": str(cost_for_receipt),
                    "total_cost": (
                        str(movement.total_cost)
                        if movement.total_cost is not None
                        else None
                    ),
                    "source": CostLayerSource.MANUAL_IN.value,
                    "reason": movement.reason,
                    "note": movement.note,
                    "received_at": received_at.isoformat(),
                },
            )

    record_audit(
        db,
        actor=user,
        action="item.csv_uploaded",
        entity_type="item",
        entity_id=None,
        before=None,
        after={
            "count": new_count,
            "updated_count": update_count,
            "file_sha256": file_sha256,
        },
    )
    db.commit()
    _flash(
        request,
        f"Imported {new_count} new, updated {update_count} item(s) from CSV.",
    )
    return RedirectResponse(url="/admin/items", status_code=status.HTTP_303_SEE_OTHER)
