"""Manager-owned stone-shape CRUD routes.

Stone shapes are the administered lookup that replaces freetext shape on
tracked stones (S1 of the architectural additions spec). The seed list
(round, oval, cushion, …) ships in migration 0025; this module is the
operator surface for adding shop-specific shapes that come up later.

Shape mirrors ``app/locations.py`` deliberately — name + sort_order +
archived_at, soft-deletable, name unique across active + archived rows
(same archive-doesn't-free-the-name posture as the rest of the lookups).
No CSV upload route — the seed set covers every shape the spec lists and
new entries are rare; a future slice can add the upload path if
operators bulk-load custom shapes.

Access: ``Manager`` and ``Admin``. Workshop + Office both 403 — Office
is a sibling role, not a subset, per MISSION §3.
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
from app.models import Role, StoneShape, User
from app.template_env import templates

router = APIRouter(prefix="/admin/stone-shapes", tags=["stone-shapes"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELDS: tuple[str, ...] = ("name", "sort_order")


def _normalise(form: dict[str, str]) -> dict[str, Any]:
    """Strip whitespace; parse sort_order as int (blank → 0)."""
    name = (form.get("name") or "").strip()
    sort_raw = (form.get("sort_order") or "").strip()
    if not sort_raw:
        sort_order = 0
    else:
        try:
            sort_order = int(sort_raw)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sort order must be a whole number",
            ) from exc
    return {"name": name, "sort_order": sort_order}


def _validate_name(name: str) -> str:
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    return name


def _check_name_unique(
    db: Session, name: str, *, exclude_id: int | None = None
) -> None:
    """Same archive-doesn't-free-the-name semantics as the other lookups."""
    stmt = select(StoneShape.id).where(StoneShape.name == name)
    if exclude_id is not None:
        stmt = stmt.where(StoneShape.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a stone shape with that name already exists",
        )


def _diff(
    shape: StoneShape, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(before, after)`` of changed fields only, or ``None`` if no-op."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(shape, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


# Active shapes render before archived ones; within each bucket the
# admin-set ``sort_order`` drives the row order, with ``name`` as the
# tie-breaker so the page is stable across loads.
_LIST_ORDER = case((StoneShape.archived_at.is_(None), 0), else_=1)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("")
def list_stone_shapes(
    request: Request,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(StoneShape)
    if show == "active":
        stmt = stmt.where(StoneShape.archived_at.is_(None))
    else:
        stmt = stmt.where(StoneShape.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, StoneShape.sort_order, StoneShape.name)

    rows = list(db.execute(stmt).scalars().all())

    return templates.TemplateResponse(
        request,
        "stone_shapes_list.html",
        {
            "current_user": _user,
            "stone_shapes": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_stone_shape_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "stone_shapes_form.html",
        {
            "current_user": _user,
            "stone_shape": None,
            "form": {"name": "", "sort_order": ""},
            "title": "New stone shape",
            "action": "/admin/stone-shapes",
        },
    )


@router.post("")
def create_stone_shape(
    request: Request,
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise({"name": name, "sort_order": sort_order})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"])

    shape = StoneShape(name=fields["name"], sort_order=fields["sort_order"])
    db.add(shape)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="stone_shape.created",
        entity_type="stone_shape",
        entity_id=shape.id,
        before=None,
        after={f: fields[f] for f in _FIELDS},
    )
    db.commit()
    _flash(request, f"Stone shape “{shape.name}” created.")
    return RedirectResponse(
        url="/admin/stone-shapes", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{shape_id}/edit", response_class=HTMLResponse)
def edit_stone_shape_form(
    request: Request,
    shape_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    shape = db.get(StoneShape, shape_id)
    if shape is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone shape not found"
        )
    return templates.TemplateResponse(
        request,
        "stone_shapes_form.html",
        {
            "current_user": _user,
            "stone_shape": shape,
            "form": {"name": shape.name, "sort_order": str(shape.sort_order)},
            "title": f"Edit {shape.name}",
            "action": f"/admin/stone-shapes/{shape.id}",
        },
    )


@router.post("/{shape_id}")
def update_stone_shape(
    request: Request,
    shape_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    shape = db.get(StoneShape, shape_id)
    if shape is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone shape not found"
        )

    fields = _normalise({"name": name, "sort_order": sort_order})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"], exclude_id=shape.id)

    diff = _diff(shape, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(shape, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="stone_shape.updated",
            entity_type="stone_shape",
            entity_id=shape.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Stone shape “{shape.name}” updated.")
    else:
        # No-op POST: don't write an audit row, but still 303 so the
        # browser's POST-redirect-GET cycle completes cleanly.
        db.rollback()

    return RedirectResponse(
        url="/admin/stone-shapes", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Archive / unarchive
# ---------------------------------------------------------------------------


@router.post("/{shape_id}/archive")
def archive_stone_shape(
    request: Request,
    shape_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    shape = db.get(StoneShape, shape_id)
    if shape is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone shape not found"
        )

    if shape.archived_at is None:
        shape.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="stone_shape.archived",
            entity_type="stone_shape",
            entity_id=shape.id,
            before={"archived_at": None},
            after={"archived_at": shape.archived_at},
        )
        db.commit()
        _flash(request, f"Stone shape “{shape.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/stone-shapes", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{shape_id}/unarchive")
def unarchive_stone_shape(
    request: Request,
    shape_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    shape = db.get(StoneShape, shape_id)
    if shape is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone shape not found"
        )

    if shape.archived_at is not None:
        previous = shape.archived_at
        shape.archived_at = None
        record_audit(
            db,
            actor=user,
            action="stone_shape.unarchived",
            entity_type="stone_shape",
            entity_id=shape.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Stone shape “{shape.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/stone-shapes", status_code=status.HTTP_303_SEE_OTHER
    )
