"""Manager-owned units admin (``/admin/units``).

S5 of the architectural additions spec. Mirrors the stone-shapes /
suppliers / locations CRUD pattern: name + sort_order + soft-delete.
Seeded with 10 canonical units (ea, pc, g, kg, ct, mm, cm, m, pair,
pack) in migration 0040; new entries are rare.

Access: ``Manager`` and ``Admin`` only — Workshop and Office both 403,
same posture as the rest of the settings admins.
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
from app.models import Role, Unit, User
from app.template_env import templates

router = APIRouter(prefix="/admin/units", tags=["units"])


_FIELDS: tuple[str, ...] = ("code", "name", "sort_order")


def _normalise(form: dict[str, str]) -> dict[str, Any]:
    code = (form.get("code") or "").strip().lower()
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code is required",
        )
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="name is required",
        )
    sort_raw = (form.get("sort_order") or "").strip()
    sort_order = 0
    if sort_raw:
        try:
            sort_order = int(sort_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sort_order must be a whole number",
            ) from exc
    return {"code": code, "name": name, "sort_order": sort_order}


def _check_code_unique(
    db: Session, code: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Unit.id).where(Unit.code == code)
    if exclude_id is not None:
        stmt = stmt.where(Unit.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a unit with that code already exists",
        )


def _diff(unit: Unit, new: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(unit, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    return (before, after) if before else None


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


_LIST_ORDER = case((Unit.archived_at.is_(None), 0), else_=1)


@router.get("")
def list_units(
    request: Request,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"
    stmt = select(Unit)
    if show == "active":
        stmt = stmt.where(Unit.archived_at.is_(None))
    else:
        stmt = stmt.where(Unit.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, Unit.sort_order, Unit.code)
    rows = list(db.execute(stmt).scalars().all())
    return templates.TemplateResponse(
        request,
        "units_list.html",
        {"current_user": _user, "units": rows, "show": show},
    )


@router.get("/new", response_class=HTMLResponse)
def new_unit_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "units_form.html",
        {
            "current_user": _user,
            "unit": None,
            "form": {"code": "", "name": "", "sort_order": ""},
            "title": "New unit",
            "action": "/admin/units",
        },
    )


@router.post("")
def create_unit(
    request: Request,
    code: str = Form(""),
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise({"code": code, "name": name, "sort_order": sort_order})
    _check_code_unique(db, fields["code"])
    unit = Unit(**fields)
    db.add(unit)
    db.flush()
    record_audit(
        db, actor=user, action="unit.created",
        entity_type="unit", entity_id=unit.id,
        before=None, after={f: fields[f] for f in _FIELDS},
    )
    db.commit()
    _flash(request, f"Unit “{unit.code}” created.")
    return RedirectResponse(url="/admin/units", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/{unit_id}/edit", response_class=HTMLResponse)
def edit_unit_form(
    request: Request,
    unit_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit not found")
    return templates.TemplateResponse(
        request,
        "units_form.html",
        {
            "current_user": _user,
            "unit": unit,
            "form": {
                "code": unit.code,
                "name": unit.name,
                "sort_order": str(unit.sort_order),
            },
            "title": f"Edit {unit.code}",
            "action": f"/admin/units/{unit.id}",
        },
    )


@router.post("/{unit_id}")
def update_unit(
    request: Request,
    unit_id: int,
    code: str = Form(""),
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit not found")
    fields = _normalise({"code": code, "name": name, "sort_order": sort_order})
    _check_code_unique(db, fields["code"], exclude_id=unit.id)

    diff = _diff(unit, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(unit, f, fields[f])
        record_audit(
            db, actor=user, action="unit.updated",
            entity_type="unit", entity_id=unit.id,
            before=before, after=after,
        )
        db.commit()
        _flash(request, f"Unit “{unit.code}” updated.")
    else:
        db.rollback()
    return RedirectResponse(url="/admin/units", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{unit_id}/archive")
def archive_unit(
    request: Request,
    unit_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit not found")
    if unit.archived_at is None:
        unit.archived_at = datetime.now(UTC)
        record_audit(
            db, actor=user, action="unit.archived",
            entity_type="unit", entity_id=unit.id,
            before={"archived_at": None}, after={"archived_at": unit.archived_at},
        )
        db.commit()
        _flash(request, f"Unit “{unit.code}” archived.")
    else:
        db.rollback()
    return RedirectResponse(url="/admin/units", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{unit_id}/unarchive")
def unarchive_unit(
    request: Request,
    unit_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    unit = db.get(Unit, unit_id)
    if unit is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unit not found")
    if unit.archived_at is not None:
        previous = unit.archived_at
        unit.archived_at = None
        record_audit(
            db, actor=user, action="unit.unarchived",
            entity_type="unit", entity_id=unit.id,
            before={"archived_at": previous}, after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Unit “{unit.code}” restored.")
    else:
        db.rollback()
    return RedirectResponse(url="/admin/units", status_code=status.HTTP_303_SEE_OTHER)
