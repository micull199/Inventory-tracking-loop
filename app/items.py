"""Manager-owned item CRUD against a taxonomy *leaf* node (I1a).

Items are the unblocking primitive for everything in MISSION §3 from "Stock
movements" onward. This slice ships the core fields: SKU, name, leaf-node,
unit, tracking mode, requires-checkout flag, reorder thresholds, optional
supplier/location/QR/notes. ``current_qty`` is read-only at 0; only stock
movements (M1+) move it. Custom fields per the leaf's schema (S5 → I2),
unique-tracked per-unit rows (I3), QR label generation (I4), and movements
(M1+) are deferred.

Access: ``Manager`` and ``Admin`` (admins always pass ``require_role``).
Workshop and Office both 403 — Office *eventually* gets read+edit per
MISSION §3 ("Office: items, movements, POs … cannot change reorder
thresholds"), but that's a separate access shape (some fields editable, some
not), so it's deferred to I1b. Workshop is read-only per §3 ("view items"),
also deferred.

URL shape mirrors ``app/suppliers.py`` / ``app/locations.py`` — flat-by-id —
because items don't have a parent in the URL the way sub-cats do; the leaf
node is just one of the form fields.
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


def _resolve_leaf_node(db: Session, raw_node_id: str) -> TaxonomyNode:
    """Load a taxonomy node by id and verify it's a non-archived leaf."""
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
    if node.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot create items under an archived category",
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


def _resolve_optional_supplier(db: Session, raw: str) -> int | None:
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
    if supplier.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier is archived",
        )
    return supplier.id


def _resolve_optional_location(db: Session, raw: str) -> int | None:
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
    if location.archived_at is not None:
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
) -> dict[str, Any]:
    """Strip / parse / validate every form field. Returns the value-shape stored on the row.

    Raises ``HTTPException(400)`` on any validation error. Uniqueness checks
    against the DB (sku / qr_code) live in their own callers because they need
    the per-route ``exclude_id``.
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

    node = _resolve_leaf_node(db, taxonomy_node_id)
    mode = _coerce_tracking_mode(tracking_mode)

    threshold = _parse_decimal(reorder_threshold, field_name="reorder threshold")
    qty = _parse_decimal(reorder_qty, field_name="reorder quantity")

    sup_id = _resolve_optional_supplier(db, supplier_id)
    loc_id = _resolve_optional_location(db, location_id)

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


def _leaf_options(db: Session) -> list[dict[str, Any]]:
    """Active leaf-node options for the form's category <select>.

    Top-level nodes appear only when they have no active children (i.e. they
    *are* the leaf). Sub-cats appear under their parent. Output is shaped for
    the template — list of ``{id, label, parent_id, sort_order}`` rows
    pre-sorted: parent block first, then its children indented.
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
        else:
            options.append(
                {"id": top.id, "label": top.name, "is_group": False}
            )
    return options


def _supplier_options(db: Session) -> list[Supplier]:
    return list(
        db.execute(
            select(Supplier)
            .where(Supplier.archived_at.is_(None))
            .order_by(Supplier.name)
        )
        .scalars()
        .all()
    )


def _location_options(db: Session) -> list[Location]:
    return list(
        db.execute(
            select(Location)
            .where(Location.archived_at.is_(None))
            .order_by(Location.name)
        )
        .scalars()
        .all()
    )


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
    _user: User = Depends(require_role(Role.MANAGER)),
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
    _user: User = Depends(require_role(Role.MANAGER)),
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
            "leaf_options": _leaf_options(db),
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
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
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )

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
