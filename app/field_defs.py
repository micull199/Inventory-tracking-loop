"""Manager-owned custom-field-def CRUD on any taxonomy node (S5).

Items inherit field defs from their leaf node *and every ancestor* — defining
a field on a top-level category propagates to every sub-category under it.
This module owns the per-node CRUD; the inheritance walk lives in
``app.items._get_active_field_defs``.

Field keys must be unique across a node and all its ancestors / descendants.
Sibling-level collisions (two different sub-categories under the same parent
both defining ``"colour"``) are allowed — those keys are independently scoped.

URL shape (matches S4 sub-cat conventions: parent-scoped for list/create,
flat-by-id for the rest):

- ``/admin/taxonomy/{node_id}/fields[?show=…]``       — list (active/archived).
- ``/admin/taxonomy/{node_id}/fields/new``             — form.
- ``POST /admin/taxonomy/{node_id}/fields``            — create.
- ``/admin/taxonomy/fields/{field_id}/edit``           — edit form.
- ``POST /admin/taxonomy/fields/{field_id}``           — update.
- ``POST /admin/taxonomy/fields/{field_id}/archive``   — archive.
- ``POST /admin/taxonomy/fields/{field_id}/unarchive`` — unarchive.

The router is mounted at the same prefix as ``app.taxonomy.router``
(``/admin/taxonomy``) but kept in a separate module so neither file passes the
~750-line readability threshold.

Access: ``Manager`` and ``Admin``. Workshop and Office both 403 — Office is a
sibling role, not a subset, per MISSION §3 ("Office cannot manage the taxonomy").
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
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


def _check_name_unique(
    db: Session, *, node_id: int, name: str, exclude_id: int | None = None
) -> None:
    stmt = (
        select(TaxonomyFieldDef.id)
        .where(TaxonomyFieldDef.node_id == node_id)
        .where(TaxonomyFieldDef.name == name)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyFieldDef.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a field with that name already exists on this node",
        )


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
            detail=("a field whose key collides with this name already exists on this node"),
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

    Excludes ``node_id`` itself. Includes both active and archived descendants
    — archived sub-cats can still host field defs whose keys would clash on
    unarchive.
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
    """Reject ``key`` if it collides with any active field def on an ancestor
    or descendant of ``node``.

    Field defs inherit downward through ancestors (see
    ``app.items._get_active_field_defs``), so the same key appearing at two
    different levels would surface twice on a leaf's item form — ambiguous and
    bug-prone. Sibling-level collisions (two different sub-categories under the
    same parent both defining ``"colour"``) are fine: those keys are
    independently scoped.
    """
    others = set(_ancestor_ids(db, node)) | set(_descendant_ids(db, node.id))
    if not others:
        return
    stmt = (
        select(TaxonomyFieldDef.id, TaxonomyFieldDef.node_id)
        .where(TaxonomyFieldDef.node_id.in_(others))
        .where(TaxonomyFieldDef.key == key)
        .where(TaxonomyFieldDef.archived_at.is_(None))
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
            f"field key {key!r} already exists on “{other_name}” in the same "
            "category tree. Field keys must be unique across a category and its "
            "descendants (inheritance would surface it twice)."
        ),
    )


def _picked_catalog_keys_in_tree(db: Session, node: TaxonomyNode) -> set[str]:
    """Catalog keys already picked on ``node``, its ancestors, or descendants.

    Used to filter the picker dropdown so users can't pick the same catalog
    entry twice in one inheritance chain. Includes active rows only —
    archived picks are free to be re-picked. Sibling collisions are
    independently scoped and not surfaced here.
    """
    tree_ids = {node.id, *_ancestor_ids(db, node), *_descendant_ids(db, node.id)}
    stmt = (
        select(TaxonomyFieldDef.catalog_key)
        .where(TaxonomyFieldDef.node_id.in_(tree_ids))
        .where(TaxonomyFieldDef.archived_at.is_(None))
        .where(TaxonomyFieldDef.catalog_key.is_not(None))
    )
    return {row for row in db.execute(stmt).scalars().all() if row}


def _available_catalog_entries(db: Session, node: TaxonomyNode) -> list[CatalogEntry]:
    """Catalog entries the manager can still pick on ``node``.

    Order: catalog declaration order. The catalog's ``sort_order`` is the
    field's *display* order on items (used by templates); the picker shows
    entries in their declared order so related fields stay grouped.
    """
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
    stmt = (
        select(TaxonomyFieldDef.id)
        .where(TaxonomyFieldDef.node_id == node_id)
        .where(TaxonomyFieldDef.archived_at.is_(None))
    )
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
    """Where the breadcrumb back-link points: parent's children list, or the
    top-level taxonomy index for a top-level node."""
    if node.parent_id is None:
        return "/admin/taxonomy"
    return f"/admin/taxonomy/{node.parent_id}/children"


_LIST_ORDER = case((TaxonomyFieldDef.archived_at.is_(None), 0), else_=1)


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

    stmt = select(TaxonomyFieldDef).where(TaxonomyFieldDef.node_id == node.id)
    if show == "active":
        stmt = stmt.where(TaxonomyFieldDef.archived_at.is_(None))
    else:
        stmt = stmt.where(TaxonomyFieldDef.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, TaxonomyFieldDef.sort_order, TaxonomyFieldDef.name)

    rows = list(db.execute(stmt).scalars().all())

    parent: TaxonomyNode | None = None
    if node.parent_id is not None:
        parent = db.get(TaxonomyNode, node.parent_id)

    # Inherited groups — active field defs on each ancestor, grouped by the
    # owning node so the template can render "Inherited from <name>" blocks.
    # Always shown on the active tab; the archived tab is for this node's own
    # archived defs only.
    inherited_groups: list[dict[str, object]] = []
    if show == "active":
        ancestor_ids = _ancestor_ids(db, node)
        for ancestor_id in ancestor_ids:
            ancestor = db.get(TaxonomyNode, ancestor_id)
            if ancestor is None:
                continue
            ancestor_fields = list(
                db.execute(
                    select(TaxonomyFieldDef)
                    .where(TaxonomyFieldDef.node_id == ancestor.id)
                    .where(TaxonomyFieldDef.archived_at.is_(None))
                    .order_by(TaxonomyFieldDef.sort_order, TaxonomyFieldDef.name)
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
# Pick from catalog (catalog-driven schema)
# ---------------------------------------------------------------------------


@router.post("/{node_id}/fields/pick")
def pick_field_def_from_catalog(
    request: Request,
    node_id: int,
    catalog_key: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    """Materialise a ``TaxonomyFieldDef`` row from a catalog entry.

    Name, type, options, and key all come from the catalog — the manager
    does not type them. Same tree-uniqueness rule as the free-text create
    route (`_check_key_unique_in_tree`): a catalog entry cannot be picked
    twice in one ancestor-descendant chain, but sibling sub-categories
    can independently pick the same entry.
    """

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

    # Same-node uniqueness on both ``name`` and ``key`` (across active +
    # archived rows). Tree uniqueness on the catalog key for active rows.
    _check_name_unique(db, node_id=node.id, name=entry.label)
    _check_key_unique(db, node_id=node.id, key=entry.key)
    _check_key_unique_in_tree(db, node=node, key=entry.key)

    field = TaxonomyFieldDef(
        node_id=node.id,
        name=entry.label,
        key=entry.key,
        catalog_key=entry.key,
        type=entry.type,
        options_json=list(entry.options) if entry.options else None,
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
            "catalog_key": entry.key,
            "key": field.key,
            "label": entry.label,
            "type": entry.type.value,
            "storage": entry.storage,
        },
    )
    db.commit()
    _flash(request, f"Field “{entry.label}” added from the catalog.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Archive / unarchive
# ---------------------------------------------------------------------------


@router.post("/fields/{field_id}/archive")
def archive_field_def(
    request: Request,
    field_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    field = _get_field_def(db, field_id)
    if field.archived_at is None:
        field.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="taxonomy_field_def.archived",
            entity_type="taxonomy_field_def",
            entity_id=field.id,
            before={"archived_at": None},
            after={"archived_at": field.archived_at},
        )
        db.commit()
        _flash(request, f"Field “{field.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{field.node_id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/fields/{field_id}/unarchive")
def unarchive_field_def(
    request: Request,
    field_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    field = _get_field_def(db, field_id)

    if field.archived_at is not None:
        # Node-archive guard stays — an archived node is structurally inert
        # and shouldn't accept a re-activated field. The legacy "leaf only"
        # gate is dropped: field defs can now live on any node and inherit
        # downward, so unarchiving onto a parent-of-children is legitimate.
        # The tree-wide key uniqueness is re-checked so a sibling field
        # added at a different level while this one was archived doesn't
        # silently clash on resurrection.
        node = db.get(TaxonomyNode, field.node_id)
        assert node is not None
        if node.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot unarchive a field on an archived node",
            )
        _check_key_unique_in_tree(db, node=node, key=field.key, exclude_id=field.id)

        previous = field.archived_at
        field.archived_at = None
        record_audit(
            db,
            actor=user,
            action="taxonomy_field_def.unarchived",
            entity_type="taxonomy_field_def",
            entity_id=field.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Field “{field.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{field.node_id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
    )
