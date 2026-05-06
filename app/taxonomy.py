"""Manager-owned taxonomy CRUD routes.

The taxonomy is a two-level hierarchy (Category → Sub-category) used to classify
items. This module covers **top-level categories only** (S3): every row written
or read here has ``parent_id IS NULL``. Sub-categories arrive in S4 and will
share the same ``taxonomy_nodes`` table.

Shape mirrors ``app/suppliers.py`` and ``app/locations.py`` deliberately: this
is the third concrete instance of the settings-CRUD pattern. Once the duplication
is undeniable across all three, the helper extraction (`_normalise`,
`_validate_name`, `_check_name_unique`, `_diff`, `_flash`) gets evaluated — but
not before, because two copies isn't enough signal to design a good abstraction.

Access: ``Manager`` and ``Admin``. Workshop and Office both 403 — Office is a
sibling role, not a subset, per MISSION §3 ("Office cannot manage the taxonomy").
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import Role, TaxonomyNode, User
from app.template_env import templates

router = APIRouter(prefix="/admin/taxonomy", tags=["taxonomy"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields tracked in audit diffs. ``parent_id`` is intentionally omitted in S3
# because the routes never let the user change it — every node here is
# top-level. When S4 lands, parent_id becomes part of the diff vocabulary.
_FIELDS: tuple[str, ...] = ("name", "sort_order")

# Step used when the user creates a top-level node without specifying
# ``sort_order``. Stepping by 10 leaves room to insert a new node between two
# existing ones without renumbering everything.
_SORT_ORDER_STEP = 10


def _normalise(name: str, sort_order: str) -> dict[str, Any]:
    """Strip whitespace; coerce ``sort_order`` to int (or ``None`` if blank)."""
    clean_name = (name or "").strip()
    raw_sort = (sort_order or "").strip()
    sort_value: int | None
    if raw_sort == "":
        sort_value = None
    else:
        try:
            sort_value = int(raw_sort)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="sort_order must be a whole number",
            ) from exc
    return {"name": clean_name, "sort_order": sort_value}


def _validate_name(name: str) -> str:
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    return name


def _check_top_name_unique(
    db: Session, name: str, *, exclude_id: int | None = None
) -> None:
    """Reject a name already used by another top-level node (active or archived)."""
    stmt = (
        select(TaxonomyNode.id)
        .where(TaxonomyNode.parent_id.is_(None))
        .where(TaxonomyNode.name == name)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyNode.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a category with that name already exists",
        )


def _next_top_sort_order(db: Session) -> int:
    """Default sort_order for a new top-level node: max(existing) + step."""
    stmt = select(func.max(TaxonomyNode.sort_order)).where(
        TaxonomyNode.parent_id.is_(None)
    )
    current_max = db.execute(stmt).scalar()
    if current_max is None:
        return 0
    return int(current_max) + _SORT_ORDER_STEP


def _diff(
    node: TaxonomyNode, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(before, after)`` of *changed* fields only, or None if no-op."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(node, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------
#
# Active first, then archived. Within each bucket, by sort_order then name so
# the page is stable across loads and reflects the manager's intended ordering.

_LIST_ORDER = case((TaxonomyNode.archived_at.is_(None), 0), else_=1)


@router.get("", response_class=HTMLResponse)
def list_taxonomy(
    request: Request,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(TaxonomyNode).where(TaxonomyNode.parent_id.is_(None))
    if show == "active":
        stmt = stmt.where(TaxonomyNode.archived_at.is_(None))
    else:
        stmt = stmt.where(TaxonomyNode.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, TaxonomyNode.sort_order, TaxonomyNode.name)

    rows = list(db.execute(stmt).scalars().all())
    return templates.TemplateResponse(
        request,
        "taxonomy_list.html",
        {
            "current_user": _user,
            "nodes": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_taxonomy_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": None,
            "form": {"name": "", "sort_order": ""},
            "title": "New category",
            "action": "/admin/taxonomy",
        },
    )


@router.post("")
def create_taxonomy(
    request: Request,
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_top_name_unique(db, fields["name"])
    if fields["sort_order"] is None:
        fields["sort_order"] = _next_top_sort_order(db)

    node = TaxonomyNode(
        parent_id=None,
        name=fields["name"],
        sort_order=fields["sort_order"],
    )
    db.add(node)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="taxonomy_node.created",
        entity_type="taxonomy_node",
        entity_id=node.id,
        before=None,
        after={
            "name": node.name,
            "sort_order": node.sort_order,
            "parent_id": None,
        },
    )
    db.commit()
    _flash(request, f"Category “{node.name}” created.")
    return RedirectResponse(
        url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{node_id}/edit", response_class=HTMLResponse)
def edit_taxonomy_form(
    request: Request,
    node_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is not None:
        # In S3 the route only ever surfaces top-level nodes. A request for a
        # sub-category id is a 404 here — S4 will introduce its own edit route.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="category not found"
        )
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": node,
            "form": {
                "name": node.name,
                "sort_order": str(node.sort_order),
            },
            "title": f"Edit {node.name}",
            "action": f"/admin/taxonomy/{node.id}",
        },
    )


@router.post("/{node_id}")
def update_taxonomy(
    request: Request,
    node_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="category not found"
        )

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_top_name_unique(db, fields["name"], exclude_id=node.id)
    if fields["sort_order"] is None:
        # A blank sort_order on edit means "leave alone", not "reset". Without
        # this the field would silently snap back to whatever default the form
        # carried (zero).
        fields["sort_order"] = node.sort_order

    diff = _diff(node, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(node, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="taxonomy_node.updated",
            entity_type="taxonomy_node",
            entity_id=node.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Category “{node.name}” updated.")
    else:
        # No-op: don't write an audit row, but still 303 so POST-redirect-GET
        # completes cleanly. Matches suppliers/locations behaviour.
        db.rollback()

    return RedirectResponse(
        url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (soft delete)
# ---------------------------------------------------------------------------


@router.post("/{node_id}/archive")
def archive_taxonomy(
    request: Request,
    node_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="category not found"
        )

    if node.archived_at is None:
        node.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="taxonomy_node.archived",
            entity_type="taxonomy_node",
            entity_id=node.id,
            before={"archived_at": None},
            after={"archived_at": node.archived_at},
        )
        db.commit()
        _flash(request, f"Category “{node.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{node_id}/unarchive")
def unarchive_taxonomy(
    request: Request,
    node_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="category not found"
        )

    if node.archived_at is not None:
        previous = node.archived_at
        node.archived_at = None
        record_audit(
            db,
            actor=user,
            action="taxonomy_node.unarchived",
            entity_type="taxonomy_node",
            entity_id=node.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Category “{node.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER
    )
