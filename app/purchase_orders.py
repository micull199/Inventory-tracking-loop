"""Purchase orders (PO2 + PO2b): draft creation, edit, and cancel.

First write surface in the PO/reorder track. Manager + Office click a
"Draft PO" button on a supplier group on ``/admin/reorder`` → POST to
``/admin/reorder/draft-po`` creates a draft ``PurchaseOrder`` with one
``PurchaseOrderLine`` per active item below threshold under that supplier.
PO2b adds the editable surface MISSION §3 requires plus a cancel path.

Route surface (mounted across two prefixes — see ``app/main.py``):

- ``POST /admin/reorder/draft-po`` — Manager + Office. Build a draft from a
  supplier group (PO2).
- ``GET  /admin/purchase-orders`` — Manager + Office. List view (filterable by
  status) (PO2).
- ``GET  /admin/purchase-orders/{po_id}`` — Manager + Office. Detail view —
  renders as an *edit form* when ``po.status == draft``; renders as a
  read-only banner otherwise.
- ``POST /admin/purchase-orders/{po_id}`` — Manager + Office (PO2b). Apply a
  diff to a draft PO: top-level ``expected_date`` / ``notes`` plus per-line
  ``qty_ordered_<line_id>`` / ``expected_unit_cost_<line_id>``. 400 if the PO
  is not a draft.
- ``POST /admin/purchase-orders/{po_id}/cancel`` — Manager + Office (PO2b).
  Flip status to ``cancelled``. 400 if the PO is not a draft.

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

Audit shape — ``purchase_order.updated`` (PO2b): sparse diff. Top-level
``expected_date`` / ``notes`` appear in ``before`` / ``after`` only when
changed. Line changes appear under a ``lines`` sub-key as a list of
``{line_id, <changed_field>: <value>}`` entries (only changed fields per
line; lines with no changes are omitted entirely). Decimals stringify via
``str(decimal)``. A no-op submit doesn't write an audit row at all.

Audit shape — ``purchase_order.cancelled`` (PO2b): ``before={"status":
"draft"}``, ``after={"status": "cancelled"}``. No line snapshot — cancelling
flips only the PO row's status.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
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
from app.pdf import render_po_pdf
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
            "is_draft": po.status == POStatus.DRAFT,
        },
    )


# ---------------------------------------------------------------------------
# PO2b — edit a draft PO
# ---------------------------------------------------------------------------
#
# Validation order: status (PO must be draft) → top-level fields → per-line
# fields. Each step short-circuits on the first error so a failed request
# leaves no DB write behind. The diff is computed in memory after parsing,
# *before* any column on the PO or its lines is mutated, so a no-op submit
# can be detected and the audit row skipped.


_NOTES_MAX_LEN = 2000


def _parse_optional_date(raw: str, *, field_name: str) -> date | None:
    """Blank → None; otherwise parse ISO date or 400."""
    text = (raw or "").strip()
    if text == "":
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be ISO date (YYYY-MM-DD)",
        ) from exc


def _parse_optional_notes(raw: str) -> str | None:
    """Strip; blank → None; reject if longer than the column limit."""
    text = (raw or "").strip()
    if text == "":
        return None
    if len(text) > _NOTES_MAX_LEN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"notes longer than {_NOTES_MAX_LEN} characters",
        )
    return text


def _parse_positive_decimal(raw: str, *, field_name: str) -> Decimal:
    """Parse a strictly-positive decimal. Blank, zero, negative all 400."""
    text = (raw or "").strip()
    if text == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc
    if value <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be positive",
        )
    return value


def _parse_optional_non_negative_decimal(
    raw: str, *, field_name: str
) -> Decimal | None:
    """Blank → None; non-blank parses as Decimal >= 0 or 400."""
    text = (raw or "").strip()
    if text == "":
        return None
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


def _require_draft(po: PurchaseOrder) -> None:
    """400 unless ``po.status == draft``. Used by edit and cancel."""
    if po.status != POStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"PO is {po.status.value}; only drafts can be edited"
            ),
        )


def _po_lines(db: Session, po_id: int) -> list[PurchaseOrderLine]:
    stmt = (
        select(PurchaseOrderLine)
        .where(PurchaseOrderLine.po_id == po_id)
        .order_by(PurchaseOrderLine.id)
    )
    return list(db.execute(stmt).scalars().all())


@list_router.post("/{po_id}")
async def update_purchase_order(
    request: Request,
    po_id: int,
    expected_date: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Apply a sparse diff to a draft PO.

    Per-line ``qty_ordered_<line_id>`` and ``expected_unit_cost_<line_id>``
    come in via ``request.form()`` because the line ids aren't known at
    function-signature time. The route reads only the keys whose ``line_id``
    suffix matches an existing line on this PO (extra keys are silently
    dropped — defends against stale tabs that hold removed line ids).
    """
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )
    _require_draft(po)

    # FastAPI's ``Form(...)`` extracts the static keys; the per-line dynamic
    # keys come via Starlette's ``request.form()`` (cached after the typed
    # Form() params parse, so this is free).
    form_data = await request.form()

    new_expected_date = _parse_optional_date(
        expected_date, field_name="expected_date"
    )
    new_notes = _parse_optional_notes(notes)

    lines = _po_lines(db, po.id)
    parsed_line_changes: list[
        tuple[PurchaseOrderLine, Decimal, Decimal | None]
    ] = []
    for line in lines:
        qty_key = f"qty_ordered_{line.id}"
        cost_key = f"expected_unit_cost_{line.id}"
        if qty_key not in form_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"missing field {qty_key}",
            )
        if cost_key not in form_data:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"missing field {cost_key}",
            )
        qty_raw = str(form_data[qty_key] or "")
        cost_raw = str(form_data[cost_key] or "")
        new_qty = _parse_positive_decimal(qty_raw, field_name="qty_ordered")
        new_cost = _parse_optional_non_negative_decimal(
            cost_raw, field_name="expected_unit_cost"
        )
        parsed_line_changes.append((line, new_qty, new_cost))

    # All inputs parsed. Now compute the sparse diff *before* any mutation
    # so a no-op skips the audit row entirely.
    before_top: dict[str, Any] = {}
    after_top: dict[str, Any] = {}
    if po.expected_date != new_expected_date:
        before_top["expected_date"] = (
            po.expected_date.isoformat() if po.expected_date else None
        )
        after_top["expected_date"] = (
            new_expected_date.isoformat() if new_expected_date else None
        )
    if po.notes != new_notes:
        before_top["notes"] = po.notes
        after_top["notes"] = new_notes

    line_before_audit: list[dict[str, Any]] = []
    line_after_audit: list[dict[str, Any]] = []
    for line, new_qty, new_cost in parsed_line_changes:
        line_before: dict[str, Any] = {"line_id": line.id}
        line_after: dict[str, Any] = {"line_id": line.id}
        if line.qty_ordered != new_qty:
            line_before["qty_ordered"] = str(line.qty_ordered)
            line_after["qty_ordered"] = str(new_qty)
        if line.expected_unit_cost != new_cost:
            line_before["expected_unit_cost"] = (
                str(line.expected_unit_cost)
                if line.expected_unit_cost is not None
                else None
            )
            line_after["expected_unit_cost"] = (
                str(new_cost) if new_cost is not None else None
            )
        # Only emit a line audit entry if at least one field changed
        # (line_id alone means "unchanged"; skip).
        if len(line_before) > 1:
            line_before_audit.append(line_before)
            line_after_audit.append(line_after)

    has_top_change = bool(before_top)
    has_line_change = bool(line_before_audit)

    if has_top_change or has_line_change:
        # Apply the diff. Top-level first (idempotent), then per-line.
        po.expected_date = new_expected_date
        po.notes = new_notes
        for line, new_qty, new_cost in parsed_line_changes:
            line.qty_ordered = new_qty
            line.expected_unit_cost = new_cost

        before: dict[str, Any] = dict(before_top)
        after: dict[str, Any] = dict(after_top)
        if has_line_change:
            before["lines"] = line_before_audit
            after["lines"] = line_after_audit

        record_audit(
            db,
            actor=user,
            action="purchase_order.updated",
            entity_type="purchase_order",
            entity_id=po.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Draft PO #{po.id} saved.")
    else:
        _flash(request, f"Draft PO #{po.id} — no changes.")

    return RedirectResponse(
        url=f"/admin/purchase-orders/{po.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# PO2b — cancel a draft PO
# ---------------------------------------------------------------------------


@list_router.post("/{po_id}/cancel")
def cancel_purchase_order(
    request: Request,
    po_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )
    _require_draft(po)

    po.status = POStatus.CANCELLED
    record_audit(
        db,
        actor=user,
        action="purchase_order.cancelled",
        entity_type="purchase_order",
        entity_id=po.id,
        before={"status": POStatus.DRAFT.value},
        after={"status": POStatus.CANCELLED.value},
    )
    db.commit()

    _flash(request, f"Draft PO #{po.id} cancelled.")
    return RedirectResponse(
        url=f"/admin/purchase-orders/{po.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# PO3 — Render a PO as a PDF
# ---------------------------------------------------------------------------


@list_router.get("/{po_id}/pdf")
def purchase_order_pdf(
    po_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Return a PDF rendering of the PO.

    Manager + Office. 404 on unknown id; 400 if the PO is cancelled (nothing
    to send). All other statuses render — drafts are a legit preview case.
    Disposition is ``inline`` so browsers preview in a new tab; PO4 will
    re-use the bytes as an email attachment.
    """
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )
    if po.status == POStatus.CANCELLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="PO is cancelled; cannot generate PDF",
        )

    supplier = db.get(Supplier, po.supplier_id)
    supplier_view: dict[str, Any] = {
        "name": supplier.name if supplier is not None else "(unknown)",
        "archived": (
            supplier.archived_at is not None if supplier is not None else False
        ),
    }

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
                "sku": item.sku,
                "name": item.name,
                "unit": item.unit,
                "qty_ordered": line.qty_ordered,
                "expected_unit_cost": line.expected_unit_cost,
            }
        )

    pdf_bytes = render_po_pdf(
        po={
            "id": po.id,
            "status": po.status.value,
            "created_at": po.created_at,
            "expected_date": po.expected_date,
            "notes": po.notes,
        },
        supplier=supplier_view,
        lines=lines,
    )
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="po-{po.id}.pdf"',
        },
    )
