"""Per-unit rows for unique-tracked items (I3).

MISSION §3: "Some items are tracked uniquely (one record per physical item,
e.g. a specific tool or mould). Others are tracked by quantity on hand". The
``item_units`` table holds those per-unit rows: serial / label, status,
optional location, soft-delete. Routes are scoped under the item so the URL
makes the parent relationship obvious.

Access mirrors ``app/items.py``:
- **Manager / Admin**: full access — list, create, edit, archive, unarchive.
- **Office**: list + edit only. No create / archive (consistent with their
  Item-level access).
- **Workshop**: 403 across the board for now (read-only access deferred to
  the same I1c slice that opens up the items list).

URL shape:
- ``GET  /admin/items/{item_id}/units?[show=…]``        — list (parent-scoped).
- ``GET  /admin/items/{item_id}/units/new``             — form (Manager-only).
- ``POST /admin/items/{item_id}/units``                  — create.
- ``GET  /admin/items/units/{unit_id}/edit``             — flat-by-id form.
- ``POST /admin/items/units/{unit_id}``                  — update.
- ``POST /admin/items/units/{unit_id}/{archive,unarchive}`` — soft delete.

Why parent-scoped for list/create but flat-by-id for everything else: same
trade-off as taxonomy sub-cats (S4). The list is naturally per-parent (we
filter on item_id); edit/archive don't need the parent in the URL and a
mismatched URL parent would just confuse callers. The unit row's ``item_id``
is the source of truth.

``current_qty`` on the parent item is *not* updated by these routes. For
unique-tracked items the count is conceptually ``COUNT(units WHERE active +
available)`` but plumbing that today bakes in a contract that the M1+ stock
movements engine will want to own. Acceptable wart: ``current_qty`` shows 0
for unique items until M1 lands. Documented in self-critique.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    ItemUnit,
    ItemUnitStatus,
    Location,
    Role,
    TrackingMode,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/items", tags=["item_units"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Audit-diff vocabulary for ``item_unit.updated``. Order is the order
# ``after_json`` entries are written when this list is iterated; keep stable
# so audit history is greppable.
_FIELDS: tuple[str, ...] = (
    "serial_or_label",
    "status",
    "location_id",
)


def _coerce_status(raw: str) -> ItemUnitStatus:
    text = (raw or "").strip()
    if text == "":
        return ItemUnitStatus.AVAILABLE
    try:
        return ItemUnitStatus(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown unit status",
        ) from exc


def _resolve_optional_location(
    db: Session, raw: str, *, current_id: int | None = None
) -> int | None:
    """Same archived-FK-preservation contract as ``app.items._resolve_optional_location``.

    Duplicated rather than imported to keep the cross-module dependency graph
    one-way (``item_units`` → ``items`` would create a cycle once items grows
    a units-aware helper). One-line predicate, two callers, both must move in
    lockstep if the contract changes.
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
    serial_or_label: str,
    status_value: str,
    location_id: str,
    current_location_id: int | None = None,
) -> dict[str, Any]:
    clean_serial = (serial_or_label or "").strip()
    if not clean_serial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="serial / label is required",
        )
    new_status = _coerce_status(status_value)
    loc_id = _resolve_optional_location(
        db, location_id, current_id=current_location_id
    )
    return {
        "serial_or_label": clean_serial,
        "status": new_status,
        "location_id": loc_id,
    }


def _check_serial_unique(
    db: Session,
    *,
    item_id: int,
    serial_or_label: str,
    exclude_id: int | None = None,
) -> None:
    stmt = (
        select(ItemUnit.id)
        .where(ItemUnit.item_id == item_id)
        .where(ItemUnit.serial_or_label == serial_or_label)
    )
    if exclude_id is not None:
        stmt = stmt.where(ItemUnit.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a unit with that serial / label already exists on this item",
        )


def _get_item(db: Session, item_id: int) -> Item:
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="item not found"
        )
    return item


def _require_unique_tracking(item: Item) -> None:
    if item.tracking_mode is not TrackingMode.UNIQUE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "this item is qty-tracked; switch its tracking mode to "
                "'unique' to manage individual units"
            ),
        )


def _get_unit(db: Session, unit_id: int) -> ItemUnit:
    unit = db.get(ItemUnit, unit_id)
    if unit is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="unit not found"
        )
    return unit


def _diff(
    unit: ItemUnit, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(unit, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _location_options(
    db: Session, *, current_id: int | None = None
) -> list[dict[str, Any]]:
    """Active locations + the assigned archived row (with "(archived)" suffix) if any.

    Identical contract to ``app.items._location_options`` — see the comment
    on ``_resolve_optional_location`` above for the duplication rationale.
    """
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


def _form_for_unit(unit: ItemUnit | None) -> dict[str, Any]:
    if unit is None:
        return {
            "serial_or_label": "",
            "status": ItemUnitStatus.AVAILABLE.value,
            "location_id": "",
        }
    return {
        "serial_or_label": unit.serial_or_label,
        "status": unit.status.value,
        "location_id": (
            str(unit.location_id) if unit.location_id is not None else ""
        ),
    }


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------
#
# Active first, archived after. Within each bucket, alphabetical by serial so
# the page order is stable.

_LIST_ORDER = case((ItemUnit.archived_at.is_(None), 0), else_=1)


@router.get("/{item_id}/units", response_class=HTMLResponse)
def list_item_units(
    request: Request,
    item_id: int,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item(db, item_id)
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(ItemUnit).where(ItemUnit.item_id == item.id)
    if show == "active":
        stmt = stmt.where(ItemUnit.archived_at.is_(None))
    else:
        stmt = stmt.where(ItemUnit.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, ItemUnit.serial_or_label)
    units = list(db.execute(stmt).scalars().all())

    location_names: dict[int, str] = {}
    for unit in units:
        if unit.location_id is not None and unit.location_id not in location_names:
            loc = db.get(Location, unit.location_id)
            if loc is not None:
                location_names[unit.location_id] = loc.name

    can_create = _user.role in (Role.MANAGER, Role.ADMIN) and (
        item.tracking_mode is TrackingMode.UNIQUE and item.archived_at is None
    )
    can_archive = _user.role in (Role.MANAGER, Role.ADMIN)

    return templates.TemplateResponse(
        request,
        "item_units_list.html",
        {
            "current_user": _user,
            "item": item,
            "units": units,
            "location_names": location_names,
            "show": show,
            "can_create": can_create,
            "can_archive": can_archive,
            "is_unique_tracked": item.tracking_mode is TrackingMode.UNIQUE,
            "is_item_archived": item.archived_at is not None,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/{item_id}/units/new", response_class=HTMLResponse)
def new_item_unit_form(
    request: Request,
    item_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    item = _get_item(db, item_id)
    _require_unique_tracking(item)
    if item.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add units under an archived item",
        )
    return templates.TemplateResponse(
        request,
        "item_unit_form.html",
        {
            "current_user": _user,
            "item": item,
            "unit": None,
            "form": _form_for_unit(None),
            "title": f"New unit for {item.name}",
            "action": f"/admin/items/{item.id}/units",
            "back_url": f"/admin/items/{item.id}/units",
            "location_options": _location_options(db),
            "statuses": [s.value for s in ItemUnitStatus],
        },
    )


@router.post("/{item_id}/units")
def create_item_unit(
    request: Request,
    item_id: int,
    serial_or_label: str = Form(""),
    status_value: str = Form("", alias="status"),
    location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    item = _get_item(db, item_id)
    _require_unique_tracking(item)
    if item.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add units under an archived item",
        )

    fields = _normalise(
        db,
        serial_or_label=serial_or_label,
        status_value=status_value,
        location_id=location_id,
    )
    _check_serial_unique(
        db, item_id=item.id, serial_or_label=fields["serial_or_label"]
    )

    unit = ItemUnit(
        item_id=item.id,
        serial_or_label=fields["serial_or_label"],
        status=fields["status"],
        location_id=fields["location_id"],
    )
    db.add(unit)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="item_unit.created",
        entity_type="item_unit",
        entity_id=unit.id,
        before=None,
        after={
            "item_id": item.id,
            "serial_or_label": unit.serial_or_label,
            "status": unit.status,
            "location_id": unit.location_id,
        },
    )
    db.commit()
    _flash(request, f"Unit “{unit.serial_or_label}” created.")
    return RedirectResponse(
        url=f"/admin/items/{item.id}/units",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/units/{unit_id}/edit", response_class=HTMLResponse)
def edit_item_unit_form(
    request: Request,
    unit_id: int,
    _user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    unit = _get_unit(db, unit_id)
    item = db.get(Item, unit.item_id)
    # FK guarantees this; assertion narrows for mypy.
    assert item is not None
    return templates.TemplateResponse(
        request,
        "item_unit_form.html",
        {
            "current_user": _user,
            "item": item,
            "unit": unit,
            "form": _form_for_unit(unit),
            "title": f"Edit unit {unit.serial_or_label}",
            "action": f"/admin/items/units/{unit.id}",
            "back_url": f"/admin/items/{item.id}/units",
            "location_options": _location_options(
                db, current_id=unit.location_id
            ),
            "statuses": [s.value for s in ItemUnitStatus],
        },
    )


@router.post("/units/{unit_id}")
def update_item_unit(
    request: Request,
    unit_id: int,
    serial_or_label: str = Form(""),
    status_value: str = Form("", alias="status"),
    location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    unit = _get_unit(db, unit_id)
    item = db.get(Item, unit.item_id)
    assert item is not None

    fields = _normalise(
        db,
        serial_or_label=serial_or_label,
        status_value=status_value,
        location_id=location_id,
        current_location_id=unit.location_id,
    )
    _check_serial_unique(
        db,
        item_id=unit.item_id,
        serial_or_label=fields["serial_or_label"],
        exclude_id=unit.id,
    )

    diff = _diff(unit, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(unit, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="item_unit.updated",
            entity_type="item_unit",
            entity_id=unit.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Unit “{unit.serial_or_label}” updated.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/items/{item.id}/units",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (soft delete)
# ---------------------------------------------------------------------------


@router.post("/units/{unit_id}/archive")
def archive_item_unit(
    request: Request,
    unit_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    unit = _get_unit(db, unit_id)
    item = db.get(Item, unit.item_id)
    assert item is not None

    if unit.archived_at is None:
        unit.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="item_unit.archived",
            entity_type="item_unit",
            entity_id=unit.id,
            before={"archived_at": None},
            after={"archived_at": unit.archived_at},
        )
        db.commit()
        _flash(request, f"Unit “{unit.serial_or_label}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/items/{item.id}/units",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/units/{unit_id}/unarchive")
def unarchive_item_unit(
    request: Request,
    unit_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    unit = _get_unit(db, unit_id)
    item = db.get(Item, unit.item_id)
    assert item is not None

    if unit.archived_at is not None:
        previous = unit.archived_at
        unit.archived_at = None
        record_audit(
            db,
            actor=user,
            action="item_unit.unarchived",
            entity_type="item_unit",
            entity_id=unit.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Unit “{unit.serial_or_label}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/items/{item.id}/units",
        status_code=status.HTTP_303_SEE_OTHER,
    )
