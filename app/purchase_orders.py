"""Purchase orders (PO2): draft creation from the reorder dashboard.

First write surface in the PO/reorder track. Manager + Office click a
"Draft PO" button on a supplier group on ``/admin/reorder`` → POST to
``/admin/reorder/draft-po`` creates a draft ``PurchaseOrder`` with one
``PurchaseOrderLine`` per active item below threshold under that supplier.

Route surface (mounted across two prefixes — see ``app/main.py``):

- ``POST /admin/reorder/draft-po`` — Manager + Office. Build a draft from a
  supplier group.
- ``GET  /admin/purchase-orders`` — Manager + Office. List view (filterable by
  status).
- ``GET  /admin/purchase-orders/{po_id}`` — Manager + Office. Read-only
  detail view (header + lines + action links). Edit form lands in PO2b.

Editing per-line ``qty_ordered`` / ``expected_unit_cost``, plus PO ``notes``
and ``expected_date``, is **deferred to PO2b**. PO2 only covers create + read,
which keeps the slice landable and the audit shape simple
(``purchase_order.created`` only — ``purchase_order.updated`` lands with the
edit form in PO2b).

Validation on create:

- ``supplier_id`` parses as int; references an *active* ``Supplier``. Blank,
  non-int, unknown, or archived → 400.
- The supplier must have at least one active item below threshold (the same
  WHERE clause PO1's dashboard uses). Otherwise 400 — typically a stale POST
  after a concurrent stock-in cleared the threshold; the dashboard wouldn't
  have shown the button.

Defaults for line construction (from PO1's data):

- ``qty_ordered`` = ``item.reorder_qty`` if ``> 0`` else the deficit
  (``threshold - current_qty``) if ``> 0`` else ``Decimal("1")``. The
  third branch handles the at-threshold-zero-reorder cohort: order at least
  one to keep the line meaningful — the operator can edit later in PO2b.
- ``expected_unit_cost`` = the most recent ``CostLayer.unit_cost`` for the
  item (``ORDER BY received_at DESC, id DESC``) or ``None`` when the item has
  no layers yet.

Audit shape — ``purchase_order.created``: ``action="purchase_order.created"``,
``entity_type="purchase_order"``, ``entity_id=po.id``, ``before=None``,
``after={supplier_id, status, expected_date, notes, lines=[...]}`` where each
line is ``{item_id, qty_ordered (str), expected_unit_cost (str | None)}``.
Decimals stringified via ``str(decimal)`` (matches M2 / M3 / M4's audit
convention). Line ids are reconstructable from
``purchase_order_lines.po_id``.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import (
    CostLayer,
    Item,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    Supplier,
    User,
)
from app.template_env import templates

# Two routers in one module: the create endpoint lives under ``/admin/reorder``
# (it's the action triggered from the reorder dashboard); the list / detail
# endpoints live under ``/admin/purchase-orders``. Keeping them in this module
# lets the helper functions (default qty, last-cost lookup, audit shape) stay
# next to their only callers.
draft_router = APIRouter(prefix="/admin/reorder", tags=["purchase_orders"])
list_router = APIRouter(
    prefix="/admin/purchase-orders", tags=["purchase_orders"]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _parse_int_id(raw: str, *, field_name: str) -> int:
    """Parse a non-blank integer id. Blank / non-numeric raise 400."""
    text = (raw or "").strip()
    if text == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    try:
        return int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc


def _resolve_active_supplier(db: Session, supplier_id: int) -> Supplier:
    sup = db.get(Supplier, supplier_id)
    if sup is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier not found",
        )
    if sup.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier is archived",
        )
    return sup


def _low_stock_items_for_supplier(
    db: Session, supplier_id: int
) -> list[Item]:
    """Active items for the supplier where ``current_qty <= reorder_threshold``.

    Same predicate as PO1's dashboard, narrowed to one supplier. Ordered by
    SKU so the resulting PO lines render in a stable order.
    """
    stmt = (
        select(Item)
        .where(Item.archived_at.is_(None))
        .where(Item.supplier_id == supplier_id)
        .where(Item.current_qty <= Item.reorder_threshold)
        .order_by(Item.sku)
    )
    return list(db.execute(stmt).scalars().all())


def _default_qty_ordered(item: Item) -> Decimal:
    """Compute the auto-defaulted ``qty_ordered`` for a draft line.

    Preference order:
    1. ``item.reorder_qty`` when set (> 0).
    2. The deficit (``threshold - current_qty``) when positive.
    3. ``Decimal("1")`` as a last resort for the at-threshold-zero-reorder
       cohort — order at least one to keep the line meaningful; the operator
       can adjust on the edit form in PO2b.
    """
    if item.reorder_qty > 0:
        return item.reorder_qty
    deficit = item.reorder_threshold - item.current_qty
    if deficit > 0:
        return deficit
    return Decimal("1")


def _last_unit_cost(db: Session, item_id: int) -> Decimal | None:
    """Most recent cost layer's unit cost for an item, or ``None``.

    Used to default ``expected_unit_cost`` on a draft line. Newest by
    ``received_at`` (with id-tiebreak), regardless of ``qty_remaining`` —
    a fully-consumed layer is still the most recent unit-cost signal.
    """
    stmt = (
        select(CostLayer.unit_cost)
        .where(CostLayer.item_id == item_id)
        .order_by(CostLayer.received_at.desc(), CostLayer.id.desc())
        .limit(1)
    )
    row = db.execute(stmt).first()
    return row[0] if row is not None else None


# ---------------------------------------------------------------------------
# POST /admin/reorder/draft-po — create a draft PO from a supplier group
# ---------------------------------------------------------------------------


@draft_router.post("/draft-po")
def create_draft_po(
    request: Request,
    supplier_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    parsed_supplier_id = _parse_int_id(supplier_id, field_name="supplier")
    supplier = _resolve_active_supplier(db, parsed_supplier_id)
    items = _low_stock_items_for_supplier(db, supplier.id)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no low-stock items for this supplier",
        )

    po = PurchaseOrder(
        supplier_id=supplier.id,
        status=POStatus.DRAFT,
        created_by=user.id,
    )
    db.add(po)
    db.flush()

    line_audit: list[dict[str, Any]] = []
    for item in items:
        qty_ordered = _default_qty_ordered(item)
        expected_unit_cost = _last_unit_cost(db, item.id)
        line = PurchaseOrderLine(
            po_id=po.id,
            item_id=item.id,
            qty_ordered=qty_ordered,
            qty_received=Decimal("0"),
            expected_unit_cost=expected_unit_cost,
        )
        db.add(line)
        line_audit.append(
            {
                "item_id": item.id,
                "qty_ordered": str(qty_ordered),
                "expected_unit_cost": (
                    str(expected_unit_cost)
                    if expected_unit_cost is not None
                    else None
                ),
            }
        )
    db.flush()

    record_audit(
        db,
        actor=user,
        action="purchase_order.created",
        entity_type="purchase_order",
        entity_id=po.id,
        before=None,
        after={
            "supplier_id": supplier.id,
            "status": POStatus.DRAFT.value,
            "expected_date": None,
            "notes": None,
            "lines": line_audit,
        },
    )
    db.commit()

    _flash(
        request,
        f"Draft PO #{po.id} created for {supplier.name} "
        f"with {len(items)} line(s).",
    )
    return RedirectResponse(
        url=f"/admin/purchase-orders/{po.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# GET /admin/purchase-orders — list view
# ---------------------------------------------------------------------------


_STATUS_FILTER_VALUES: tuple[str, ...] = (
    "all",
    *(s.value for s in POStatus),
)


@list_router.get("", response_class=HTMLResponse)
def list_purchase_orders(
    request: Request,
    status_filter: str = "all",
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """List POs newest-first, optionally filtered by status."""
    if status_filter not in _STATUS_FILTER_VALUES:
        status_filter = "all"

    stmt = select(PurchaseOrder, Supplier).join(
        Supplier, PurchaseOrder.supplier_id == Supplier.id
    )
    if status_filter != "all":
        stmt = stmt.where(PurchaseOrder.status == POStatus(status_filter))
    stmt = stmt.order_by(
        PurchaseOrder.created_at.desc(), PurchaseOrder.id.desc()
    )

    rows: list[dict[str, Any]] = []
    for po, supplier in db.execute(stmt).all():
        rows.append(
            {
                "id": po.id,
                "supplier_name": supplier.name,
                "supplier_archived": supplier.archived_at is not None,
                "status": po.status.value,
                "created_at": po.created_at,
                "line_count": _count_lines(db, po.id),
            }
        )
    return templates.TemplateResponse(
        request,
        "purchase_orders_list.html",
        {
            "current_user": user,
            "rows": rows,
            "status_filter": status_filter,
            "status_options": _STATUS_FILTER_VALUES,
        },
    )


def _count_lines(db: Session, po_id: int) -> int:
    n = db.scalar(
        select(func.count(PurchaseOrderLine.id)).where(
            PurchaseOrderLine.po_id == po_id
        )
    )
    return int(n or 0)


# ---------------------------------------------------------------------------
# GET /admin/purchase-orders/{po_id} — detail view
# ---------------------------------------------------------------------------


@list_router.get("/{po_id}", response_class=HTMLResponse)
def purchase_order_detail(
    request: Request,
    po_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )

    supplier = db.get(Supplier, po.supplier_id)
    created_by_email: str | None = None
    if po.created_by is not None:
        actor = db.get(User, po.created_by)
        if actor is not None:
            created_by_email = actor.email

    line_stmt = (
        select(PurchaseOrderLine, Item)
        .join(Item, PurchaseOrderLine.item_id == Item.id)
        .where(PurchaseOrderLine.po_id == po.id)
        .order_by(Item.sku)
    )
    lines: list[dict[str, Any]] = []
    for line, item in db.execute(line_stmt).all():
        lines.append(
            {
                "id": line.id,
                "item_id": item.id,
                "item_sku": item.sku,
                "item_name": item.name,
                "item_unit": item.unit,
                "qty_ordered": line.qty_ordered,
                "qty_received": line.qty_received,
                "expected_unit_cost": line.expected_unit_cost,
            }
        )

    return templates.TemplateResponse(
        request,
        "purchase_order_detail.html",
        {
            "current_user": user,
            "po": po,
            "supplier": supplier,
            "created_by_email": created_by_email,
            "lines": lines,
        },
    )
