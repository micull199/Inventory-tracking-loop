"""Internal Transfer Orders — two-event movement of stock between locations.

Slice 2 of the in-transit / stages scope addition (see PROGRESS.md).

A Transfer Order (TO) is a document representing stock moving between two UC
locations with separate ship + receive events. Compared to the existing instant-
flip ``TRANSFER`` movement under ``/admin/items/{id}/transfer`` (a "quick
relocate" path, preserved unchanged), a TO captures the *in transit* state:
while ``shipped_at`` is set and ``received_at`` is not, each line's item has
``location_id = NULL`` and the TO is visible in the in-transit listing.

Route surface (mounted at ``/admin/transfers``):

- ``GET  /``               — list TOs, filtered by status.
- ``GET  /new``            — render create form (lines added one-at-a-time).
- ``POST /``               — create a draft TO with the supplied lines.
- ``GET  /{to_id}``        — detail; renders editable form if DRAFT, read-only
                              otherwise.
- ``POST /{to_id}``        — update a DRAFT TO (locations, notes, lines).
- ``POST /{to_id}/cancel`` — cancel a DRAFT TO (no movements written).
- ``POST /{to_id}/ship``   — ship: emit ``TRANSFER`` movements on each line's
                              item; flip status to SHIPPED; null each item's
                              ``location_id``.
- ``POST /{to_id}/receive``— receive: emit ``TRANSFER`` movements on each line's
                              item; flip status to RECEIVED; set each item's
                              ``location_id = destination_location_id``.

Cost engine is **never** invoked — matches the existing instant-flip behaviour.

Roles: Manager + Office can create / ship / receive / cancel. Workshop +
Admin (via blanket override) read-only on the list / detail.

v1 limitations (in line with the user-confirmed plan):

- Full receipt only. Discrepancies after the fact go through the existing
  adjustment movement path.
- Whole-item location flip (one location per item). No per-location-qty split.
- Archived items rejected at ship time (consistent with movements / checkouts).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import (
    Item,
    Location,
    MovementType,
    Role,
    StockMovement,
    TransferOrder,
    TransferOrderLine,
    TransferOrderStatus,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/transfers", tags=["transfers"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_LIST_ORDER = case(
    (TransferOrder.status == TransferOrderStatus.DRAFT, 0),
    (TransferOrder.status == TransferOrderStatus.SHIPPED, 1),
    (TransferOrder.status == TransferOrderStatus.RECEIVED, 2),
    else_=3,
)


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _parse_int(raw: str, *, field_name: str) -> int:
    cleaned = (raw or "").strip()
    if not cleaned:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an integer",
        ) from exc


def _parse_optional_int(raw: str) -> int | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        return int(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="value must be an integer",
        ) from exc


def _parse_optional_decimal(raw: str, *, field_name: str) -> Decimal:
    """Parse ``qty`` for a TO line; defaults to ``Decimal('0')`` when blank.

    Transfers are whole-item location flips in v1, so ``qty`` is informational
    only. We still require a valid Decimal when one is supplied so a typo
    surfaces as a 400 rather than a silent drop.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return Decimal("0")
    try:
        return Decimal(cleaned)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc


def _parse_optional_date(raw: str) -> date | None:
    cleaned = (raw or "").strip()
    if not cleaned:
        return None
    try:
        return date.fromisoformat(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="expected arrival must be an ISO date (YYYY-MM-DD)",
        ) from exc


def _location_or_400(db: Session, loc_id: int, *, field_name: str) -> Location:
    loc = db.get(Location, loc_id)
    if loc is None or loc.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} not found or archived",
        )
    return loc


def _location_options(db: Session) -> list[Location]:
    return list(
        db.execute(
            select(Location).where(Location.archived_at.is_(None)).order_by(Location.name)
        )
        .scalars()
        .all()
    )


def _item_options(db: Session) -> list[Item]:
    return list(
        db.execute(
            select(Item).where(Item.archived_at.is_(None)).order_by(Item.sku)
        )
        .scalars()
        .all()
    )


def _get_to_or_404(db: Session, to_id: int) -> TransferOrder:
    to = db.get(TransferOrder, to_id)
    if to is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="transfer order not found"
        )
    return to


def _lines_for(db: Session, to_id: int) -> list[TransferOrderLine]:
    return list(
        db.execute(
            select(TransferOrderLine)
            .where(TransferOrderLine.transfer_order_id == to_id)
            .order_by(TransferOrderLine.id)
        )
        .scalars()
        .all()
    )


def _line_audit_shape(line: TransferOrderLine, item: Item | None) -> dict[str, Any]:
    return {
        "line_id": line.id,
        "item_id": line.item_id,
        "item_sku": item.sku if item else None,
        "qty": str(line.qty),
        "ship_movement_id": line.ship_movement_id,
        "receive_movement_id": line.receive_movement_id,
    }


_TRANSFER_CSV_HEADERS: list[str] = [
    "id",
    "source_location",
    "destination_location",
    "status",
    "shipped_at",
    "received_at",
    "expected_arrival",
    "carrier",
    "tracking_number",
    "line_count",
]


def _csv_rows(db: Session, tos: list[TransferOrder]) -> list[list[Any]]:
    location_ids = {to.source_location_id for to in tos} | {
        to.destination_location_id for to in tos
    }
    locations: dict[int, Location] = {}
    if location_ids:
        for loc in (
            db.execute(select(Location).where(Location.id.in_(location_ids)))
            .scalars()
            .all()
        ):
            locations[loc.id] = loc
    rows: list[list[Any]] = []
    for to in tos:
        src = locations.get(to.source_location_id)
        dst = locations.get(to.destination_location_id)
        line_count = (
            db.execute(
                select(func.count())
                .select_from(TransferOrderLine)
                .where(TransferOrderLine.transfer_order_id == to.id)
            ).scalar()
            or 0
        )
        rows.append(
            [
                to.id,
                src.name if src else "",
                dst.name if dst else "",
                to.status.value,
                to.shipped_at,
                to.received_at,
                to.expected_arrival,
                to.carrier or "",
                to.tracking_number or "",
                line_count,
            ]
        )
    return rows


# ---------------------------------------------------------------------------
# List / detail
# ---------------------------------------------------------------------------


@router.get("")
def list_transfers(
    request: Request,
    show: str = "all",
    format: str = "",
    _user: User = Depends(require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    valid_filters = {"all", "draft", "shipped", "received", "cancelled", "open"}
    if show not in valid_filters:
        show = "all"

    stmt = select(TransferOrder)
    if show == "open":
        stmt = stmt.where(
            TransferOrder.status.in_(
                [TransferOrderStatus.DRAFT, TransferOrderStatus.SHIPPED]
            )
        )
    elif show != "all":
        stmt = stmt.where(TransferOrder.status == TransferOrderStatus(show))
    stmt = stmt.order_by(_LIST_ORDER, TransferOrder.created_at.desc())

    rows = list(db.execute(stmt).scalars().all())

    if (
        resp := csv_branch(
            format,
            filename=f"transfers_{show}.csv",
            headers=_TRANSFER_CSV_HEADERS,
            rows=_csv_rows(db, rows),
        )
    ) is not None:
        return resp

    locations_by_id = {loc.id: loc for loc in _location_options(db)}
    line_counts: dict[int, int] = {}
    if rows:
        line_count_rows = db.execute(
            select(
                TransferOrderLine.transfer_order_id,
                func.count(TransferOrderLine.id),
            )
            .where(TransferOrderLine.transfer_order_id.in_([to.id for to in rows]))
            .group_by(TransferOrderLine.transfer_order_id)
        ).all()
        line_counts = {to_id: int(count) for to_id, count in line_count_rows}

    return templates.TemplateResponse(
        request,
        "transfers_list.html",
        {
            "current_user": _user,
            "transfers": rows,
            "locations": locations_by_id,
            "line_counts": line_counts,
            "show": show,
            "can_create": _user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN),
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_transfer_form(
    request: Request,
    user: User = Depends(require_role(Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "transfers_form.html",
        {
            "current_user": user,
            "transfer": None,
            "form": {
                "source_location_id": "",
                "destination_location_id": "",
                "expected_arrival": "",
                "carrier": "",
                "tracking_number": "",
                "notes": "",
            },
            "lines": [],
            "title": "New transfer order",
            "action": "/admin/transfers",
            "location_options": _location_options(db),
            "item_options": _item_options(db),
        },
    )


@router.post("")
async def create_transfer(
    request: Request,
    source_location_id: str = Form(""),
    destination_location_id: str = Form(""),
    expected_arrival: str = Form(""),
    carrier: str = Form(""),
    tracking_number: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    src_id = _parse_int(source_location_id, field_name="source location")
    dst_id = _parse_int(destination_location_id, field_name="destination location")
    if src_id == dst_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source and destination must differ",
        )
    src = _location_or_400(db, src_id, field_name="source location")
    dst = _location_or_400(db, dst_id, field_name="destination location")
    expected = _parse_optional_date(expected_arrival)

    raw = await request.form()
    # ``item_id_<N>`` / ``qty_<N>`` pairs in the same row index.
    line_specs: list[tuple[int, Decimal]] = []
    seen_items: set[int] = set()
    for key, value in raw.multi_items():
        if not key.startswith("item_id_"):
            continue
        idx = key.removeprefix("item_id_")
        item_raw = value if isinstance(value, str) else ""
        item_id = _parse_optional_int(item_raw)
        if item_id is None:
            continue
        qty_raw = raw.get(f"qty_{idx}", "")
        qty = _parse_optional_decimal(qty_raw if isinstance(qty_raw, str) else "", field_name="qty")
        if item_id in seen_items:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="duplicate item on this transfer order",
            )
        seen_items.add(item_id)
        item = db.get(Item, item_id)
        if item is None or item.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"item {item_id} not found or archived",
            )
        line_specs.append((item_id, qty))

    to = TransferOrder(
        source_location_id=src.id,
        destination_location_id=dst.id,
        status=TransferOrderStatus.DRAFT,
        expected_arrival=expected,
        carrier=(carrier or "").strip() or None,
        tracking_number=(tracking_number or "").strip() or None,
        notes=(notes or "").strip() or None,
        created_by=user.id,
    )
    db.add(to)
    db.flush()

    for item_id, qty in line_specs:
        db.add(
            TransferOrderLine(
                transfer_order_id=to.id, item_id=item_id, qty=qty
            )
        )

    record_audit(
        db,
        actor=user,
        action="transfer_order.created",
        entity_type="transfer_order",
        entity_id=to.id,
        before=None,
        after={
            "source_location_id": src.id,
            "destination_location_id": dst.id,
            "status": to.status.value,
            "expected_arrival": expected,
            "carrier": to.carrier,
            "tracking_number": to.tracking_number,
            "notes": to.notes,
            "lines": [{"item_id": item_id, "qty": str(qty)} for item_id, qty in line_specs],
        },
    )
    db.commit()
    _flash(request, f"Transfer order #{to.id} created.")
    return RedirectResponse(
        url=f"/admin/transfers/{to.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/{to_id}", response_class=HTMLResponse)
def transfer_detail(
    request: Request,
    to_id: int,
    user: User = Depends(require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    to = _get_to_or_404(db, to_id)
    lines = _lines_for(db, to.id)
    items_by_id = {
        i.id: i
        for i in db.execute(
            select(Item).where(Item.id.in_([line.item_id for line in lines]))
        )
        .scalars()
        .all()
    } if lines else {}
    src = db.get(Location, to.source_location_id)
    dst = db.get(Location, to.destination_location_id)

    return templates.TemplateResponse(
        request,
        "transfers_detail.html",
        {
            "current_user": user,
            "transfer": to,
            "lines": lines,
            "items": items_by_id,
            "source": src,
            "destination": dst,
            "can_edit": (
                to.status == TransferOrderStatus.DRAFT
                and user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN)
            ),
            "can_ship": (
                to.status == TransferOrderStatus.DRAFT
                and user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN)
            ),
            "can_receive": (
                to.status == TransferOrderStatus.SHIPPED
                and user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN)
            ),
            "can_cancel": (
                to.status == TransferOrderStatus.DRAFT
                and user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN)
            ),
        },
    )


@router.post("/{to_id}/cancel")
def cancel_transfer(
    request: Request,
    to_id: int,
    user: User = Depends(require_role(Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    to = _get_to_or_404(db, to_id)
    if to.status != TransferOrderStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only DRAFT transfer orders can be cancelled",
        )
    before_status = to.status
    to.status = TransferOrderStatus.CANCELLED
    record_audit(
        db,
        actor=user,
        action="transfer_order.cancelled",
        entity_type="transfer_order",
        entity_id=to.id,
        before={"status": before_status.value},
        after={"status": to.status.value},
    )
    db.commit()
    _flash(request, f"Transfer order #{to.id} cancelled.")
    return RedirectResponse(
        url=f"/admin/transfers/{to.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{to_id}/ship")
def ship_transfer(
    request: Request,
    to_id: int,
    reason: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_role(Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    to = _get_to_or_404(db, to_id)
    if to.status != TransferOrderStatus.DRAFT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only DRAFT transfer orders can be shipped",
        )
    lines = _lines_for(db, to.id)
    if not lines:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot ship an empty transfer order",
        )

    # Validate every line's item is active and currently at the source location.
    items_by_id: dict[int, Item] = {}
    for line in lines:
        item = db.get(Item, line.item_id)
        if item is None or item.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"item {line.item_id} not found or archived",
            )
        if item.location_id != to.source_location_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"item {item.sku} is not at the source location — refresh and "
                    "either update the TO or use a quick transfer to relocate first"
                ),
            )
        items_by_id[item.id] = item

    shipped_at = datetime.now(UTC)
    clean_reason = (reason or "").strip() or "transfer_order_ship"
    clean_note = (note or "").strip() or None
    line_audit: list[dict[str, Any]] = []
    for line in lines:
        item = items_by_id[line.item_id]
        movement = StockMovement(
            item_id=item.id,
            type=MovementType.TRANSFER,
            qty=line.qty,
            user_id=user.id,
            reason=clean_reason,
            note=clean_note,
            transfer_order_id=to.id,
        )
        db.add(movement)
        db.flush()
        line.ship_movement_id = movement.id
        item.location_id = None  # in transit
        line_audit.append(_line_audit_shape(line, item))

    to.status = TransferOrderStatus.SHIPPED
    to.shipped_at = shipped_at
    to.shipped_by = user.id

    record_audit(
        db,
        actor=user,
        action="transfer_order.shipped",
        entity_type="transfer_order",
        entity_id=to.id,
        before={"status": TransferOrderStatus.DRAFT.value},
        after={
            "status": to.status.value,
            "shipped_at": shipped_at,
            "shipped_by": user.id,
            "lines": line_audit,
        },
    )
    db.commit()
    _flash(request, f"Transfer order #{to.id} shipped.")
    return RedirectResponse(
        url=f"/admin/transfers/{to.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{to_id}/receive")
def receive_transfer(
    request: Request,
    to_id: int,
    reason: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_role(Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    to = _get_to_or_404(db, to_id)
    if to.status != TransferOrderStatus.SHIPPED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="only SHIPPED transfer orders can be received",
        )
    lines = _lines_for(db, to.id)
    if not lines:  # pragma: no cover - prevented at ship time
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="transfer order has no lines",
        )

    items_by_id: dict[int, Item] = {}
    for line in lines:
        item = db.get(Item, line.item_id)
        if item is None:  # pragma: no cover - FK guarantees presence
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"item {line.item_id} not found",
            )
        items_by_id[item.id] = item

    received_at = datetime.now(UTC)
    clean_reason = (reason or "").strip() or "transfer_order_receive"
    clean_note = (note or "").strip() or None
    line_audit: list[dict[str, Any]] = []
    for line in lines:
        item = items_by_id[line.item_id]
        movement = StockMovement(
            item_id=item.id,
            type=MovementType.TRANSFER,
            qty=line.qty,
            user_id=user.id,
            reason=clean_reason,
            note=clean_note,
            transfer_order_id=to.id,
        )
        db.add(movement)
        db.flush()
        line.receive_movement_id = movement.id
        item.location_id = to.destination_location_id
        line_audit.append(_line_audit_shape(line, item))

    to.status = TransferOrderStatus.RECEIVED
    to.received_at = received_at
    to.received_by = user.id

    record_audit(
        db,
        actor=user,
        action="transfer_order.received",
        entity_type="transfer_order",
        entity_id=to.id,
        before={"status": TransferOrderStatus.SHIPPED.value},
        after={
            "status": to.status.value,
            "received_at": received_at,
            "received_by": user.id,
            "lines": line_audit,
        },
    )
    db.commit()
    _flash(request, f"Transfer order #{to.id} received.")
    return RedirectResponse(
        url=f"/admin/transfers/{to.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


def open_in_transit_summary(db: Session) -> dict[str, int]:
    """Counts for the dashboard widget: how many TOs / lines are in transit.

    A TO is "in transit" while its status is SHIPPED.
    """
    count_tos = (
        db.execute(
            select(func.count())
            .select_from(TransferOrder)
            .where(TransferOrder.status == TransferOrderStatus.SHIPPED)
        ).scalar()
        or 0
    )
    count_lines = (
        db.execute(
            select(func.count())
            .select_from(TransferOrderLine)
            .join(
                TransferOrder, TransferOrder.id == TransferOrderLine.transfer_order_id
            )
            .where(TransferOrder.status == TransferOrderStatus.SHIPPED)
        ).scalar()
        or 0
    )
    return {"transfers": int(count_tos), "lines": int(count_lines)}
