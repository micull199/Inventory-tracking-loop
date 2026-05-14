"""Manager-owned visibility-selector for catalog fields (S5, post-0024).

Items have a fixed set of standard fields (every catalog entry maps to a
column on ``items``). This module owns the per-category *picks* — which
catalog fields show on the items form, list, and CSV for items in that
category. Picks inherit downward: a key picked on a top-level category is
visible on every descendant.

Sibling-level collisions (two different sub-categories under the same
parent both picking ``ring_size``) are allowed — those picks are
independently scoped. Same-tree collisions (a key picked on a node AND any
ancestor/descendant) are rejected; inheritance would surface the key twice.

URL shape:

- ``/admin/taxonomy/{node_id}/fields[?show=…]``       — list picks.
- ``POST /admin/taxonomy/{node_id}/fields/pick``      — pick a catalog field.
- ``POST /admin/taxonomy/fields/{field_id}/archive``   — remove the pick.
- ``POST /admin/taxonomy/fields/{field_id}/unarchive`` — no-op, kept for
  RBAC table stability; 400s with a "no longer supported" message.

The router is mounted at the same prefix as ``app.taxonomy.router``
(``/admin/taxonomy``) but kept in a separate module so neither file passes
the ~750-line readability threshold.

Access: ``Manager`` and ``Admin``. Workshop and Office both 403.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.field_catalog import CATALOG_BY_KEY, FIELD_CATALOG, CatalogEntry
from app.models import Role, TaxonomyFieldDef, TaxonomyNode, User
from app.template_env import templates

router = APIRouter(prefix="/admin/taxonomy", tags=["taxonomy_field_defs"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SORT_ORDER_STEP = 10


def _check_key_unique(
    db: Session, *, node_id: int, key: str, exclude_id: int | None = None
) -> None:
    stmt = (
        select(TaxonomyFieldDef.id)
        .where(TaxonomyFieldDef.node_id == node_id)
        .where(TaxonomyFieldDef.key == key)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyFieldDef.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="this field is already picked on this category",
        )


def _ancestor_ids(db: Session, node: TaxonomyNode) -> list[int]:
    """Walk ``parent_id`` upward from ``node`` (exclusive). Top of chain first."""
    chain: list[int] = []
    cursor: TaxonomyNode | None = node
    seen: set[int] = set()
    if cursor is not None and cursor.parent_id is not None:
        cursor = db.get(TaxonomyNode, cursor.parent_id)
        while cursor is not None and cursor.id not in seen:
            chain.append(cursor.id)
            seen.add(cursor.id)
            if cursor.parent_id is None:
                break
            cursor = db.get(TaxonomyNode, cursor.parent_id)
    return chain


def _descendant_ids(db: Session, node_id: int) -> list[int]:
    """Two-level descendant collection (matches the taxonomy depth limit).

    Excludes ``node_id`` itself.
    """
    children = list(
        db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id == node_id))
        .scalars()
        .all()
    )
    grandchildren: list[int] = []
    if children:
        grandchildren = list(
            db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id.in_(children)))
            .scalars()
            .all()
        )
    return [*children, *grandchildren]


def _check_key_unique_in_tree(
    db: Session, *, node: TaxonomyNode, key: str, exclude_id: int | None = None
) -> None:
    """Reject ``key`` if any ancestor or descendant of ``node`` already
    picks it. Sibling-level overlap (two siblings under the same parent
    picking the same key) is allowed — those picks are independently scoped.
    """
    others = set(_ancestor_ids(db, node)) | set(_descendant_ids(db, node.id))
    if not others:
        return
    stmt = (
        select(TaxonomyFieldDef.id, TaxonomyFieldDef.node_id)
        .where(TaxonomyFieldDef.node_id.in_(others))
        .where(TaxonomyFieldDef.key == key)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyFieldDef.id != exclude_id)
    row = db.execute(stmt).first()
    if row is None:
        return
    other_node = db.get(TaxonomyNode, row.node_id)
    other_name = other_node.name if other_node is not None else f"node {row.node_id}"
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            f"field {key!r} is already picked on “{other_name}” in the same "
            "category tree. Picks must be unique across a category and its "
            "descendants (inheritance would surface it twice)."
        ),
    )


def _picked_catalog_keys_in_tree(db: Session, node: TaxonomyNode) -> set[str]:
    """Catalog keys already picked on ``node``, its ancestors, or descendants."""
    tree_ids = {node.id, *_ancestor_ids(db, node), *_descendant_ids(db, node.id)}
    stmt = select(TaxonomyFieldDef.key).where(TaxonomyFieldDef.node_id.in_(tree_ids))
    return {row for row in db.execute(stmt).scalars().all() if row}


def _available_catalog_entries(db: Session, node: TaxonomyNode) -> list[CatalogEntry]:
    """Catalog entries the manager can still pick on ``node``."""
    picked = _picked_catalog_keys_in_tree(db, node)
    return [e for e in FIELD_CATALOG if e.key not in picked]


def _next_sort_order(db: Session, node_id: int) -> int:
    stmt = select(func.max(TaxonomyFieldDef.sort_order)).where(TaxonomyFieldDef.node_id == node_id)
    current_max = db.execute(stmt).scalar()
    if current_max is None:
        return 0
    return int(current_max) + _SORT_ORDER_STEP


def _has_active_children(db: Session, node_id: int) -> bool:
    stmt = (
        select(TaxonomyNode.id)
        .where(TaxonomyNode.parent_id == node_id)
        .where(TaxonomyNode.archived_at.is_(None))
    )
    return db.execute(stmt).first() is not None


def _is_leaf(db: Session, node: TaxonomyNode) -> bool:
    """Sub-cats are always leaves; top-level nodes are leaves iff no active children."""
    if node.parent_id is not None:
        return True
    return not _has_active_children(db, node.id)


def has_active_field_defs(db: Session, node_id: int) -> bool:
    """Public helper used by ``app.taxonomy`` to gate sub-cat creation."""
    stmt = select(TaxonomyFieldDef.id).where(TaxonomyFieldDef.node_id == node_id)
    return db.execute(stmt).first() is not None


def _get_node(db: Session, node_id: int) -> TaxonomyNode:
    node = db.get(TaxonomyNode, node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="taxonomy node not found",
        )
    return node


def _get_field_def(db: Session, field_id: int) -> TaxonomyFieldDef:
    field = db.get(TaxonomyFieldDef, field_id)
    if field is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="field not found")
    return field


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _children_back_url(node: TaxonomyNode) -> str:
    if node.parent_id is None:
        return "/admin/taxonomy"
    return f"/admin/taxonomy/{node.parent_id}/children"


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


@router.get("/{node_id}/fields", response_class=HTMLResponse)
def list_field_defs(
    request: Request,
    node_id: int,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    node = _get_node(db, node_id)

    if show not in {"active", "archived"}:
        show = "active"
    # Picks are hard-delete now (no archived_at). The ``show=archived`` query
    # param is retained for URL stability but always renders an empty list.
    if show == "archived":
        rows: list[TaxonomyFieldDef] = []
    else:
        rows = list(
            db.execute(
                select(TaxonomyFieldDef)
                .where(TaxonomyFieldDef.node_id == node.id)
                .order_by(TaxonomyFieldDef.sort_order, TaxonomyFieldDef.key)
            )
            .scalars()
            .all()
        )

    parent: TaxonomyNode | None = None
    if node.parent_id is not None:
        parent = db.get(TaxonomyNode, node.parent_id)

    inherited_groups: list[dict[str, object]] = []
    if show == "active":
        for ancestor_id in _ancestor_ids(db, node):
            ancestor = db.get(TaxonomyNode, ancestor_id)
            if ancestor is None:
                continue
            ancestor_fields = list(
                db.execute(
                    select(TaxonomyFieldDef)
                    .where(TaxonomyFieldDef.node_id == ancestor.id)
                    .order_by(TaxonomyFieldDef.sort_order, TaxonomyFieldDef.key)
                )
                .scalars()
                .all()
            )
            if ancestor_fields:
                inherited_groups.append({"node": ancestor, "fields": ancestor_fields})

    return templates.TemplateResponse(
        request,
        "taxonomy_field_defs_list.html",
        {
            "current_user": _user,
            "node": node,
            "parent": parent,
            "fields": rows,
            "inherited_groups": inherited_groups,
            "show": show,
            "is_leaf": _is_leaf(db, node),
            "back_url": _children_back_url(node),
            "available_catalog_entries": _available_catalog_entries(db, node),
            "catalog_by_key": CATALOG_BY_KEY,
        },
    )


# ---------------------------------------------------------------------------
# Pick from catalog
# ---------------------------------------------------------------------------


@router.post("/{node_id}/fields/pick")
def pick_field_def_from_catalog(
    request: Request,
    node_id: int,
    catalog_key: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    """Pick a catalog field to show on this category's items."""

    node = _get_node(db, node_id)
    if node.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add fields under an archived node",
        )

    entry = CATALOG_BY_KEY.get(catalog_key.strip())
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown catalog field {catalog_key!r}",
        )

    _check_key_unique(db, node_id=node.id, key=entry.key)
    _check_key_unique_in_tree(db, node=node, key=entry.key)

    field = TaxonomyFieldDef(
        node_id=node.id,
        key=entry.key,
        required=False,
        sort_order=_next_sort_order(db, node.id),
    )
    db.add(field)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="taxonomy_field_def.picked_from_catalog",
        entity_type="taxonomy_field_def",
        entity_id=field.id,
        before=None,
        after={
            "node_id": node.id,
            "key": entry.key,
            "label": entry.label,
            "type": entry.type.value,
        },
    )
    db.commit()
    _flash(request, f"Field “{entry.label}” added.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Remove pick (hard-delete; replaces the archive/unarchive pair)
# ---------------------------------------------------------------------------


@router.post("/fields/{field_id}/archive")
def archive_field_def(
    request: Request,
    field_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    """Remove a pick. Path name kept for RBAC table stability — the action
    is now a hard-delete since the slim TaxonomyFieldDef has no
    ``archived_at`` column. Audited as ``taxonomy_field_def.removed``.
    """
    field = _get_field_def(db, field_id)
    node_id = field.node_id
    entry = CATALOG_BY_KEY.get(field.key)
    label = entry.label if entry else field.key
    record_audit(
        db,
        actor=user,
        action="taxonomy_field_def.removed",
        entity_type="taxonomy_field_def",
        entity_id=field.id,
        before={"node_id": node_id, "key": field.key},
        after=None,
    )
    db.delete(field)
    db.commit()
    _flash(request, f"Field “{label}” removed.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{node_id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/fields/{field_id}/unarchive")
def unarchive_field_def(
    request: Request,
    field_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    """No-op — picks are hard-deleted now. Returns 400 with explanation.

    Path retained for RBAC sweep stability. ``record_audit`` is intentionally
    *not* called because no state change occurs; the route is exempt from
    the audit-coverage sweep via ``_EXEMPT_FROM_AUDIT_WRITE``.
    """
    _ = _get_field_def(db, field_id)
    _ = user  # explicit gate via the dependency; no state to audit.
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "Picks are now hard-deleted on remove (no archive/unarchive). "
            "Re-pick the field from the catalog if you want it back."
        ),
    )
