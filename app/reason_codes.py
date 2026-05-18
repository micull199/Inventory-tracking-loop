"""Manager-owned reason-codes admin (``/admin/reason-codes``).

S5 of the architectural additions spec. Reason codes are scoped by
``MovementType`` (``in`` / ``out`` / ``adjustment`` / ``transfer`` /
``stage_change``) so ``sale`` and ``po_receipt`` don't compete for the
same pick list. The lookup pairs with the freetext
``StockMovement.reason`` column, which stays for the long tail
(one-off explanations that don't deserve a code).

Spec §6 seeds the seven canonical ``out`` reasons; this route lets
managers add codes for other movement types as recurrent reasons
emerge.

Access: ``Manager`` and ``Admin`` only.
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
from app.models import MovementType, ReasonCode, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/reason-codes", tags=["reason-codes"])


_MOVEMENT_TYPE_VALUES: tuple[str, ...] = tuple(m.value for m in MovementType)

_FIELDS: tuple[str, ...] = ("movement_type", "code", "label", "sort_order")


def _validate_movement_type(raw: str) -> str:
    text = (raw or "").strip()
    if text not in _MOVEMENT_TYPE_VALUES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"movement_type must be one of: {', '.join(_MOVEMENT_TYPE_VALUES)}",
        )
    return text


def _normalise(form: dict[str, str]) -> dict[str, Any]:
    movement_type = _validate_movement_type(form.get("movement_type") or "")
    code = (form.get("code") or "").strip().lower()
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code is required",
        )
    label = (form.get("label") or "").strip()
    if not label:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="label is required",
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
    return {
        "movement_type": movement_type,
        "code": code,
        "label": label,
        "sort_order": sort_order,
    }


def _check_unique(
    db: Session,
    movement_type: str,
    code: str,
    *,
    exclude_id: int | None = None,
) -> None:
    """Same archive-doesn't-free-the-code semantics scoped by movement_type.

    The DB partial-unique index would catch this too; surfacing here
    yields a friendlier error than a deferred IntegrityError.
    """
    stmt = (
        select(ReasonCode.id)
        .where(ReasonCode.movement_type == movement_type)
        .where(ReasonCode.code == code)
    )
    if exclude_id is not None:
        stmt = stmt.where(ReasonCode.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"a reason code {code!r} already exists for movement type "
                f"{movement_type!r}"
            ),
        )


def _diff(
    rc: ReasonCode, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(rc, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    return (before, after) if before else None


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


_LIST_ORDER = case((ReasonCode.archived_at.is_(None), 0), else_=1)


@router.get("")
def list_reason_codes(
    request: Request,
    show: str = "active",
    movement_type: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"
    stmt = select(ReasonCode)
    if show == "active":
        stmt = stmt.where(ReasonCode.archived_at.is_(None))
    else:
        stmt = stmt.where(ReasonCode.archived_at.is_not(None))
    # Optional ?movement_type filter for the same UX as
    # /admin/metal-prices?metal_id=…
    selected_type = movement_type.strip() if movement_type.strip() in _MOVEMENT_TYPE_VALUES else ""
    if selected_type:
        stmt = stmt.where(ReasonCode.movement_type == selected_type)
    stmt = stmt.order_by(
        _LIST_ORDER,
        ReasonCode.movement_type,
        ReasonCode.sort_order,
        ReasonCode.code,
    )
    rows = list(db.execute(stmt).scalars().all())
    return templates.TemplateResponse(
        request,
        "reason_codes_list.html",
        {
            "current_user": _user,
            "reason_codes": rows,
            "show": show,
            "movement_types": list(_MOVEMENT_TYPE_VALUES),
            "selected_movement_type": selected_type,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_reason_code_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "reason_codes_form.html",
        {
            "current_user": _user,
            "reason_code": None,
            "form": {
                "movement_type": "out",
                "code": "",
                "label": "",
                "sort_order": "",
            },
            "title": "New reason code",
            "action": "/admin/reason-codes",
            "movement_types": list(_MOVEMENT_TYPE_VALUES),
        },
    )


@router.post("")
def create_reason_code(
    request: Request,
    movement_type: str = Form(""),
    code: str = Form(""),
    label: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise(
        {
            "movement_type": movement_type,
            "code": code,
            "label": label,
            "sort_order": sort_order,
        }
    )
    _check_unique(db, fields["movement_type"], fields["code"])
    rc = ReasonCode(**fields)
    db.add(rc)
    db.flush()
    record_audit(
        db, actor=user, action="reason_code.created",
        entity_type="reason_code", entity_id=rc.id,
        before=None, after={f: fields[f] for f in _FIELDS},
    )
    db.commit()
    _flash(request, f"Reason code “{rc.code}” created for {rc.movement_type}.")
    return RedirectResponse(
        url="/admin/reason-codes", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{reason_id}/edit", response_class=HTMLResponse)
def edit_reason_code_form(
    request: Request,
    reason_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    rc = db.get(ReasonCode, reason_id)
    if rc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="reason code not found"
        )
    return templates.TemplateResponse(
        request,
        "reason_codes_form.html",
        {
            "current_user": _user,
            "reason_code": rc,
            "form": {
                "movement_type": rc.movement_type,
                "code": rc.code,
                "label": rc.label,
                "sort_order": str(rc.sort_order),
            },
            "title": f"Edit {rc.code} ({rc.movement_type})",
            "action": f"/admin/reason-codes/{rc.id}",
            "movement_types": list(_MOVEMENT_TYPE_VALUES),
        },
    )


@router.post("/{reason_id}")
def update_reason_code(
    request: Request,
    reason_id: int,
    movement_type: str = Form(""),
    code: str = Form(""),
    label: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    rc = db.get(ReasonCode, reason_id)
    if rc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="reason code not found"
        )
    fields = _normalise(
        {
            "movement_type": movement_type,
            "code": code,
            "label": label,
            "sort_order": sort_order,
        }
    )
    _check_unique(db, fields["movement_type"], fields["code"], exclude_id=rc.id)

    diff = _diff(rc, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(rc, f, fields[f])
        record_audit(
            db, actor=user, action="reason_code.updated",
            entity_type="reason_code", entity_id=rc.id,
            before=before, after=after,
        )
        db.commit()
        _flash(request, f"Reason code “{rc.code}” updated.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/reason-codes", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{reason_id}/archive")
def archive_reason_code(
    request: Request,
    reason_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    rc = db.get(ReasonCode, reason_id)
    if rc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="reason code not found"
        )
    if rc.archived_at is None:
        rc.archived_at = datetime.now(UTC)
        record_audit(
            db, actor=user, action="reason_code.archived",
            entity_type="reason_code", entity_id=rc.id,
            before={"archived_at": None}, after={"archived_at": rc.archived_at},
        )
        db.commit()
        _flash(request, f"Reason code “{rc.code}” archived.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/reason-codes", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{reason_id}/unarchive")
def unarchive_reason_code(
    request: Request,
    reason_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    rc = db.get(ReasonCode, reason_id)
    if rc is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="reason code not found"
        )
    if rc.archived_at is not None:
        previous = rc.archived_at
        rc.archived_at = None
        record_audit(
            db, actor=user, action="reason_code.unarchived",
            entity_type="reason_code", entity_id=rc.id,
            before={"archived_at": previous}, after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Reason code “{rc.code}” restored.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/reason-codes", status_code=status.HTTP_303_SEE_OTHER
    )
