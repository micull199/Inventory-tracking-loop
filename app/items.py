"""Item CRUD against a taxonomy *leaf* node (I1a + I1b).

Items are the unblocking primitive for everything in MISSION §3 from "Stock
movements" onward. The shipped fields: SKU, name, leaf-node, unit, tracking
mode, requires-checkout flag, reorder thresholds, optional supplier/location/
QR/notes. ``current_qty`` is read-only at 0; only stock movements (M1+) move
it. Custom fields per the leaf's schema (S5 → I2), unique-tracked per-unit
rows (I3), QR label generation (I4), and movements (M1+) are deferred.

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

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import (
    Item,
    Location,
    Role,
    Supplier,
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


@router.get("", response_class=HTMLResponse)
def list_items(
    request: Request,
    show: str = "active",
    node_id: int | None = None,
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(Item)
    if show == "active":
        stmt = stmt.where(Item.archived_at.is_(None))
    else:
        stmt = stmt.where(Item.archived_at.is_not(None))
    if node_id is not None:
        stmt = stmt.where(Item.taxonomy_node_id == node_id)
    stmt = stmt.order_by(_LIST_ORDER, Item.sku)

    items = list(db.execute(stmt).scalars().all())
    rows = [
        {
            "item": item,
            "category_label": _category_label(item, db),
        }
        for item in items
    ]
    return templates.TemplateResponse(
        request,
        "items_list.html",
        {
            "current_user": _user,
            "rows": rows,
            "show": show,
            "node_id": node_id,
            "can_create": _user.role in (Role.MANAGER, Role.ADMIN),
            "can_archive": _user.role in (Role.MANAGER, Role.ADMIN),
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
    if node_id is not None:
        # Pre-fill the category if the URL specified one; the form still
        # re-validates on POST so an archived/non-leaf id here just means the
        # user sees their pick rejected.
        form["taxonomy_node_id"] = str(node_id)
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
        },
    )


@router.post("")
def create_item(
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

    record_audit(
        db,
        actor=user,
        action="item.created",
        entity_type="item",
        entity_id=item.id,
        before=None,
        after={f: fields[f] for f in _FIELDS} | {"current_qty": item.current_qty},
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
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )
    return templates.TemplateResponse(
        request,
        "items_form.html",
        {
            "current_user": _user,
            "item": item,
            "form": _form_for_item(item),
            "title": f"Edit {item.name}",
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
        },
    )


@router.post("/{item_id}")
def update_item(
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
