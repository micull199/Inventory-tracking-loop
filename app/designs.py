"""Manager-owned design CRUD routes + ``DSG-NNNN`` allocator.

Spec §3 / ADR-003. Designs separate design IP (CAD file, designer
credit, intro date, standard cost) from the production hierarchy
(taxonomy tree). The items-level FK + backfill is **deliberately
deferred** to a follow-up slice — see the ADR for the staged-rollout
rationale.

This module ships CRUD (list / create / edit / archive / unarchive).
The ADR-003 follow-up items still pending are the ``items.design_id``
FK + backfill + design picker in the items form. Per the ADR, archive
UX deliberately ships *separately* from the items FK so operators can
mark old designs as discontinued without committing the items table
to referencing them.

Access mirrors the other lookup admins: ``Manager`` + ``Admin`` only.
Workshop and Office both 403 — Office is a sibling role, not a subset
of Manager, per MISSION §3.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import Design, Metal, Role, SequenceCounter, StyleFamily, User
from app.template_env import templates

router = APIRouter(prefix="/admin/designs", tags=["designs"])


# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------

_DESIGN_CODE_COUNTER_NAME = "design_code"
_DESIGN_CODE_PAD_WIDTH = 4


def allocate_design_code(db: Session) -> str:
    """Atomically allocate the next ``DSG-NNNN`` design code.

    Mirrors ``app.stones.allocate_stone_code`` exactly — single
    ``UPDATE … RETURNING`` round-trip serialises concurrent allocators
    on the shared ``sequence_counters`` row. SQLite (>= 3.35) and
    Postgres both support this.

    Raises ``RuntimeError`` if the counter row is missing (the seed
    insert in migration 0044 should make this impossible) — defensive
    surface that yields a clearer error than the cryptic empty-tuple
    unpacking that would otherwise happen.
    """
    stmt = (
        sa.update(SequenceCounter)
        .where(SequenceCounter.name == _DESIGN_CODE_COUNTER_NAME)
        .values(next_value=SequenceCounter.next_value + 1)
        .returning(SequenceCounter.next_value)
    )
    result = db.execute(stmt)
    rows = result.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            "allocate_design_code: counter row missing — was migration "
            f"0044 applied? (expected 1 matched row, got {len(rows)})"
        )
    allocated = int(rows[0][0]) - 1
    return f"DSG-{allocated:0{_DESIGN_CODE_PAD_WIDTH}d}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Editable fields (everything except ``id``, ``design_code`` which is
# allocator-assigned and immutable, and timestamps). Order is the order
# audit ``after_json`` entries iterate; keep stable so audit history is
# greppable.
_FIELDS: tuple[str, ...] = (
    "name",
    "collection",
    "style_family",
    "designer",
    "cad_file_path",
    "cad_version",
    "cad_updated_at",
    "default_metal_id",
    "intro_date",
    "discontinued_date",
    "standard_cost",
    "notes",
)


def _parse_optional_date(raw: str, field_name: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an ISO date (YYYY-MM-DD)",
        ) from exc


def _parse_optional_datetime(raw: str, field_name: str) -> datetime | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an ISO datetime",
        ) from exc
    return parsed


def _parse_optional_decimal(raw: str, field_name: str) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc


def _parse_optional_int(raw: str, field_name: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a whole number",
        ) from exc


def _parse_style_family(raw: str) -> StyleFamily | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return StyleFamily(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown style family {text!r}",
        ) from exc


def _resolve_optional_metal(
    db: Session, raw: str, *, current_id: int | None = None
) -> int | None:
    """Resolve a ``default_metal_id`` form field to a real metal row.

    Returns ``None`` on blank input. Accepts the existing ``current_id``
    even if that metal is now archived (so editing a design doesn't
    silently drop an archived metal reference). New picks against an
    archived metal are rejected — same posture as
    ``app.items._resolve_optional_supplier``.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        metal_id = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="default_metal_id must be an integer",
        ) from exc
    metal = db.get(Metal, metal_id)
    if metal is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"metal id {metal_id} does not exist",
        )
    if metal.archived_at is not None and metal_id != current_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"metal {metal.metal_code!r} is archived",
        )
    return metal_id


def _normalise(
    db: Session,
    form: dict[str, str],
    *,
    current_metal_id: int | None = None,
) -> dict[str, Any]:
    """Strip + parse every form field. Returns the stored-value shape."""
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    return {
        "name": name,
        "collection": (form.get("collection") or "").strip() or None,
        "style_family": _parse_style_family(form.get("style_family") or ""),
        "designer": (form.get("designer") or "").strip() or None,
        "cad_file_path": (form.get("cad_file_path") or "").strip() or None,
        "cad_version": (form.get("cad_version") or "").strip() or None,
        "cad_updated_at": _parse_optional_datetime(
            form.get("cad_updated_at") or "", "cad_updated_at"
        ),
        "default_metal_id": _resolve_optional_metal(
            db,
            form.get("default_metal_id") or "",
            current_id=current_metal_id,
        ),
        "intro_date": _parse_optional_date(
            form.get("intro_date") or "", "intro_date"
        ),
        "discontinued_date": _parse_optional_date(
            form.get("discontinued_date") or "", "discontinued_date"
        ),
        "standard_cost": _parse_optional_decimal(
            form.get("standard_cost") or "", "standard_cost"
        ),
        "notes": (form.get("notes") or "").strip() or None,
    }


def _diff(
    design: Design, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(before, after)`` of changed fields only, or ``None`` if no-op."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(design, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _metal_options(
    db: Session, *, current_id: int | None = None
) -> list[dict[str, Any]]:
    """Render-ready active metals + an archived current_id when relevant.

    Mirrors ``items._supplier_options``: archived metals stay in the
    dropdown when they're the design's current pick so an edit doesn't
    silently drop the reference; a new archived pick is rejected by
    ``_resolve_optional_metal``.
    """
    stmt = select(Metal).where(Metal.archived_at.is_(None)).order_by(Metal.metal_code)
    rows: list[Metal] = list(db.execute(stmt).scalars().all())
    if current_id is not None and not any(m.id == current_id for m in rows):
        current = db.get(Metal, current_id)
        if current is not None:
            rows.append(current)
    return [
        {
            "id": m.id,
            "label": (
                f"{m.metal_code} — {m.name}"
                + (" (archived)" if m.archived_at is not None else "")
            ),
        }
        for m in rows
    ]


_LIST_ORDER = case((Design.archived_at.is_(None), 0), else_=1)


# ---------------------------------------------------------------------------
# List (active/archived tabs)
# ---------------------------------------------------------------------------


@router.get("")
def list_designs(
    request: Request,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"
    stmt = select(Design)
    if show == "active":
        stmt = stmt.where(Design.archived_at.is_(None))
    else:
        stmt = stmt.where(Design.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, Design.design_code)
    rows = list(db.execute(stmt).scalars().all())
    return templates.TemplateResponse(
        request,
        "designs_list.html",
        {
            "current_user": _user,
            "designs": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


def _empty_form_view() -> dict[str, str]:
    return {
        "name": "",
        "collection": "",
        "style_family": "",
        "designer": "",
        "cad_file_path": "",
        "cad_version": "",
        "cad_updated_at": "",
        "default_metal_id": "",
        "intro_date": "",
        "discontinued_date": "",
        "standard_cost": "",
        "notes": "",
    }


def _form_view_for(design: Design) -> dict[str, str]:
    def _iso(v: datetime | date | None) -> str:
        return v.isoformat() if v is not None else ""

    return {
        "name": design.name,
        "collection": design.collection or "",
        "style_family": design.style_family.value if design.style_family else "",
        "designer": design.designer or "",
        "cad_file_path": design.cad_file_path or "",
        "cad_version": design.cad_version or "",
        "cad_updated_at": _iso(design.cad_updated_at),
        "default_metal_id": (
            str(design.default_metal_id) if design.default_metal_id is not None else ""
        ),
        "intro_date": _iso(design.intro_date),
        "discontinued_date": _iso(design.discontinued_date),
        "standard_cost": (
            str(design.standard_cost) if design.standard_cost is not None else ""
        ),
        "notes": design.notes or "",
    }


@router.get("/new", response_class=HTMLResponse)
def new_design_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "designs_form.html",
        {
            "current_user": _user,
            "design": None,
            "form": _empty_form_view(),
            "title": "New design",
            "action": "/admin/designs",
            "metal_options": _metal_options(db),
            "style_families": [s.value for s in StyleFamily],
        },
    )


@router.post("")
def create_design(
    request: Request,
    name: str = Form(""),
    collection: str = Form(""),
    style_family: str = Form(""),
    designer: str = Form(""),
    cad_file_path: str = Form(""),
    cad_version: str = Form(""),
    cad_updated_at: str = Form(""),
    default_metal_id: str = Form(""),
    intro_date: str = Form(""),
    discontinued_date: str = Form(""),
    standard_cost: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise(
        db,
        {
            "name": name,
            "collection": collection,
            "style_family": style_family,
            "designer": designer,
            "cad_file_path": cad_file_path,
            "cad_version": cad_version,
            "cad_updated_at": cad_updated_at,
            "default_metal_id": default_metal_id,
            "intro_date": intro_date,
            "discontinued_date": discontinued_date,
            "standard_cost": standard_cost,
            "notes": notes,
        },
    )
    design_code = allocate_design_code(db)
    design = Design(design_code=design_code, **fields)
    db.add(design)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="design.created",
        entity_type="design",
        entity_id=design.id,
        before=None,
        after={"design_code": design_code, **{f: fields[f] for f in _FIELDS}},
    )
    db.commit()
    _flash(request, f"Design “{design.name}” ({design.design_code}) created.")
    return RedirectResponse(
        url="/admin/designs", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{design_id}/edit", response_class=HTMLResponse)
def edit_design_form(
    request: Request,
    design_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    design = db.get(Design, design_id)
    if design is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="design not found"
        )
    return templates.TemplateResponse(
        request,
        "designs_form.html",
        {
            "current_user": _user,
            "design": design,
            "form": _form_view_for(design),
            "title": f"Edit {design.name}",
            "action": f"/admin/designs/{design.id}",
            "metal_options": _metal_options(db, current_id=design.default_metal_id),
            "style_families": [s.value for s in StyleFamily],
        },
    )


@router.post("/{design_id}")
def update_design(
    request: Request,
    design_id: int,
    name: str = Form(""),
    collection: str = Form(""),
    style_family: str = Form(""),
    designer: str = Form(""),
    cad_file_path: str = Form(""),
    cad_version: str = Form(""),
    cad_updated_at: str = Form(""),
    default_metal_id: str = Form(""),
    intro_date: str = Form(""),
    discontinued_date: str = Form(""),
    standard_cost: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    design = db.get(Design, design_id)
    if design is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="design not found"
        )

    fields = _normalise(
        db,
        {
            "name": name,
            "collection": collection,
            "style_family": style_family,
            "designer": designer,
            "cad_file_path": cad_file_path,
            "cad_version": cad_version,
            "cad_updated_at": cad_updated_at,
            "default_metal_id": default_metal_id,
            "intro_date": intro_date,
            "discontinued_date": discontinued_date,
            "standard_cost": standard_cost,
            "notes": notes,
        },
        current_metal_id=design.default_metal_id,
    )

    diff = _diff(design, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(design, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="design.updated",
            entity_type="design",
            entity_id=design.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Design “{design.name}” updated.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/designs", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (ADR-003 follow-up — column existed since migration 0044)
# ---------------------------------------------------------------------------


@router.post("/{design_id}/archive")
def archive_design(
    request: Request,
    design_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    design = db.get(Design, design_id)
    if design is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="design not found"
        )
    if design.archived_at is None:
        design.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="design.archived",
            entity_type="design",
            entity_id=design.id,
            before={"archived_at": None},
            after={"archived_at": design.archived_at},
        )
        db.commit()
        _flash(request, f"Design “{design.name}” archived.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/designs", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{design_id}/unarchive")
def unarchive_design(
    request: Request,
    design_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    design = db.get(Design, design_id)
    if design is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="design not found"
        )
    if design.archived_at is not None:
        previous = design.archived_at
        design.archived_at = None
        record_audit(
            db,
            actor=user,
            action="design.unarchived",
            entity_type="design",
            entity_id=design.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Design “{design.name}” restored.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/designs", status_code=status.HTTP_303_SEE_OTHER
    )
