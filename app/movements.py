"""Manual stock-in (M2), stock-out (M3), adjustment (M4), and transfer (M5)
routes plus the read-only item detail page (M6).

The first slices that wire the FIFO cost engine (``app/cost_engine.py``) into
real routes. Workshop's first positive-write surface alongside Manager +
Office.

Route surface (mounted at ``/admin/items``):

- ``GET  /admin/items/{item_id}/in`` — render a form (qty + unit_cost + reason
  + note) plus a recent-movements table for the item.
- ``POST /admin/items/{item_id}/in`` — validate, build a ``StockMovement(type=IN)``,
  flush, call ``record_receipt`` (which creates the cost layer + bumps
  ``current_qty`` + sets ``movement.total_cost``), write a ``stock_movement.in``
  audit row, commit, redirect 303 back with a flash.
- ``GET  /admin/items/{item_id}/out`` — render a stock-out form (qty + reason +
  note; no unit_cost — consumption price is per-layer) plus the same recent-
  movements table and an "open value" summary line.
- ``POST /admin/items/{item_id}/out`` — validate, build a ``StockMovement(type=OUT)``,
  flush, call ``consume_fifo`` (which writes one ``CostLayerConsumption`` row
  per layer touched, decrements ``qty_remaining``, decrements ``current_qty``,
  sets ``movement.total_cost``), write a ``stock_movement.out`` audit row,
  commit, redirect 303 with a flash. ``InsufficientStockError`` from the engine
  re-renders the form with a 400 status and an in-form error message — the
  only data-dependent failure case in the route, hence the carve-out.
- ``GET  /admin/items/{item_id}/adjust`` — render an adjust form (qty +
  direction {increase, decrease} + unit_cost + reason + note) plus the same
  open-value + recent-movements summary.
- ``POST /admin/items/{item_id}/adjust`` — validate, build a
  ``StockMovement(type=ADJUSTMENT)``, flush, dispatch to ``record_receipt``
  (direction=increase, ``source=POSITIVE_ADJUSTMENT``) or ``consume_fifo``
  (direction=decrease — same insufficient-stock atomic-on-raise contract as
  the ``out`` route), write a ``stock_movement.adjustment`` audit row, commit,
  303 with flash. **Reason is required** (variance has to be attributed —
  MISSION §3); ``unit_cost`` is required for increases and ignored for
  decreases (consumption price comes from layers).

The route never touches ``cost_layers.qty_remaining``, ``movement.total_cost``,
or ``item.current_qty`` directly — the engine is the single owner of those
columns (M1's contract).

Validation: ``qty`` must parse as a positive ``Decimal``; ``unit_cost`` must
parse as a non-negative ``Decimal`` (zero is allowed for sample / gifted
stock). Both 400 on blank / non-numeric / out-of-range. Item must exist (404)
and not be archived (400 — same posture as taxonomy/items "no new structure
under archived"). All validation 400s fire *before* any DB write, so a failed
request leaves the DB untouched.

Audit shape — stock-in: ``action="stock_movement.in"``, ``entity_type="stock_movement"``,
``entity_id=movement.id``, ``before=None``, ``after`` carries the route inputs
plus the engine outputs (``total_cost``, ``source``, ``received_at``). The
movement row is the audit primary; the cost layer and any future consumption
rows are reconstructable from ``cost_layers.source_movement_id`` and
``cost_layer_consumptions.movement_id``.

Audit shape — stock-out: ``action="stock_movement.out"``,
``entity_type="stock_movement"``, ``entity_id=movement.id``, ``before=None``,
``after={item_id, qty, total_cost, reason, note}``. No ``unit_cost`` (varies per
layer) and no ``source`` (consumes don't carry one). The layer-level breakdown
is in ``cost_layer_consumptions`` (queryable by ``movement_id``).

Audit shape — adjustment: ``action="stock_movement.adjustment"``,
``entity_type="stock_movement"``, ``entity_id=movement.id``, ``before=None``,
``after={item_id, qty, direction, total_cost, reason, note, ...}``. ``qty``
is always the positive Decimal the user submitted; ``direction`` carries the
sign. Increase additionally records ``unit_cost``, ``source`` (=
"positive_adjustment"), and ``received_at``; decrease leaves those keys absent
(same gap as stock-out).

Audit shape — transfer (M5): ``action="stock_movement.transfer"``,
``entity_type="stock_movement"``, ``entity_id=movement.id``,
``before={location_id}`` (the item's prior location), ``after={item_id, qty,
from_location_id, to_location_id, reason, note}``. Transfer is the **one**
movement type that bypasses the cost engine — no cost layer is created or
consumed, ``movement.total_cost`` stays ``None``, and ``item.current_qty`` is
unchanged. The route mutates ``item.location_id`` directly (the only Item
column it touches besides the movement insert).

Out of scope (deferred): backdated ``created_at`` / ``received_at`` UI,
PO-receipt path (PO5), partial-receive against a PO line, per-unit transfer
on unique-tracked items (transfers today flip the item's location wholesale).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.cost_engine import (
    InsufficientStockError,
    consume_fifo,
    open_value,
    record_receipt,
)
from app.db import get_session
from app.models import (
    CostLayer,
    CostLayerConsumption,
    CostLayerSource,
    Item,
    Location,
    MovementType,
    Role,
    StockMovement,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/items", tags=["movements"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _parse_non_negative_decimal(raw: str, *, field_name: str) -> Decimal:
    """Parse a non-negative decimal. Blank and negative reject; zero allowed."""
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


def _parse_required_reason(raw: str) -> str:
    """Return a non-empty stripped reason, or 400.

    Distinct from ``in`` and ``out`` where reason is optional: adjustments
    record stock-take corrections (variance), and MISSION §3 requires every
    adjustment movement to carry a reason so the variance is attributed.
    """
    text = (raw or "").strip()
    if text == "":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="reason is required",
        )
    return text


_VALID_DIRECTIONS = ("increase", "decrease")


def _parse_direction(raw: str) -> str:
    """Return ``"increase"`` or ``"decrease"`` — anything else 400s."""
    text = (raw or "").strip()
    if text not in _VALID_DIRECTIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="direction must be 'increase' or 'decrease'",
        )
    return text


def _get_item_or_404(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )
    return item


def _reject_archived(item: Item) -> None:
    """Archived items don't accept new stock (cleanup-only posture)."""
    if item.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="item is archived",
        )


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


# ---------------------------------------------------------------------------
# Recent movements list (for the form + future timeline)
# ---------------------------------------------------------------------------


_RECENT_LIMIT = 10


def _recent_movements(db: Session, item_id: int) -> list[dict[str, Any]]:
    """Latest movements for an item, newest first.

    Returns view-shaped dicts so the template doesn't have to know about ORM
    relationships. Each row carries the actor's email when available; ``None``
    if the actor was deleted (``user_id`` was set to NULL by the FK
    ``ON DELETE SET NULL`` cascade).
    """
    stmt = (
        select(StockMovement, User.email)
        .outerjoin(User, StockMovement.user_id == User.id)
        .where(StockMovement.item_id == item_id)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(_RECENT_LIMIT)
    )
    out: list[dict[str, Any]] = []
    for movement, actor_email in db.execute(stmt).all():
        out.append(
            {
                "id": movement.id,
                "type": movement.type.value,
                "qty": movement.qty,
                "total_cost": movement.total_cost,
                "reason": movement.reason,
                "actor_email": actor_email,
                "created_at": movement.created_at,
            }
        )
    return out


# ---------------------------------------------------------------------------
# GET /admin/items/{item_id}/in — form
# ---------------------------------------------------------------------------


@router.get("/{item_id}/in", response_class=HTMLResponse)
def stock_in_form(
    request: Request,
    item_id: int,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)
    return templates.TemplateResponse(
        request,
        "stock_in_form.html",
        {
            "current_user": user,
            "item": item,
            "recent_movements": _recent_movements(db, item.id),
            "form": {
                "qty": "",
                "unit_cost": "",
                "reason": "",
                "note": "",
            },
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/items/{item_id}/in — record a receipt
# ---------------------------------------------------------------------------


@router.post("/{item_id}/in")
def record_stock_in(
    request: Request,
    item_id: int,
    qty: str = Form(""),
    unit_cost: str = Form(""),
    reason: str = Form(""),
    note: str = Form(""),
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> Response:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)

    qty_decimal = _parse_positive_decimal(qty, field_name="quantity")
    unit_cost_decimal = _parse_non_negative_decimal(
        unit_cost, field_name="unit cost"
    )
    clean_reason = (reason or "").strip() or None
    clean_note = (note or "").strip() or None

    # Build the movement, flush so the engine can FK off its id, then let the
    # engine create the cost layer + bump current_qty + set total_cost.
    received_at = datetime.now(UTC)
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.IN,
        qty=qty_decimal,
        user_id=user.id,
        reason=clean_reason,
        note=clean_note,
    )
    db.add(movement)
    db.flush()

    record_receipt(
        db,
        item=item,
        qty=qty_decimal,
        unit_cost=unit_cost_decimal,
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
            "qty": str(qty_decimal),
            "unit_cost": str(unit_cost_decimal),
            "total_cost": str(movement.total_cost) if movement.total_cost is not None else None,
            "source": CostLayerSource.MANUAL_IN.value,
            "reason": clean_reason,
            "note": clean_note,
            "received_at": received_at.isoformat(),
        },
    )
    db.commit()

    _flash(
        request,
        f"Stock-in recorded: +{qty_decimal} {item.unit} of “{item.name}”.",
    )
    return RedirectResponse(
        url=f"/admin/items/{item.id}/in",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# GET /admin/items/{item_id}/out — form
# ---------------------------------------------------------------------------


def _render_stock_out_form(
    request: Request,
    *,
    user: User,
    item: Item,
    db: Session,
    form_values: dict[str, str],
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "stock_out_form.html",
        {
            "current_user": user,
            "item": item,
            "open_value": open_value(db, item),
            "recent_movements": _recent_movements(db, item.id),
            "form": form_values,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/{item_id}/out", response_class=HTMLResponse)
def stock_out_form(
    request: Request,
    item_id: int,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)
    return _render_stock_out_form(
        request,
        user=user,
        item=item,
        db=db,
        form_values={"qty": "", "reason": "", "note": ""},
    )


# ---------------------------------------------------------------------------
# POST /admin/items/{item_id}/out — record a consumption
# ---------------------------------------------------------------------------


@router.post("/{item_id}/out")
def record_stock_out(
    request: Request,
    item_id: int,
    qty: str = Form(""),
    reason: str = Form(""),
    note: str = Form(""),
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> Response:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)

    qty_decimal = _parse_positive_decimal(qty, field_name="quantity")
    clean_reason = (reason or "").strip() or None
    clean_note = (note or "").strip() or None

    movement = StockMovement(
        item_id=item.id,
        type=MovementType.OUT,
        qty=qty_decimal,
        user_id=user.id,
        reason=clean_reason,
        note=clean_note,
    )
    db.add(movement)
    db.flush()

    try:
        consume_fifo(db, item=item, qty=qty_decimal, movement=movement)
    except InsufficientStockError as exc:
        # Atomic by engine contract: no rows mutated when this raises. Roll
        # back to drop the (unflushed-to-commit) movement we added above and
        # re-render the form with the user's inputs preserved + an error.
        db.rollback()
        # ``item`` is detached after rollback; reload for the re-render so the
        # current_qty / open_value reflect what the user actually has.
        item = _get_item_or_404(db, item_id)
        return _render_stock_out_form(
            request,
            user=user,
            item=item,
            db=db,
            form_values={
                "qty": qty.strip(),
                "reason": (reason or "").strip(),
                "note": (note or "").strip(),
            },
            error=(
                f"Not enough stock: requested {exc.requested}, "
                f"only {exc.available} available."
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    record_audit(
        db,
        actor=user,
        action="stock_movement.out",
        entity_type="stock_movement",
        entity_id=movement.id,
        before=None,
        after={
            "item_id": item.id,
            "qty": str(qty_decimal),
            "total_cost": (
                str(movement.total_cost)
                if movement.total_cost is not None
                else None
            ),
            "reason": clean_reason,
            "note": clean_note,
        },
    )
    db.commit()

    _flash(
        request,
        f"Stock-out recorded: -{qty_decimal} {item.unit} of “{item.name}”.",
    )
    return RedirectResponse(
        url=f"/admin/items/{item.id}/out",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# GET /admin/items/{item_id}/adjust — form
# ---------------------------------------------------------------------------


def _render_stock_adjust_form(
    request: Request,
    *,
    user: User,
    item: Item,
    db: Session,
    form_values: dict[str, str],
    error: str | None = None,
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "stock_adjust_form.html",
        {
            "current_user": user,
            "item": item,
            "open_value": open_value(db, item),
            "recent_movements": _recent_movements(db, item.id),
            "form": form_values,
            "directions": _VALID_DIRECTIONS,
            "error": error,
        },
        status_code=status_code,
    )


@router.get("/{item_id}/adjust", response_class=HTMLResponse)
def stock_adjust_form(
    request: Request,
    item_id: int,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)
    return _render_stock_adjust_form(
        request,
        user=user,
        item=item,
        db=db,
        form_values={
            "qty": "",
            "direction": "",
            "unit_cost": "",
            "reason": "",
            "note": "",
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/items/{item_id}/adjust — record an adjustment
# ---------------------------------------------------------------------------


@router.post("/{item_id}/adjust")
def record_stock_adjustment(
    request: Request,
    item_id: int,
    qty: str = Form(""),
    direction: str = Form(""),
    unit_cost: str = Form(""),
    reason: str = Form(""),
    note: str = Form(""),
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> Response:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)

    qty_decimal = _parse_positive_decimal(qty, field_name="quantity")
    direction_value = _parse_direction(direction)
    clean_reason = _parse_required_reason(reason)
    clean_note = (note or "").strip() or None

    # unit_cost is only required + parsed for increases. For decreases it is
    # ignored (consumption price is per-layer; same as the ``out`` route).
    unit_cost_decimal: Decimal | None = None
    if direction_value == "increase":
        unit_cost_decimal = _parse_non_negative_decimal(
            unit_cost, field_name="unit cost"
        )

    received_at = datetime.now(UTC)
    movement = StockMovement(
        item_id=item.id,
        type=MovementType.ADJUSTMENT,
        qty=qty_decimal,
        user_id=user.id,
        reason=clean_reason,
        note=clean_note,
    )
    db.add(movement)
    db.flush()

    if direction_value == "increase":
        # mypy: unit_cost_decimal is non-None on this branch by construction.
        assert unit_cost_decimal is not None
        record_receipt(
            db,
            item=item,
            qty=qty_decimal,
            unit_cost=unit_cost_decimal,
            source=CostLayerSource.POSITIVE_ADJUSTMENT,
            movement=movement,
            received_at=received_at,
        )
        audit_after: dict[str, Any] = {
            "item_id": item.id,
            "qty": str(qty_decimal),
            "direction": direction_value,
            "unit_cost": str(unit_cost_decimal),
            "total_cost": (
                str(movement.total_cost)
                if movement.total_cost is not None
                else None
            ),
            "source": CostLayerSource.POSITIVE_ADJUSTMENT.value,
            "reason": clean_reason,
            "note": clean_note,
            "received_at": received_at.isoformat(),
        }
        flash_message = (
            f"Adjustment recorded: +{qty_decimal} {item.unit} of "
            f"“{item.name}”."
        )
    else:
        try:
            consume_fifo(db, item=item, qty=qty_decimal, movement=movement)
        except InsufficientStockError as exc:
            db.rollback()
            item = _get_item_or_404(db, item_id)
            return _render_stock_adjust_form(
                request,
                user=user,
                item=item,
                db=db,
                form_values={
                    "qty": qty.strip(),
                    "direction": direction_value,
                    "unit_cost": (unit_cost or "").strip(),
                    "reason": clean_reason,
                    "note": (note or "").strip(),
                },
                error=(
                    f"Not enough stock: requested {exc.requested}, "
                    f"only {exc.available} available."
                ),
                status_code=status.HTTP_400_BAD_REQUEST,
            )
        audit_after = {
            "item_id": item.id,
            "qty": str(qty_decimal),
            "direction": direction_value,
            "total_cost": (
                str(movement.total_cost)
                if movement.total_cost is not None
                else None
            ),
            "reason": clean_reason,
            "note": clean_note,
        }
        flash_message = (
            f"Adjustment recorded: -{qty_decimal} {item.unit} of "
            f"“{item.name}”."
        )

    record_audit(
        db,
        actor=user,
        action="stock_movement.adjustment",
        entity_type="stock_movement",
        entity_id=movement.id,
        before=None,
        after=audit_after,
    )
    db.commit()

    _flash(request, flash_message)
    return RedirectResponse(
        url=f"/admin/items/{item.id}/adjust",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Transfer between locations (M5)
# ---------------------------------------------------------------------------
#
# The single movement type that bypasses the cost engine. Transfers change an
# item's location pointer; they don't change quantity on hand and don't change
# valuation. The route writes a ``StockMovement(type=TRANSFER, total_cost=None)``
# for audit, flips ``item.location_id``, and records the from/to in the audit
# row's before/after.
#
# Validation guards:
# - Item exists (404), not archived (400), has a current location_id (400 —
#   "set a location via the edit form first"; transfer presupposes a "from").
# - ``to_location_id`` must reference an active ``Location`` and must differ
#   from the item's current ``location_id`` (a no-op transfer would write a
#   confusing audit row).
# - ``qty`` must parse as a positive Decimal — same as in / out / adjust. We
#   don't validate qty against ``current_qty``: for v1 the location pointer
#   flips wholesale, and ``qty`` on the movement row is informational only.


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


def _resolve_active_location(db: Session, location_id: int) -> Location:
    """Look up an active location or 400. Archived rows reject."""
    loc = db.get(Location, location_id)
    if loc is None or loc.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target location is not active",
        )
    return loc


def _other_active_locations(
    db: Session, *, exclude_id: int
) -> list[dict[str, Any]]:
    """Active locations other than ``exclude_id`` (the item's current).

    Returned as view-shaped dicts so the template can iterate without ORM
    knowledge. Ordered by name for predictable rendering and tests.
    """
    rows = list(
        db.execute(
            select(Location)
            .where(Location.archived_at.is_(None))
            .where(Location.id != exclude_id)
            .order_by(Location.name)
        )
        .scalars()
        .all()
    )
    return [{"id": loc.id, "name": loc.name} for loc in rows]


def _render_stock_transfer_form(
    request: Request,
    *,
    user: User,
    item: Item,
    db: Session,
    form_values: dict[str, str],
    status_code: int = status.HTTP_200_OK,
) -> HTMLResponse:
    # Item must have a from-location for transfer to make sense.
    if item.location_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this item has no location yet — set one via the edit form "
                "before transferring"
            ),
        )
    from_location = db.get(Location, item.location_id)
    return templates.TemplateResponse(
        request,
        "stock_transfer_form.html",
        {
            "current_user": user,
            "item": item,
            "from_location": from_location,
            "to_location_options": _other_active_locations(
                db, exclude_id=item.location_id
            ),
            "recent_movements": _recent_movements(db, item.id),
            "form": form_values,
        },
        status_code=status_code,
    )


@router.get("/{item_id}/transfer", response_class=HTMLResponse)
def stock_transfer_form(
    request: Request,
    item_id: int,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)
    return _render_stock_transfer_form(
        request,
        user=user,
        item=item,
        db=db,
        form_values={
            "to_location_id": "",
            "qty": str(item.current_qty),
            "reason": "",
            "note": "",
        },
    )


@router.post("/{item_id}/transfer")
def record_stock_transfer(
    request: Request,
    item_id: int,
    to_location_id: str = Form(""),
    qty: str = Form(""),
    reason: str = Form(""),
    note: str = Form(""),
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> Response:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)

    # The from-location must exist for transfer to make sense; same 400 the
    # GET handler raises so the user has a single recoverable error message.
    from_location_id = item.location_id
    if from_location_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this item has no location yet — set one via the edit form "
                "before transferring"
            ),
        )

    target_id = _parse_int_id(to_location_id, field_name="target location")
    if target_id == from_location_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="target location must differ from the current location",
        )
    target = _resolve_active_location(db, target_id)
    qty_decimal = _parse_positive_decimal(qty, field_name="quantity")
    clean_reason = (reason or "").strip() or None
    clean_note = (note or "").strip() or None
    from_location = db.get(Location, from_location_id)

    movement = StockMovement(
        item_id=item.id,
        type=MovementType.TRANSFER,
        qty=qty_decimal,
        user_id=user.id,
        reason=clean_reason,
        note=clean_note,
    )
    db.add(movement)
    db.flush()
    item.location_id = target.id

    record_audit(
        db,
        actor=user,
        action="stock_movement.transfer",
        entity_type="stock_movement",
        entity_id=movement.id,
        before={"location_id": from_location_id},
        after={
            "item_id": item.id,
            "qty": str(qty_decimal),
            "from_location_id": from_location_id,
            "to_location_id": target.id,
            "reason": clean_reason,
            "note": clean_note,
        },
    )
    db.commit()

    from_label = from_location.name if from_location is not None else "?"
    _flash(
        request,
        f"Transfer recorded: {qty_decimal} {item.unit} of "
        f"“{item.name}” from {from_label} to {target.name}.",
    )
    return RedirectResponse(
        url=f"/admin/items/{item.id}/transfer",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# GET /admin/items/{item_id}/detail — read-only item detail (M6)
# ---------------------------------------------------------------------------
#
# Single page that consolidates the per-form recent-movements lists from M2 /
# M3 / M4 into one place: open cost layers + paginated full timeline + per-row
# layer breakdown for OUT / negative-adjust rows. Read-only — no mutations, no
# audit. Mounted on the same router because the bulk of the page is movement +
# layer data, not item-edit data.
#
# Role surface: Manager + Office + Workshop (mirroring the in / out / adjust
# routes). Workshop's first non-form per-item read surface; deep-link only
# until I1c gives them a list.

_PAGE_SIZE = 20


def _open_layers(db: Session, item_id: int) -> list[dict[str, Any]]:
    """Open FIFO cost layers for an item (qty_remaining > 0), oldest first.

    Layers are not deleted when drained — they stay as audit history with
    qty_remaining=0. The detail page's "open layers" section only shows the
    rows still contributing to current_qty / open_value.
    """
    stmt = (
        select(CostLayer)
        .where(CostLayer.item_id == item_id)
        .where(CostLayer.qty_remaining > 0)
        .order_by(CostLayer.received_at.asc(), CostLayer.id.asc())
    )
    out: list[dict[str, Any]] = []
    for layer in db.execute(stmt).scalars().all():
        out.append(
            {
                "id": layer.id,
                "received_at": layer.received_at,
                "qty_received": layer.qty_received,
                "qty_remaining": layer.qty_remaining,
                "unit_cost": layer.unit_cost,
                "source": layer.source.value,
                "source_movement_id": layer.source_movement_id,
            }
        )
    return out


def _count_movements(db: Session, item_id: int) -> int:
    total = db.scalar(
        select(func.count(StockMovement.id)).where(
            StockMovement.item_id == item_id
        )
    )
    return int(total or 0)


def _movements_page(
    db: Session, item_id: int, *, page: int, page_size: int
) -> list[dict[str, Any]]:
    """One page of movements for an item, newest first.

    The view-shaped dicts deliberately mirror :func:`_recent_movements` so the
    template's ``movement-row`` rendering can be reused for the timeline. The
    extra fields the timeline needs (``direction``, ``breakdown``) are merged
    in by :func:`_attach_directions_and_breakdown` after this returns.
    """
    offset = (page - 1) * page_size
    stmt = (
        select(StockMovement, User.email)
        .outerjoin(User, StockMovement.user_id == User.id)
        .where(StockMovement.item_id == item_id)
        .order_by(StockMovement.created_at.desc(), StockMovement.id.desc())
        .limit(page_size)
        .offset(offset)
    )
    out: list[dict[str, Any]] = []
    for movement, actor_email in db.execute(stmt).all():
        out.append(
            {
                "id": movement.id,
                "type": movement.type.value,
                "qty": movement.qty,
                "total_cost": movement.total_cost,
                "reason": movement.reason,
                "note": movement.note,
                "actor_email": actor_email,
                "created_at": movement.created_at,
            }
        )
    return out


def _attach_directions_and_breakdown(
    db: Session, movements: list[dict[str, Any]]
) -> None:
    """Merge ``direction`` ("+"/"-") and ``breakdown`` (list) into each row.

    Direction is derived from whether the movement created a cost layer
    (a row in ``cost_layers`` with ``source_movement_id == movement.id``) or
    consumed layers (rows in ``cost_layer_consumptions`` with
    ``movement_id == movement.id``). For IN / positive-adjust the layer-side
    join lights up; for OUT / negative-adjust the consumption-side join
    lights up. Both queries are batched over the page's movement ids.
    """
    if not movements:
        return
    ids = [m["id"] for m in movements]

    layer_movement_ids: set[int] = set(
        db.execute(
            select(CostLayer.source_movement_id)
            .where(CostLayer.source_movement_id.in_(ids))
            .distinct()
        )
        .scalars()
        .all()
    )

    breakdown: dict[int, list[dict[str, Any]]] = {mid: [] for mid in ids}
    rows = db.execute(
        select(
            CostLayerConsumption.movement_id,
            CostLayerConsumption.qty_consumed,
            CostLayerConsumption.unit_cost_at_consumption,
            CostLayer.received_at,
            CostLayer.id,
        )
        .join(CostLayer, CostLayerConsumption.layer_id == CostLayer.id)
        .where(CostLayerConsumption.movement_id.in_(ids))
        .order_by(
            CostLayerConsumption.movement_id,
            CostLayer.received_at.asc(),
            CostLayer.id.asc(),
        )
    ).all()
    for movement_id, qty_consumed, unit_cost, received_at, layer_id in rows:
        breakdown[movement_id].append(
            {
                "qty_consumed": qty_consumed,
                "unit_cost_at_consumption": unit_cost,
                "layer_received_at": received_at,
                "layer_id": layer_id,
            }
        )

    for m in movements:
        m_id = m["id"]
        m["breakdown"] = breakdown[m_id]
        if m_id in layer_movement_ids:
            m["direction"] = "+"
        elif breakdown[m_id]:
            m["direction"] = "-"
        else:
            # Defensive: a movement with no layer + no consumption is a row
            # that never went through the cost engine. Today no route writes
            # such a row (M5 / TRANSFER will be the first), but the timeline
            # should still render something rather than crash.
            m["direction"] = ""


def _paginate(total: int, page: int, page_size: int) -> dict[str, Any]:
    """Compute pagination metadata. Out-of-range pages clamp to [1, page_count].

    Clamping (vs 400-on-out-of-range) is the friendlier UX for stale links: a
    user navigates to ``?page=5`` from a bookmark, more movements have come in,
    and the page count has dropped — they land on the last page rather than an
    error. Empty-list (``total=0``) collapses to ``page=1, page_count=1``.
    """
    if page_size <= 0:
        page_size = _PAGE_SIZE
    page_count = max(1, (total + page_size - 1) // page_size)
    page = max(1, min(page, page_count))
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "page_count": page_count,
        "has_prev": page > 1,
        "has_next": page < page_count,
    }


def _can_edit_item(user: User) -> bool:
    """Manager + Office can edit (matches I1b's items-edit role surface)."""
    return user.role in (Role.MANAGER, Role.OFFICE, Role.ADMIN)


@router.get("/{item_id}/detail", response_class=HTMLResponse)
def item_detail(
    request: Request,
    item_id: int,
    page: int = 1,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Read-only item detail page (M6).

    Surfaces the cost-engine outputs in one place: open layers + paginated
    movement timeline (with per-row layer breakdown for OUT / negative-adjust).
    Archived items still render — detail is a read of history, including for
    archived items; the action links to in / out / adjust hide on archived
    rows (mirrors ``items_form.html``).
    """
    item = _get_item_or_404(db, item_id)
    layers = _open_layers(db, item.id)
    total_movements = _count_movements(db, item.id)
    pagination = _paginate(total_movements, page, _PAGE_SIZE)
    movements = _movements_page(
        db,
        item.id,
        page=pagination["page"],
        page_size=pagination["page_size"],
    )
    _attach_directions_and_breakdown(db, movements)

    return templates.TemplateResponse(
        request,
        "item_detail.html",
        {
            "current_user": user,
            "item": item,
            "layers": layers,
            "open_value": open_value(db, item),
            "movements": movements,
            "pagination": pagination,
            "can_edit_item": _can_edit_item(user),
        },
    )
