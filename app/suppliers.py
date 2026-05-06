"""Manager-owned supplier CRUD routes.

Suppliers are the simplest settings entity: name + a few contact fields, no
schema versioning, no children. They are required for purchase orders (PO1+),
so this slice lands them first.

Access: ``Manager`` and ``Admin`` (admins always pass ``require_role``).
Workshop and Office both 403 — Office is a sibling role, not a subset, per
MISSION §3 ("Office: items, movements, POs ... cannot manage the taxonomy").

All mutations are POST + 303 + audit-logged. Validation failures raise
``HTTPException(400, ...)``; a future iteration may upgrade to in-form error
rendering for a better UX (queued in self-critique).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import Role, Supplier, User

# Templates injected from app.main so the same Jinja env (with the CSRF
# context processor) is shared across the app. We avoid building a second
# Jinja2Templates instance here because that would silently drop the CSRF
# token from supplier templates.
_templates: Jinja2Templates | None = None


def init_templates(templates: Jinja2Templates) -> None:
    """Wire in the shared ``Jinja2Templates`` from the app factory."""
    global _templates
    _templates = templates


def _t() -> Jinja2Templates:
    if _templates is None:
        raise RuntimeError("suppliers.init_templates() was never called")
    return _templates


router = APIRouter(prefix="/admin/suppliers", tags=["suppliers"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELDS: tuple[str, ...] = ("name", "email", "phone", "notes")


def _normalise(form: dict[str, str]) -> dict[str, str | None]:
    """Strip whitespace; treat empty string as ``None`` for optional fields."""
    name = (form.get("name") or "").strip()
    out: dict[str, str | None] = {"name": name}
    for f in ("email", "phone", "notes"):
        v = (form.get(f) or "").strip()
        out[f] = v or None
    return out


def _validate_name(name: str | None) -> str:
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    return name


def _check_name_unique(
    db: Session, name: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Supplier.id).where(Supplier.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Supplier.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a supplier with that name already exists",
        )


def _diff(supplier: Supplier, new: dict[str, str | None]) -> tuple[
    dict[str, Any], dict[str, Any]
] | None:
    """Return ``(before, after)`` of *changed* fields only, or None if no-op."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(supplier, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    """Stash a one-shot message in the session; rendered + cleared by base.html."""
    request.session["flash"] = message


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------
#
# Active first, then archived. Within each bucket, alphabetical so the page is
# stable across loads (no ORDER BY on `created_at` would tilt by recency).

_LIST_ORDER = case((Supplier.archived_at.is_(None), 0), else_=1)


@router.get("", response_class=HTMLResponse)
def list_suppliers(
    request: Request,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(Supplier)
    if show == "active":
        stmt = stmt.where(Supplier.archived_at.is_(None))
    else:
        stmt = stmt.where(Supplier.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, Supplier.name)

    rows = list(db.execute(stmt).scalars().all())
    return _t().TemplateResponse(
        request,
        "suppliers_list.html",
        {
            "current_user": _user,
            "suppliers": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_supplier_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return _t().TemplateResponse(
        request,
        "suppliers_form.html",
        {
            "current_user": _user,
            "supplier": None,
            "form": {"name": "", "email": "", "phone": "", "notes": ""},
            "title": "New supplier",
            "action": "/admin/suppliers",
        },
    )


@router.post("")
def create_supplier(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise({"name": name, "email": email, "phone": phone, "notes": notes})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"])  # type: ignore[arg-type]

    supplier = Supplier(
        name=fields["name"],
        email=fields["email"],
        phone=fields["phone"],
        notes=fields["notes"],
    )
    db.add(supplier)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="supplier.created",
        entity_type="supplier",
        entity_id=supplier.id,
        before=None,
        after={f: fields[f] for f in _FIELDS},
    )
    db.commit()
    _flash(request, f"Supplier “{supplier.name}” created.")
    return RedirectResponse(
        url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{supplier_id}/edit", response_class=HTMLResponse)
def edit_supplier_form(
    request: Request,
    supplier_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found"
        )
    return _t().TemplateResponse(
        request,
        "suppliers_form.html",
        {
            "current_user": _user,
            "supplier": supplier,
            "form": {
                "name": supplier.name,
                "email": supplier.email or "",
                "phone": supplier.phone or "",
                "notes": supplier.notes or "",
            },
            "title": f"Edit {supplier.name}",
            "action": f"/admin/suppliers/{supplier.id}",
        },
    )


@router.post("/{supplier_id}")
def update_supplier(
    request: Request,
    supplier_id: int,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found"
        )

    fields = _normalise({"name": name, "email": email, "phone": phone, "notes": notes})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"], exclude_id=supplier.id)  # type: ignore[arg-type]

    diff = _diff(supplier, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(supplier, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="supplier.updated",
            entity_type="supplier",
            entity_id=supplier.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Supplier “{supplier.name}” updated.")
    else:
        # No-op: don't write an audit row, but still 303 to the list so the
        # browser's POST-redirect-GET cycle completes cleanly.
        db.rollback()

    return RedirectResponse(
        url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (soft delete)
# ---------------------------------------------------------------------------


@router.post("/{supplier_id}/archive")
def archive_supplier(
    request: Request,
    supplier_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found"
        )

    if supplier.archived_at is None:
        supplier.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="supplier.archived",
            entity_type="supplier",
            entity_id=supplier.id,
            before={"archived_at": None},
            after={"archived_at": supplier.archived_at},
        )
        db.commit()
        _flash(request, f"Supplier “{supplier.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{supplier_id}/unarchive")
def unarchive_supplier(
    request: Request,
    supplier_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found"
        )

    if supplier.archived_at is not None:
        previous = supplier.archived_at
        supplier.archived_at = None
        record_audit(
            db,
            actor=user,
            action="supplier.unarchived",
            entity_type="supplier",
            entity_id=supplier.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Supplier “{supplier.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER
    )
