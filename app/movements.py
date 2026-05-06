"""Manual stock-in route (M2).

The first slice that wires the FIFO cost engine (``app/cost_engine.py``) into a
real route. Workshop's first positive-write surface alongside Manager + Office.

Route surface (mounted at ``/admin/items``):

- ``GET  /admin/items/{item_id}/in`` — render a form (qty + unit_cost + reason
  + note) plus a recent-movements table for the item.
- ``POST /admin/items/{item_id}/in`` — validate, build a ``StockMovement(type=IN)``,
  flush, call ``record_receipt`` (which creates the cost layer + bumps
  ``current_qty`` + sets ``movement.total_cost``), write a ``stock_movement.in``
  audit row, commit, redirect 303 back with a flash.

The route never touches ``cost_layers.qty_remaining``, ``movement.total_cost``,
or ``item.current_qty`` directly — the engine is the single owner of those
columns (M1's contract).

Validation: ``qty`` must parse as a positive ``Decimal``; ``unit_cost`` must
parse as a non-negative ``Decimal`` (zero is allowed for sample / gifted
stock). Both 400 on blank / non-numeric / out-of-range. Item must exist (404)
and not be archived (400 — same posture as taxonomy/items "no new structure
under archived"). All validation 400s fire *before* any DB write, so a failed
request leaves the DB untouched.

Audit shape: ``action="stock_movement.in"``, ``entity_type="stock_movement"``,
``entity_id=movement.id``, ``before=None``, ``after`` carries the route inputs
plus the engine outputs (``total_cost``, ``source``, ``received_at``). The
movement row is the audit primary; the cost layer and any future consumption
rows are reconstructable from ``cost_layers.source_movement_id`` and
``cost_layer_consumptions.movement_id``.

Out of M2's scope (deferred): backdated ``received_at`` UI, PO-receipt path
(PO5), partial-receive against a PO line, "stock out" (M3), adjustments (M4).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.cost_engine import record_receipt
from app.db import get_session
from app.models import (
    CostLayerSource,
    Item,
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
