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

import re
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.field_catalog import CATALOG_BY_KEY, FIELD_CATALOG, CatalogEntry
from app.field_visibility import (
    BUILT_IN_FIELDS,
    VISIBILITY_STATES,
    effective_field_visibility,
    validate_visibility_submission,
)
from app.models import FieldType, Role, TaxonomyFieldDef, TaxonomyNode, User
from app.template_env import templates

router = APIRouter(prefix="/admin/taxonomy", tags=["taxonomy_field_defs"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Audit-diff vocabulary for updates. ``options_json`` is included so changes to
# select/multiselect option lists are recorded; ``key`` is intentionally NOT
# editable (see ``_derive_key``), so it never appears in a diff for an update.
_FIELDS: tuple[str, ...] = (
    "name",
    "type",
    "options_json",
    "required",
    "sort_order",
)

_SORT_ORDER_STEP = 10

# All eight values are "select" + "multiselect" only; everything else is
# treated as a non-options type.
_OPTIONS_TYPES: frozenset[FieldType] = frozenset({FieldType.SELECT, FieldType.MULTISELECT})


def _derive_key(name: str) -> str:
    """Derive a stable, lowercase, alphanumeric/underscore key from ``name``.

    The key is what items reference once they store values; it must not change
    when a manager renames the field. Empty after derive (e.g. name is all
    punctuation) → 400.
    """
    key = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    if not key:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="name must contain at least one letter or digit",
        )
    return key


def _parse_options(raw: str) -> list[str]:
    """Parse a textarea blob into a clean ``list[str]`` of options.

    One option per line. Whitespace stripped, empty lines dropped, duplicates
    rejected (after strip). Empty result → caller decides whether that's OK
    (it's an error for select/multiselect, fine for everything else).
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in (raw or "").splitlines():
        opt = line.strip()
        if not opt:
            continue
        if opt in seen:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="options must not contain duplicates",
            )
        seen.add(opt)
        out.append(opt)
    return out


def _coerce_type(raw: str) -> FieldType:
    try:
        return FieldType(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="unknown field type",
        ) from exc


def _normalise(
    name: str,
    type_value: str,
    options_text: str,
    required: bool,
    sort_order: str,
) -> dict[str, Any]:
    """Strip + parse form input into the value shape stored on the row."""
    clean_name = (name or "").strip()
    field_type = _coerce_type(type_value)

    parsed_options = _parse_options(options_text)
    if field_type in _OPTIONS_TYPES:
        if not parsed_options:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="select / multiselect fields need at least one option",
            )
        options_value: list[str] | None = parsed_options
    else:
        if parsed_options:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("options are only valid for select / multiselect fields"),
            )
        options_value = None

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

    return {
        "name": clean_name,
        "type": field_type,
        "options_json": options_value,
        "required": bool(required),
        "sort_order": sort_value,
    }


def _validate_name(name: str) -> str:
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    return name


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


def _diff(
    field: TaxonomyFieldDef, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(field, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _children_back_url(node: TaxonomyNode) -> str:
    """Where the breadcrumb back-link points: parent's children list, or the
    top-level taxonomy index for a top-level node."""
    if node.parent_id is None:
        return "/admin/taxonomy"
    return f"/admin/taxonomy/{node.parent_id}/children"


def _form_back_url(node: TaxonomyNode) -> str:
    return f"/admin/taxonomy/{node.id}/fields"


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
            "built_in_fields": BUILT_IN_FIELDS,
            "visibility_states": VISIBILITY_STATES,
            "field_visibility": effective_field_visibility(node),
            "available_catalog_entries": _available_catalog_entries(db, node),
            "catalog_by_key": CATALOG_BY_KEY,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/{node_id}/fields/new", response_class=HTMLResponse)
def new_field_def_form(
    request: Request,
    node_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    node = _get_node(db, node_id)
    if node.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add fields under an archived node",
        )

    parent: TaxonomyNode | None = None
    if node.parent_id is not None:
        parent = db.get(TaxonomyNode, node.parent_id)

    return templates.TemplateResponse(
        request,
        "taxonomy_field_def_form.html",
        {
            "current_user": _user,
            "node": node,
            "parent": parent,
            "field": None,
            "form": {
                "name": "",
                "type": FieldType.TEXT.value,
                "options_text": "",
                "required": False,
                "sort_order": "",
            },
            "field_types": [t.value for t in FieldType],
            "show_options": False,  # default type is TEXT — no options
            "title": f"New field on {node.name}",
            "action": f"/admin/taxonomy/{node.id}/fields",
            "back_url": _form_back_url(node),
        },
    )


@router.post("/{node_id}/fields")
def create_field_def(
    request: Request,
    node_id: int,
    name: str = Form(""),
    type: str = Form(""),
    options_text: str = Form(""),
    required: bool = Form(False),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = _get_node(db, node_id)
    if node.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add fields under an archived node",
        )

    fields = _normalise(name, type, options_text, required, sort_order)
    _validate_name(fields["name"])
    key = _derive_key(fields["name"])

    _check_name_unique(db, node_id=node.id, name=fields["name"])
    _check_key_unique(db, node_id=node.id, key=key)
    _check_key_unique_in_tree(db, node=node, key=key)

    if fields["sort_order"] is None:
        fields["sort_order"] = _next_sort_order(db, node.id)

    field = TaxonomyFieldDef(
        node_id=node.id,
        name=fields["name"],
        key=key,
        type=fields["type"],
        options_json=fields["options_json"],
        required=fields["required"],
        sort_order=fields["sort_order"],
    )
    db.add(field)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="taxonomy_field_def.created",
        entity_type="taxonomy_field_def",
        entity_id=field.id,
        before=None,
        after={
            "node_id": node.id,
            "name": field.name,
            "key": field.key,
            "type": field.type,
            "options_json": field.options_json,
            "required": field.required,
            "sort_order": field.sort_order,
        },
    )
    db.commit()
    _flash(request, f"Field “{field.name}” created.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
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
# HTMX fragment: options textarea visibility per field type
# ---------------------------------------------------------------------------


@router.get("/fields/_options-partial", response_class=HTMLResponse)
def options_partial(
    request: Request,
    type: str = "",
    options_text: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    """HTMX fragment: options textarea, shown only for select / multiselect.

    Wired to the field-def form's type ``<select>`` via ``hx-get`` /
    ``hx-trigger="change"``. Returns a non-empty body (the options ``<p>``)
    when the user picks select or multiselect; an empty body otherwise so
    the textarea disappears from the form.

    ``options_text`` round-trips any text the user already typed before
    flipping types; if the user picks a non-select type, the textarea
    vanishes and the value is dropped (server-side validation would 400 on
    submit anyway). Manager-only — same gate as the form itself.
    """
    show_options = type in {t.value for t in _OPTIONS_TYPES}
    return templates.TemplateResponse(
        request,
        "taxonomy_field_def_options_partial.html",
        {
            "show_options": show_options,
            "form": {"options_text": options_text},
        },
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/fields/{field_id}/edit", response_class=HTMLResponse)
def edit_field_def_form(
    request: Request,
    field_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    field = _get_field_def(db, field_id)
    node = db.get(TaxonomyNode, field.node_id)
    # FK guarantees node exists; assertion narrows the type for mypy.
    assert node is not None
    parent: TaxonomyNode | None = None
    if node.parent_id is not None:
        parent = db.get(TaxonomyNode, node.parent_id)

    options_text = "\n".join(field.options_json) if field.options_json is not None else ""

    return templates.TemplateResponse(
        request,
        "taxonomy_field_def_form.html",
        {
            "current_user": _user,
            "node": node,
            "parent": parent,
            "field": field,
            "form": {
                "name": field.name,
                "type": field.type.value,
                "options_text": options_text,
                "required": field.required,
                "sort_order": str(field.sort_order),
            },
            "field_types": [t.value for t in FieldType],
            "show_options": field.type in _OPTIONS_TYPES,
            "title": f"Edit {field.name}",
            "action": f"/admin/taxonomy/fields/{field.id}",
            "back_url": _form_back_url(node),
        },
    )


@router.post("/fields/{field_id}")
def update_field_def(
    request: Request,
    field_id: int,
    name: str = Form(""),
    type: str = Form(""),
    options_text: str = Form(""),
    required: bool = Form(False),
    sort_order: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    field = _get_field_def(db, field_id)

    fields = _normalise(name, type, options_text, required, sort_order)
    _validate_name(fields["name"])
    # Key is derived from the *current* name so a rename keeps using the same
    # key whenever the slug doesn't change. ``_derive_key`` raises 400 if the
    # new name has no alphanumeric content.
    new_key = _derive_key(fields["name"])

    _check_name_unique(db, node_id=field.node_id, name=fields["name"], exclude_id=field.id)
    _check_key_unique(db, node_id=field.node_id, key=new_key, exclude_id=field.id)
    field_node = db.get(TaxonomyNode, field.node_id)
    assert field_node is not None  # FK guarantees presence; narrows for mypy
    _check_key_unique_in_tree(
        db, node=field_node, key=new_key, exclude_id=field.id
    )
    if fields["sort_order"] is None:
        fields["sort_order"] = field.sort_order

    diff = _diff(field, fields)
    if diff is not None:
        before, after = diff
        # Name change may shift the slug (key). When that happens, record the
        # key shift in the same audit row alongside the name change. If the
        # rename lands on the same slug (e.g. casing-only change), the key
        # stays put — invisible to the diff, items references unchanged.
        if field.key != new_key:
            before["key"] = field.key
            after["key"] = new_key
            field.key = new_key
        for f in _FIELDS:
            setattr(field, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="taxonomy_field_def.updated",
            entity_type="taxonomy_field_def",
            entity_id=field.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Field “{field.name}” updated.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{field.node_id}/fields",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Built-in field visibility (per leaf)
# ---------------------------------------------------------------------------


@router.post("/{node_id}/fields/visibility")
async def save_field_visibility(
    request: Request,
    node_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    """Persist the built-in field visibility map for ``node_id``.

    Accepts ``visibility_<field>`` form values, one per built-in field. The
    submission is fully replaced — any field omitted from the post collapses
    to the default state. Archived / non-leaf nodes still allow editing so a
    Manager can pre-stage a leaf before items exist.
    """
    node = _get_node(db, node_id)
    form = await request.form()
    raw = {k: v for k, v in form.items() if isinstance(v, str)}
    new_visibility = validate_visibility_submission(raw)

    before = dict(node.field_visibility_json) if node.field_visibility_json else None
    node.field_visibility_json = new_visibility
    record_audit(
        db,
        actor=user,
        action="taxonomy_node.field_visibility_updated",
        entity_type="taxonomy_node",
        entity_id=node.id,
        before={"field_visibility_json": before},
        after={"field_visibility_json": new_visibility},
    )
    db.commit()
    _flash(request, f"Built-in field visibility for “{node.name}” updated.")
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
