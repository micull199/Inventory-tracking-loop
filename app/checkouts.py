"""Tool / mould check-out flow (C2 — first half of DoD #4).

Two routes mounted at ``/admin/items``:

- ``GET  /admin/items/{item_id}/checkout`` — render a check-out form. For a
  unique-tracked item the form includes a ``<select>`` of *available* units
  (active + status=available + not-currently-in-an-open-checkout); for a qty-
  tracked item there's no unit select. The page also surfaces a status block
  listing any open checkouts so the operator can see "this is already out"
  before they POST.
- ``POST /admin/items/{item_id}/checkout`` — validate, create a ``Checkout``
  row, write a ``checkout.created`` audit row, commit, 303 back with a flash.

Engine isolation: a checkout is custody, not consumption. The route does NOT
call the cost engine, does NOT create a ``StockMovement``, and does NOT change
``item.current_qty`` — a tool that's checked out still belongs to UC. C3 will
add the symmetric check-in route; C4 the manager "currently out / overdue"
view.

Validation (all 400, before any DB write so a failure is atomic):
- Item exists (404), not archived (400), ``requires_checkout=True`` (400).
- ``expected_return``: blank → ``None``; ISO ``YYYY-MM-DD`` else 400.
- ``condition_note``: stripped, blank → ``None``, ≤ 2000 chars else 400.
- Unique-tracked: ``item_unit_id`` required (blank rejects), parses as int
  (non-numeric rejects), references an active ``ItemUnit`` *on this item*
  (mismatched-item / archived / status=lost all reject), and is not currently
  in an open checkout (returned_at IS NULL — rejects).
- Qty-tracked: ``item_unit_id`` is silently ignored on the input side and the
  row is written with ``item_unit_id=None``. At-most-one-open-per-item is
  enforced by a query against the open-checkout set.

Audit shape: ``action="checkout.created"``, ``entity_type="checkout"``,
``entity_id=checkout.id``, ``before=None``, ``after={item_id, item_unit_id,
user_id, checked_out_at (ISO), expected_return (ISO | None), condition_note}``.
The ``expected_return`` is upgraded to a UTC-midnight ``datetime`` so the
column accepts it; serialised back to ISO for the audit row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import (
    Checkout,
    Item,
    ItemUnit,
    ItemUnitStatus,
    Role,
    TrackingMode,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/items", tags=["checkouts"])

_CONDITION_NOTE_MAX = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_item_or_404(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )
    return item


def _reject_archived(item: Item) -> None:
    if item.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="item is archived",
        )


def _reject_non_flagged(item: Item) -> None:
    if not item.requires_checkout:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this item is not flagged for check-out",
        )


def _parse_optional_date(raw: str, *, field_name: str) -> date | None:
    """Parse ``YYYY-MM-DD`` or ``None`` from blank. Else 400."""
    text = (raw or "").strip()
    if text == "":
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be YYYY-MM-DD",
        ) from exc


def _parse_optional_note(raw: str) -> str | None:
    text = (raw or "").strip()
    if text == "":
        return None
    if len(text) > _CONDITION_NOTE_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"condition_note must be {_CONDITION_NOTE_MAX} characters or fewer"
            ),
        )
    return text


def _open_checkout_unit_ids(db: Session, item_id: int) -> set[int]:
    """Item-unit ids currently in an open checkout for ``item_id``."""
    rows = db.execute(
        select(Checkout.item_unit_id)
        .where(Checkout.item_id == item_id)
        .where(Checkout.returned_at.is_(None))
        .where(Checkout.item_unit_id.is_not(None))
    ).scalars().all()
    return {int(uid) for uid in rows if uid is not None}


def _has_open_qty_checkout(db: Session, item_id: int) -> bool:
    """Is there an open checkout for the qty-tracked-item-as-a-whole?"""
    row = db.execute(
        select(Checkout.id)
        .where(Checkout.item_id == item_id)
        .where(Checkout.item_unit_id.is_(None))
        .where(Checkout.returned_at.is_(None))
        .limit(1)
    ).first()
    return row is not None


def _available_units(db: Session, item_id: int) -> list[ItemUnit]:
    """Active item-units eligible to be checked out (not lost, not open)."""
    open_ids = _open_checkout_unit_ids(db, item_id)
    stmt = (
        select(ItemUnit)
        .where(ItemUnit.item_id == item_id)
        .where(ItemUnit.archived_at.is_(None))
        .where(ItemUnit.status == ItemUnitStatus.AVAILABLE)
        .order_by(ItemUnit.serial_or_label)
    )
    rows = list(db.execute(stmt).scalars().all())
    return [u for u in rows if u.id not in open_ids]


def _open_checkouts(db: Session, item_id: int) -> list[dict[str, Any]]:
    """View-shaped open checkouts for the item, oldest first."""
    stmt = (
        select(Checkout, User.email, ItemUnit.serial_or_label)
        .outerjoin(User, Checkout.user_id == User.id)
        .outerjoin(ItemUnit, Checkout.item_unit_id == ItemUnit.id)
        .where(Checkout.item_id == item_id)
        .where(Checkout.returned_at.is_(None))
        .order_by(Checkout.checked_out_at.asc(), Checkout.id.asc())
    )
    out: list[dict[str, Any]] = []
    for co, actor_email, unit_label in db.execute(stmt).all():
        out.append(
            {
                "id": co.id,
                "item_unit_id": co.item_unit_id,
                "unit_label": unit_label,
                "actor_email": actor_email,
                "checked_out_at": co.checked_out_at,
                "expected_return": co.expected_return,
            }
        )
    return out


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


# ---------------------------------------------------------------------------
# GET /admin/items/{item_id}/checkout — form
# ---------------------------------------------------------------------------


@router.get("/{item_id}/checkout", response_class=HTMLResponse)
def checkout_form(
    request: Request,
    item_id: int,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)
    _reject_non_flagged(item)

    is_unique = item.tracking_mode is TrackingMode.UNIQUE
    units = _available_units(db, item.id) if is_unique else []
    open_rows = _open_checkouts(db, item.id)
    # For qty-tracked items, the submit only makes sense when no open checkout
    # exists. For unique-tracked items, the form is still useful as long as at
    # least one unit is available.
    can_submit = (
        bool(units) if is_unique else not _has_open_qty_checkout(db, item.id)
    )

    return templates.TemplateResponse(
        request,
        "checkout_form.html",
        {
            "current_user": user,
            "item": item,
            "is_unique": is_unique,
            "available_units": units,
            "open_checkouts": open_rows,
            "can_submit": can_submit,
            "form": {
                "item_unit_id": "",
                "expected_return": "",
                "condition_note": "",
            },
        },
    )


# ---------------------------------------------------------------------------
# POST /admin/items/{item_id}/checkout — record a checkout
# ---------------------------------------------------------------------------


@router.post("/{item_id}/checkout")
def record_checkout(
    request: Request,
    item_id: int,
    item_unit_id: str = Form(""),
    expected_return: str = Form(""),
    condition_note: str = Form(""),
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> Response:
    item = _get_item_or_404(db, item_id)
    _reject_archived(item)
    _reject_non_flagged(item)

    expected_date = _parse_optional_date(
        expected_return, field_name="expected_return"
    )
    clean_note = _parse_optional_note(condition_note)

    unit: ItemUnit | None = None
    if item.tracking_mode is TrackingMode.UNIQUE:
        text = (item_unit_id or "").strip()
        if text == "":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="item_unit_id is required for unique-tracked items",
            )
        try:
            uid = int(text)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="item_unit_id must be a number",
            ) from exc
        unit = db.get(ItemUnit, uid)
        if unit is None or unit.item_id != item.id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unit not found on this item",
            )
        if unit.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unit is archived",
            )
        if unit.status is not ItemUnitStatus.AVAILABLE:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unit is not available for check-out",
            )
        if unit.id in _open_checkout_unit_ids(db, item.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="unit is already checked out",
            )
    else:
        # qty-tracked: ignore any submitted item_unit_id; enforce at-most-one
        # open checkout for the item-as-a-whole.
        if _has_open_qty_checkout(db, item.id):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="this item is already checked out",
            )

    now = datetime.now(UTC)
    expected_dt = (
        datetime.combine(expected_date, datetime.min.time(), tzinfo=UTC)
        if expected_date is not None
        else None
    )

    co = Checkout(
        item_id=item.id,
        item_unit_id=unit.id if unit is not None else None,
        user_id=user.id,
        checked_out_at=now,
        expected_return=expected_dt,
        condition_note=clean_note,
    )
    db.add(co)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="checkout.created",
        entity_type="checkout",
        entity_id=co.id,
        before=None,
        after={
            "item_id": item.id,
            "item_unit_id": unit.id if unit is not None else None,
            "user_id": user.id,
            "checked_out_at": now.isoformat(),
            "expected_return": (
                expected_dt.isoformat() if expected_dt is not None else None
            ),
            "condition_note": clean_note,
        },
    )
    db.commit()

    label = (
        f"{item.name} (unit {unit.serial_or_label})"
        if unit is not None
        else item.name
    )
    _flash(request, f"Checked out: {label}.")
    return RedirectResponse(
        url=f"/admin/items/{item.id}/checkout",
        status_code=status.HTTP_303_SEE_OTHER,
    )
