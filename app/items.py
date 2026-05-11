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
    Archetype,
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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="SKU is required")
    clean_name = (name or "").strip()
    if not clean_name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
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

    sup_id = _resolve_optional_supplier(db, supplier_id, current_id=current_supplier_id)
    loc_id = _resolve_optional_location(db, location_id, current_id=current_location_id)

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


def _custom_form_view_from_post(raw: Any, field_defs: list[TaxonomyFieldDef]) -> dict[str, Any]:
    """Build the items-form custom-field view from raw POST data without parsing.

    Used by the create / update re-render path so a validation failure
    doesn't wipe the user's typed values: every key in ``form["custom"]``
    is the raw string the operator submitted (or list, for multiselect),
    not the typed parsed value. Returning the raw text means the template
    re-fills the inputs verbatim — including malformed values like
    ``"1.2x"`` that a user can correct in place rather than re-type from
    scratch.

    Boolean: present + truthy → True; absent → False (HTML checkbox shape).
    Multiselect: multi-valued list (preserve order, drop blanks).
    Everything else: trimmed string.
    """
    out: dict[str, Any] = {}
    for fd in field_defs:
        name = f"cf_{fd.key}"
        if fd.type == FieldType.MULTISELECT:
            getlist = getattr(raw, "getlist", None)
            vals = getlist(name) if callable(getlist) else []
            out[fd.key] = [v for v in vals if isinstance(v, str) and v.strip()]
        elif fd.type == FieldType.BOOLEAN:
            v = raw.get(name)
            out[fd.key] = bool(v) and v != ""
        else:
            v = raw.get(name)
            out[fd.key] = v.strip() if isinstance(v, str) else ""
    return out


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


def _get_active_field_defs(db: Session, node_id: int) -> list[TaxonomyFieldDef]:
    """Active (non-archived) field defs for ``node_id``, ordered by sort_order then name."""
    stmt = (
        select(TaxonomyFieldDef)
        .where(TaxonomyFieldDef.node_id == node_id)
        .where(TaxonomyFieldDef.archived_at.is_(None))
        .order_by(TaxonomyFieldDef.sort_order, TaxonomyFieldDef.name)
    )
    return list(db.execute(stmt).scalars().all())


def _parse_custom_field(field_def: TaxonomyFieldDef, raw: str | list[str] | None) -> Any:
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
        values = [v.strip() for v in (raw if isinstance(raw, list) else []) if v and v.strip()]
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


def _collect_custom_fields(form: FormData, field_defs: list[TaxonomyFieldDef]) -> dict[int, Any]:
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


def _load_custom_field_value_rows(db: Session, item_id: int) -> dict[int, ItemFieldValue]:
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
            "leaf_options": _pickable_options(db),
            "pickable_options": _pickable_options(db),
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
            "can_edit_thresholds": True,
            "can_save": True,
            "field_defs": field_defs,
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
            oob_keys = {k for k in _DEFAULT_KEYS_TO_FORM if k in leaf.defaults_json}
            if leaf.defaults_json.get("requires_checkout") is True:
                oob_keys.add("requires_checkout")
    form["custom"] = _form_for_custom_fields(field_defs, {})
    # SKU preview OOB swap: when the create form posts a category change, also
    # update the ``#sku-preview`` caption alongside the custom-fields swap so
    # the user sees the server-allocated SKU shape for the picked leaf. On
    # edit (``include_defaults`` omitted) the SKU is already fixed, so no
    # preview is emitted; the form's static empty ``<output>`` stays empty.
    sku_preview_caption = ""
    if include_defaults == "1" and leaf is not None:
        composed = _compose_sku_preview(db, leaf)
        if composed:
            sku_preview_caption = f"Next SKU: {composed}"
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
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    # Read the form once up-front so the re-render path below can echo the
    # user's typed values (incl. custom fields) back into the template.
    # FastAPI caches this for the route's lifetime, so the typed ``Form()``
    # parameters above and ``request.form()`` below are the same object.
    raw_form = await request.form()

    def _re_render(error: str) -> Response:
        # Validation failed — re-render the create form with the typed
        # values + the error message rather than letting HTTPException
        # bubble out as raw JSON. Keeps the manager's other typed inputs
        # (incl. custom fields) so they can fix the problem in place.
        try:
            leaf_id_for_view = int((taxonomy_node_id or "").strip())
        except ValueError:
            leaf_id_for_view = 0
        view_field_defs = (
            _get_active_field_defs(db, leaf_id_for_view) if leaf_id_for_view > 0 else []
        )
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
            "custom": _custom_form_view_from_post(raw_form, view_field_defs),
        }
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
                "field_defs": view_field_defs,
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
        _check_sku_unique(db, fields["sku"])

        # Custom fields parse against the picked node's schema. For
        # unique-variant items the auto-leaf has no field defs (it's a
        # naked numeric leaf); field defs live on the depth-1 sub-cat the
        # user picked. For bulk / unique items the picked node is the
        # destination leaf, so the same id covers both cases.
        cf_node_id = picked.id
        field_defs = _get_active_field_defs(db, cf_node_id)
        parsed_custom = _collect_custom_fields(raw_form, field_defs)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        return _re_render(str(exc.detail))

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
        assigned_sequence=allocated_seq,
    )
    db.add(item)
    db.flush()

    custom_audit = _persist_custom_field_values(db, item.id, field_defs, parsed_custom)

    audit_after: dict[str, Any] = {f: fields[f] for f in _FIELDS} | {
        "current_qty": item.current_qty,
        "assigned_sequence": allocated_seq,
    }
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
    return RedirectResponse(url="/admin/items", status_code=status.HTTP_303_SEE_OTHER)


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
            "title": (f"Edit {item.name}" if can_save else f"View {item.name}"),
            "action": f"/admin/items/{item.id}",
            "leaf_options": _pickable_options(db, current_id=item.taxonomy_node_id),
            "pickable_options": _pickable_options(db, current_id=item.taxonomy_node_id),
            "supplier_options": _supplier_options(db, current_id=item.supplier_id),
            "location_options": _location_options(db, current_id=item.location_id),
            "tracking_modes": [m.value for m in TrackingMode],
            "can_edit_thresholds": _can_edit_thresholds(_user),
            "can_save": can_save,
            "field_defs": field_defs,
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

    raw_form = await request.form()

    def _re_render(error: str) -> Response:
        # Validation failed — re-render the edit form preserving the user's
        # typed values + error rather than letting HTTPException bubble out
        # as raw JSON. The "current_id" args on the option helpers keep an
        # archived FK assignment present in the dropdown.
        try:
            leaf_id_for_view = int((taxonomy_node_id or "").strip())
        except ValueError:
            leaf_id_for_view = item.taxonomy_node_id
        view_field_defs = _get_active_field_defs(db, leaf_id_for_view)
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
            "custom": _custom_form_view_from_post(raw_form, view_field_defs),
        }
        can_save = _can_save_item(user)
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
                "field_defs": view_field_defs,
                "error": error,
                "form_taxonomy_breadcrumb": _breadcrumb_for_form(db, form_view["taxonomy_node_id"]),
                "form_sku_preview": "",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    try:
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
        # Existing rows for archived defs (or for defs that no longer belong
        # to this leaf if the category just changed) are intentionally left
        # alone — MISSION §3 "existing items keep their stored values".
        # Required-flag validation runs here, so a 400 short-circuits before
        # any item update writes.
        field_defs = _get_active_field_defs(db, fields["taxonomy_node_id"])
        parsed_custom = _collect_custom_fields(raw_form, field_defs)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_400_BAD_REQUEST:
            raise
        return _re_render(str(exc.detail))
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
