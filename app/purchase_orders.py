"""Purchase orders (PO2 + PO2b + PO3 + PO4 + PO5): draft creation, edit,
cancel, PDF, send, and receive.

First write surface in the PO/reorder track. Manager + Office click a
"Draft PO" button on a supplier group on ``/admin/reorder`` → POST to
``/admin/reorder/draft-po`` creates a draft ``PurchaseOrder`` with one
``PurchaseOrderLine`` per active item below threshold under that supplier.
PO2b adds the editable surface MISSION §3 requires plus a cancel path.
PO5 closes the loop: receive against a sent PO with actual unit costs,
which creates ``StockMovement(type=IN, po_id=po.id)`` rows + cost layers
(via the engine) and flips ``po.status`` to ``partially_received`` /
``received`` based on cumulative line state.

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

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.config import settings as app_settings
from app.cost_engine import record_receipt
from app.csv_export import csv_branch
from app.db import get_session
from app.email_backend import (
    EmailAttachment,
    EmailMessage,
    get_email_backend,
)
from app.models import (
    CostLayer,
    CostLayerSource,
    Item,
    MovementType,
    POStatus,
    PurchaseOrder,
    PurchaseOrderLine,
    Role,
    StockMovement,
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

_PO_LIST_CSV_HEADERS: list[str] = [
    "po_id",
    "supplier",
    "supplier_archived",
    "status",
    "line_count",
    "created_at",
]


def _po_list_csv_rows(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Map view-shaped PO list rows to CSV cell values.

    The ``supplier_archived`` cell renders as the literal string ``"yes"`` or
    ``"no"`` rather than ``"True"`` / ``"False"``: spreadsheet receivers tend
    to find yes/no easier to filter on. Documented in ``app/csv_export.py``'s
    module docstring as a per-caller pre-coercion.
    """
    return [
        [
            r["id"],
            r["supplier_name"],
            "yes" if r["supplier_archived"] else "no",
            r["status"],
            r["line_count"],
            r["created_at"],
        ]
        for r in rows
    ]


@list_router.get("")
def list_purchase_orders(
    request: Request,
    status_filter: str = "all",
    format: str = "",
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """List POs newest-first, optionally filtered by status.

    ``?format=csv`` triggers a downloadable CSV; anything else (blank,
    ``html``, garbage) renders the existing HTML — same silent-coerce posture
    as ``status_filter``.
    """
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

    if (
        resp := csv_branch(
            format,
            filename=f"purchase_orders_{status_filter}.csv",
            headers=_PO_LIST_CSV_HEADERS,
            rows=_po_list_csv_rows(rows),
        )
    ) is not None:
        return resp

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
            "is_receivable": po.status in _RECEIVABLE_STATUSES,
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


# ---------------------------------------------------------------------------
# PO4 — Email the PO PDF to the supplier (draft → sent)
# ---------------------------------------------------------------------------


def _po_pdf_view(
    db: Session, po: PurchaseOrder, supplier: Supplier
) -> bytes:
    """Build the PDF view + render bytes. Same shape as the PO3 GET handler."""
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
    return render_po_pdf(
        po={
            "id": po.id,
            "status": po.status.value,
            "created_at": po.created_at,
            "expected_date": po.expected_date,
            "notes": po.notes,
        },
        supplier={
            "name": supplier.name,
            "archived": supplier.archived_at is not None,
        },
        lines=lines,
    )


def _build_send_message(
    *,
    po: PurchaseOrder,
    supplier: Supplier,
    line_count: int,
    pdf_bytes: bytes,
    sender: str,
) -> EmailMessage:
    """Build the EmailMessage view for the supplier's PDF email.

    Body is intentionally short HTML — letterhead / branding is a future
    polish pass (see PO3 self-critique).
    """
    expected_line: str
    if po.expected_date is not None:
        expected_line = (
            f"<p>Expected delivery: {po.expected_date.isoformat()}.</p>"
        )
    else:
        expected_line = ""
    notes_block = (
        f"<p>Notes: {po.notes}</p>" if po.notes else ""
    )
    html = (
        "<html><body>"
        f"<p>Hi {supplier.name},</p>"
        f"<p>Please find attached purchase order #{po.id} from UC, "
        f"covering {line_count} line(s).</p>"
        f"{expected_line}"
        f"{notes_block}"
        "<p>Reply to this email to confirm receipt or raise any issues.</p>"
        "<p>Thanks,<br>UC Inventory</p>"
        "</body></html>"
    )
    return EmailMessage(
        sender=sender,
        recipient=supplier.email or "",
        subject=f"Purchase Order #{po.id} from UC",
        html_body=html,
        attachments=[
            EmailAttachment(
                filename=f"po-{po.id}.pdf",
                content_type="application/pdf",
                content=pdf_bytes,
            )
        ],
    )


@list_router.post("/{po_id}/send")
def send_purchase_order(
    request: Request,
    po_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Send a draft PO to its supplier as an email with the PDF attached.

    Validation order (all 400 *before* delivery / DB write — atomic):

    1. PO exists (404).
    2. ``po.status == draft`` (400 via ``_require_draft`` — non-drafts cannot
       be sent; resending is out of scope for v1).
    3. Supplier still active (400 — defence in depth; the dashboard hides the
       button for archived-supplier groups).
    4. Supplier has a non-blank email (400 — show the user a clear message).

    On success: render the PDF; deliver via the configured email backend
    (``settings.email_backend``); flip ``po.status`` to ``sent``; set
    ``po.sent_at = now()``; write a ``purchase_order.sent`` audit row;
    commit; flash; 303 to the detail page. Delivery happens *before* the DB
    flip — if the backend raises, no state change and no audit row.
    """
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )
    _require_draft(po)

    supplier = db.get(Supplier, po.supplier_id)
    if supplier is None or supplier.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="supplier is not active",
        )
    supplier_email = (supplier.email or "").strip()
    if supplier_email == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "supplier has no email address — add one before sending"
            ),
        )

    pdf_bytes = _po_pdf_view(db, po, supplier)

    line_count = _count_lines(db, po.id)
    message = _build_send_message(
        po=po,
        supplier=supplier,
        line_count=line_count,
        pdf_bytes=pdf_bytes,
        sender=app_settings.smtp_from,
    )
    backend = get_email_backend(app_settings)
    backend.send(message)

    sent_at = datetime.now(UTC)
    po.status = POStatus.SENT
    po.sent_at = sent_at
    record_audit(
        db,
        actor=user,
        action="purchase_order.sent",
        entity_type="purchase_order",
        entity_id=po.id,
        before={"status": POStatus.DRAFT.value},
        after={
            "status": POStatus.SENT.value,
            "sent_at": sent_at.isoformat(),
            "to_email": supplier_email,
        },
    )
    db.commit()

    _flash(request, f"PO #{po.id} sent to {supplier_email}.")
    return RedirectResponse(
        url=f"/admin/purchase-orders/{po.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# PO5 — Receive against a PO (sent / partially_received → partially_received
# / received)
# ---------------------------------------------------------------------------
#
# Operator visits ``/admin/purchase-orders/{po_id}/receive`` (GET → form), fills
# per-line ``received_<line_id>`` + ``cost_<line_id>``, submits. For each line
# with ``received > 0``: build a ``StockMovement(type=IN, po_id=po.id)``,
# flush, call the FIFO engine's ``record_receipt`` (creates a cost layer with
# ``source=PO_RECEIPT``, bumps ``item.current_qty``, sets ``movement.total_cost``),
# increment ``line.qty_received``. After all lines are processed, flip
# ``po.status`` to ``RECEIVED`` if every line is fully received, else
# ``PARTIALLY_RECEIVED``. Audit row carries the new status + a per-line list of
# what was received in this transaction (only lines with ``received > 0``).
#
# Audit shape — ``purchase_order.received``: ``before={"status": prev_status}``,
# ``after={"status": new_status, "lines": [{"line_id", "received_qty" (str),
# "actual_unit_cost" (str), "movement_id"} ...]}``. The movement_id pins each
# line entry to the exact ``StockMovement`` row it created (and the cost layer
# is reachable from there via ``cost_layers.source_movement_id``). A no-op
# submit (every line received=0) writes no movement, no layer, no audit row,
# and flashes "no receipts".
#
# Status guard: only ``sent`` and ``partially_received`` can be received. Draft
# / received / cancelled all 400 — drafts must be sent first; received POs are
# closed; cancelled POs aren't expected to receive (cancellation happens before
# any receipt). Receiving against a received PO would over-receive. The
# operator who receives "extra" stock outside what the PO ordered records a
# manual stock-in (M2) instead.
#
# No over-receipt: cumulative ``line.qty_received + new_received`` cannot exceed
# ``line.qty_ordered``. The operator who orders 100 but receives 110 must first
# bump qty_ordered via the (currently draft-only) edit route — not modeled in
# v1 since edit is gated on draft status. Pragmatic v1 path: record a manual
# stock-in for the +10 and document via the audit log + reason.


def _parse_optional_non_negative_decimal_or_zero(
    raw: str, *, field_name: str
) -> Decimal:
    """Parse ``raw`` as a non-negative Decimal; blank → ``Decimal("0")``.

    PO5 receive uses this for the per-line ``received_<id>`` field — a blank or
    "0" entry means "received nothing on this line this time", which is a
    perfectly valid partial-receive scenario (the operator only got one of two
    suppliers' line items). Negative / non-numeric still 400.
    """
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


_RECEIVABLE_STATUSES: tuple[POStatus, ...] = (
    POStatus.SENT,
    POStatus.PARTIALLY_RECEIVED,
)


def _require_receivable(po: PurchaseOrder) -> None:
    """400 unless the PO is in a status that accepts receipts."""
    if po.status not in _RECEIVABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"PO is {po.status.value}; only sent or partially-received "
                "POs can be received against"
            ),
        )


def _receive_lines_view(
    db: Session, po_id: int
) -> list[dict[str, Any]]:
    """Per-line view dicts for the receive form.

    Same SKU-ordering as the read-only render. Adds ``outstanding`` =
    ``qty_ordered - qty_received`` so the template can show the operator how
    much is still expected on each line.
    """
    line_stmt = (
        select(PurchaseOrderLine, Item)
        .join(Item, PurchaseOrderLine.item_id == Item.id)
        .where(PurchaseOrderLine.po_id == po_id)
        .order_by(Item.sku)
    )
    out: list[dict[str, Any]] = []
    for line, item in db.execute(line_stmt).all():
        out.append(
            {
                "id": line.id,
                "item_id": item.id,
                "item_sku": item.sku,
                "item_name": item.name,
                "item_unit": item.unit,
                "qty_ordered": line.qty_ordered,
                "qty_received": line.qty_received,
                "outstanding": line.qty_ordered - line.qty_received,
                "expected_unit_cost": line.expected_unit_cost,
            }
        )
    return out


@list_router.get("/{po_id}/receive", response_class=HTMLResponse)
def receive_purchase_order_form(
    request: Request,
    po_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the receive form. Status guard: sent / partially_received only."""
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )
    _require_receivable(po)
    supplier = db.get(Supplier, po.supplier_id)
    return templates.TemplateResponse(
        request,
        "purchase_order_receive_form.html",
        {
            "current_user": user,
            "po": po,
            "supplier": supplier,
            "lines": _receive_lines_view(db, po.id),
        },
    )


@list_router.post("/{po_id}/receive")
async def receive_purchase_order(
    request: Request,
    po_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Receive against a sent / partially-received PO.

    Reads per-line ``received_<line_id>`` + ``cost_<line_id>`` from the form
    body. For each line where the new received qty is > 0:

    1. Build a ``StockMovement(type=IN, po_id=po.id, qty=new_received)``,
       flush.
    2. Call ``record_receipt(item, qty, unit_cost, source=PO_RECEIPT,
       movement, received_at=now())`` — engine creates the cost layer + bumps
       ``item.current_qty`` + sets ``movement.total_cost``.
    3. Increment ``line.qty_received`` by ``new_received``.

    After processing, flip ``po.status`` to ``RECEIVED`` if every line is now
    fully received (``qty_received == qty_ordered``), else
    ``PARTIALLY_RECEIVED``. Write a ``purchase_order.received`` audit row and
    commit. An all-zero submit is a no-op (no movement, no layer, no audit, no
    status change; flash "no receipts").
    """
    po = db.get(PurchaseOrder, po_id)
    if po is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="PO not found"
        )
    _require_receivable(po)
    prev_status = po.status

    form_data = await request.form()
    lines = _po_lines(db, po.id)

    # Per-line raw inputs. Captured up-front so a validation re-render
    # below can echo what the operator typed back into the form.
    submitted_received: dict[int, str] = {}
    submitted_cost: dict[int, str] = {}
    for line in lines:
        submitted_received[line.id] = str(
            form_data.get(f"received_{line.id}", "") or ""
        ).strip()
        submitted_cost[line.id] = str(
            form_data.get(f"cost_{line.id}", "") or ""
        ).strip()

    def _re_render(error: str) -> Response:
        # Re-render the receive form with the typed values + error message
        # rather than letting the HTTPException bubble up as raw JSON. The
        # operator keeps the per-line costs they already typed.
        supplier = db.get(Supplier, po.supplier_id)
        return templates.TemplateResponse(
            request,
            "purchase_order_receive_form.html",
            {
                "current_user": user,
                "po": po,
                "supplier": supplier,
                "lines": _receive_lines_view(db, po.id),
                "submitted_received": submitted_received,
                "submitted_cost": submitted_cost,
                "error": error,
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    # First pass: parse + validate every line. No mutation yet — atomic
    # validation per the rest of the PO module's pattern.
    parsed: list[tuple[PurchaseOrderLine, Decimal, Decimal]] = []
    for line in lines:
        rec_raw = submitted_received[line.id]
        cost_raw = submitted_cost[line.id]
        try:
            new_received = _parse_optional_non_negative_decimal_or_zero(
                rec_raw, field_name="received qty"
            )
        except HTTPException as exc:
            if exc.status_code != status.HTTP_400_BAD_REQUEST:
                raise
            return _re_render(str(exc.detail))
        # Reject over-receipt before any movement is created.
        if line.qty_received + new_received > line.qty_ordered:
            return _re_render(
                f"line {line.id}: cannot receive more than ordered "
                f"(ordered {line.qty_ordered}, received "
                f"{line.qty_received}, requested {new_received})"
            )
        # Cost is only required when something is being received on this line.
        # When ``new_received == 0`` we don't validate the cost field — the
        # operator can leave it blank and we don't write a movement anyway.
        actual_cost: Decimal
        if new_received > 0:
            try:
                actual_cost = _parse_non_negative_decimal_required(
                    cost_raw, field_name="actual unit cost"
                )
            except HTTPException as exc:
                if exc.status_code != status.HTTP_400_BAD_REQUEST:
                    raise
                return _re_render(str(exc.detail))
        else:
            actual_cost = Decimal("0")
        parsed.append((line, new_received, actual_cost))

    # Second pass: write the movements + cost layers + bump line.qty_received.
    received_at = datetime.now(UTC)
    audit_lines: list[dict[str, Any]] = []
    for line, new_received, actual_cost in parsed:
        if new_received <= 0:
            continue
        item = db.get(Item, line.item_id)
        # Item shouldn't be missing (PO line FK + RESTRICT); defensive 400.
        if item is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"line {line.id}: item not found",
            )
        movement = StockMovement(
            item_id=item.id,
            type=MovementType.IN,
            qty=new_received,
            user_id=user.id,
            po_id=po.id,
        )
        db.add(movement)
        db.flush()
        record_receipt(
            db,
            item=item,
            qty=new_received,
            unit_cost=actual_cost,
            source=CostLayerSource.PO_RECEIPT,
            movement=movement,
            received_at=received_at,
        )
        line.qty_received = line.qty_received + new_received
        audit_lines.append(
            {
                "line_id": line.id,
                "received_qty": str(new_received),
                "actual_unit_cost": str(actual_cost),
                "movement_id": movement.id,
            }
        )

    if not audit_lines:
        # All-zero submit — bail without changing state.
        _flash(request, f"PO #{po.id} — no receipts entered.")
        return RedirectResponse(
            url=f"/admin/purchase-orders/{po.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    # Decide the new status from the cumulative state across every line on the
    # PO (not just those received this transaction).
    fully = all(line.qty_received >= line.qty_ordered for line in lines)
    new_status = POStatus.RECEIVED if fully else POStatus.PARTIALLY_RECEIVED
    po.status = new_status

    record_audit(
        db,
        actor=user,
        action="purchase_order.received",
        entity_type="purchase_order",
        entity_id=po.id,
        before={"status": prev_status.value},
        after={
            "status": new_status.value,
            "lines": audit_lines,
        },
    )
    db.commit()

    if new_status == POStatus.RECEIVED:
        _flash(
            request,
            f"PO #{po.id} fully received "
            f"({len(audit_lines)} line(s) updated).",
        )
    else:
        _flash(
            request,
            f"PO #{po.id} partial receipt recorded "
            f"({len(audit_lines)} line(s) updated).",
        )
    return RedirectResponse(
        url=f"/admin/purchase-orders/{po.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def _parse_non_negative_decimal_required(
    raw: str, *, field_name: str
) -> Decimal:
    """Parse a non-negative decimal; blank rejects.

    Distinct from ``_parse_optional_non_negative_decimal`` (which accepts
    blank → None) and from ``movements._parse_non_negative_decimal`` (which is
    in the movements module). Used by the receive route for the actual unit
    cost on a line where qty > 0 — zero allowed for sample stock, blank
    rejected because the operator must affirm a price.
    """
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
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} cannot be negative",
        )
    return value
