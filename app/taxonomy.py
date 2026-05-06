"""Manager-owned taxonomy CRUD routes.

The taxonomy is a two-level hierarchy (Category → Sub-category) used to classify
items. This module covers both levels:

- **Top-level categories** (S3): rows with ``parent_id IS NULL``, surfaced under
  ``/admin/taxonomy`` (list / new / edit / archive / unarchive).
- **Sub-categories** (S4): rows with ``parent_id`` pointing at a top-level node.
  Surfaced under two URL shapes:

  - ``/admin/taxonomy/{parent_id}/children``       (list + new form scoped by parent)
  - ``/admin/taxonomy/{parent_id}/children``  POST (create under a parent)
  - ``/admin/taxonomy/sub/{node_id}/edit``         (edit a sub-cat)
  - ``/admin/taxonomy/sub/{node_id}``         POST (update name + sort_order)
  - ``/admin/taxonomy/sub/{node_id}/archive``      (archive)
  - ``/admin/taxonomy/sub/{node_id}/unarchive``    (unarchive)

  Edit/archive/unarchive use a flat ``/sub/{id}`` URL rather than nesting under
  ``{parent_id}``. Reason: nesting would invite a "URL parent" / "DB parent"
  mismatch (someone hand-edits the URL, or a refactor moves the sub-cat to a
  different parent and the URL goes stale). The handler verifies
  ``node.parent_id is not None`` and 404s otherwise; that's the only invariant
  worth checking here.

Depth limit (max two levels) is enforced in the application layer when creating
a sub-cat: the parent must itself be top-level (``parent.parent_id is None``).
The DB schema doesn't enforce this — only the route does.

Shape mirrors ``app/suppliers.py`` and ``app/locations.py`` for the top-level
routes; sub-cat routes add the depth-limit guard and a "no new sub-cats under an
archived parent" rule. The settings-CRUD helper-extraction question gets
re-evaluated after S4 (per the S3 self-critique).

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
from app.csv_export import csv_branch
from app.db import get_session
from app.field_defs import has_active_field_defs
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


def _check_child_name_unique(
    db: Session,
    *,
    parent_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    """Reject a sub-cat name already used by a sibling under ``parent_id``.

    The DB-level partial unique index ``uq_taxonomy_child_name`` enforces the
    same invariant, but a friendly 400 beats an `IntegrityError` 500.
    """
    stmt = (
        select(TaxonomyNode.id)
        .where(TaxonomyNode.parent_id == parent_id)
        .where(TaxonomyNode.name == name)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyNode.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a sub-category with that name already exists under this parent",
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


def _next_child_sort_order(db: Session, parent_id: int) -> int:
    """Default sort_order for a new sub-cat under ``parent_id``."""
    stmt = select(func.max(TaxonomyNode.sort_order)).where(
        TaxonomyNode.parent_id == parent_id
    )
    current_max = db.execute(stmt).scalar()
    if current_max is None:
        return 0
    return int(current_max) + _SORT_ORDER_STEP


def _get_top_level_parent(db: Session, parent_id: int) -> TaxonomyNode:
    """Load a parent node, requiring it to be top-level (depth-limit guard).

    Returns the row; 404s if the id doesn't exist or 400s if the candidate
    parent is itself a sub-category. The depth limit is enforced here rather
    than at the DB layer because it's a per-application invariant (a future
    deeper hierarchy would just relax this check).
    """
    parent = db.get(TaxonomyNode, parent_id)
    if parent is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="category not found"
        )
    if parent.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="taxonomy is two levels deep — sub-categories cannot have children",
        )
    return parent


def _get_sub_category(db: Session, node_id: int) -> TaxonomyNode:
    """Load a sub-category by id; 404 if missing or if it's actually top-level."""
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="sub-category not found",
        )
    return node


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


_TAXONOMY_CSV_HEADERS: list[str] = [
    "id",
    "sort_order",
    "name",
]


def _csv_rows_for_taxonomy(rows: list[TaxonomyNode]) -> list[list[Any]]:
    """Map top-level ``TaxonomyNode`` rows to CSV cell values.

    The cells mirror the HTML table's "Order" + "Name" columns one-for-one.
    ``id`` is added at the front so a downstream consumer can join (the HTML
    carries it as ``data-node-id`` rather than a cell). ``archived_at`` is
    not exposed — the active partition is encoded in the filename. Caller
    is responsible for ensuring the input contains only top-level nodes
    (the route's ``parent_id IS NULL`` filter handles this).
    """
    return [[n.id, n.sort_order, n.name] for n in rows]


@router.get("")
def list_taxonomy(
    request: Request,
    show: str = "active",
    format: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(TaxonomyNode).where(TaxonomyNode.parent_id.is_(None))
    if show == "active":
        stmt = stmt.where(TaxonomyNode.archived_at.is_(None))
    else:
        stmt = stmt.where(TaxonomyNode.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, TaxonomyNode.sort_order, TaxonomyNode.name)

    rows = list(db.execute(stmt).scalars().all())

    if (
        resp := csv_branch(
            format,
            filename=f"taxonomy_{show}.csv",
            headers=_TAXONOMY_CSV_HEADERS,
            rows=_csv_rows_for_taxonomy(rows),
        )
    ) is not None:
        return resp

    # A top-level node is a leaf (and therefore eligible to own field defs) iff
    # it has no active children. The template uses this to decide whether the
    # per-row "Fields" link renders or a "manage on sub-cats instead" hint
    # appears. Computed here so the template stays logic-light.
    leaf_ids: set[int] = set()
    if rows:
        active_parent_ids = set(
            db.execute(
                select(TaxonomyNode.parent_id)
                .where(TaxonomyNode.parent_id.in_([n.id for n in rows]))
                .where(TaxonomyNode.archived_at.is_(None))
            )
            .scalars()
            .all()
        )
        leaf_ids = {n.id for n in rows if n.id not in active_parent_ids}
    return templates.TemplateResponse(
        request,
        "taxonomy_list.html",
        {
            "current_user": _user,
            "nodes": rows,
            "show": show,
            "leaf_ids": leaf_ids,
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


# ===========================================================================
# Sub-category routes (S4)
# ===========================================================================
#
# Sub-categories live under a parent (top-level) node. The depth limit (max two
# levels) is enforced in ``_get_top_level_parent``. Active and archived sub-cats
# share the same name namespace per parent (``uq_taxonomy_child_name``).


# ---------------------------------------------------------------------------
# List view (sub-cats under a parent)
# ---------------------------------------------------------------------------


@router.get("/{parent_id}/children", response_class=HTMLResponse)
def list_sub_categories(
    request: Request,
    parent_id: int,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    parent = _get_top_level_parent(db, parent_id)

    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(TaxonomyNode).where(TaxonomyNode.parent_id == parent.id)
    if show == "active":
        stmt = stmt.where(TaxonomyNode.archived_at.is_(None))
    else:
        stmt = stmt.where(TaxonomyNode.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, TaxonomyNode.sort_order, TaxonomyNode.name)

    rows = list(db.execute(stmt).scalars().all())
    return templates.TemplateResponse(
        request,
        "taxonomy_children_list.html",
        {
            "current_user": _user,
            "parent": parent,
            "nodes": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create (sub-cat under a parent)
# ---------------------------------------------------------------------------


@router.get("/{parent_id}/children/new", response_class=HTMLResponse)
def new_sub_category_form(
    request: Request,
    parent_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    parent = _get_top_level_parent(db, parent_id)
    # An archived parent is a structural dead-end. Hiding the link in the
    # template isn't enough — a hostile/buggy client could still GET this
    # route by URL. 400 here keeps the contract honest.
    if parent.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add sub-categories under an archived category",
        )
    # S5 leaf invariant: a top-level node with active field defs is the leaf
    # — adding a sub-cat would un-leaf it and orphan the schema. Manager has
    # to archive the field defs first.
    if has_active_field_defs(db, parent.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add sub-categories: this category has custom fields. "
                "Archive the fields first, then add sub-categories."
            ),
        )
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": None,
            "parent": parent,
            "form": {"name": "", "sort_order": ""},
            "title": f"New sub-category under {parent.name}",
            "action": f"/admin/taxonomy/{parent.id}/children",
            "back_url": f"/admin/taxonomy/{parent.id}/children",
        },
    )


@router.post("/{parent_id}/children")
def create_sub_category(
    request: Request,
    parent_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    parent = _get_top_level_parent(db, parent_id)
    if parent.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add sub-categories under an archived category",
        )
    # S5 leaf invariant — same as the form GET above.
    if has_active_field_defs(db, parent.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add sub-categories: this category has custom fields. "
                "Archive the fields first, then add sub-categories."
            ),
        )

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_child_name_unique(db, parent_id=parent.id, name=fields["name"])
    if fields["sort_order"] is None:
        fields["sort_order"] = _next_child_sort_order(db, parent.id)

    node = TaxonomyNode(
        parent_id=parent.id,
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
            "parent_id": parent.id,
        },
    )
    db.commit()
    _flash(request, f"Sub-category “{node.name}” created.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{parent.id}/children",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Edit / update (sub-cat by id)
# ---------------------------------------------------------------------------


@router.get("/sub/{node_id}/edit", response_class=HTMLResponse)
def edit_sub_category_form(
    request: Request,
    node_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    node = _get_sub_category(db, node_id)
    parent = db.get(TaxonomyNode, node.parent_id)
    # ``parent`` cannot be None given the FK + the sub-cat existence guarantees
    # — assert for the type checker rather than dressing it as runtime noise.
    assert parent is not None
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": node,
            "parent": parent,
            "form": {
                "name": node.name,
                "sort_order": str(node.sort_order),
            },
            "title": f"Edit {node.name}",
            "action": f"/admin/taxonomy/sub/{node.id}",
            "back_url": f"/admin/taxonomy/{parent.id}/children",
        },
    )


@router.post("/sub/{node_id}")
def update_sub_category(
    request: Request,
    node_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = _get_sub_category(db, node_id)
    assert node.parent_id is not None  # narrowed by _get_sub_category

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_child_name_unique(
        db, parent_id=node.parent_id, name=fields["name"], exclude_id=node.id
    )
    if fields["sort_order"] is None:
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
        _flash(request, f"Sub-category “{node.name}” updated.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{node.parent_id}/children",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (sub-cat by id)
# ---------------------------------------------------------------------------


@router.post("/sub/{node_id}/archive")
def archive_sub_category(
    request: Request,
    node_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = _get_sub_category(db, node_id)
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
        _flash(request, f"Sub-category “{node.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{node.parent_id}/children",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/sub/{node_id}/unarchive")
def unarchive_sub_category(
    request: Request,
    node_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = _get_sub_category(db, node_id)
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
        _flash(request, f"Sub-category “{node.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{node.parent_id}/children",
        status_code=status.HTTP_303_SEE_OTHER,
    )
