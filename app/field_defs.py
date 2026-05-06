"""Manager-owned custom-field-def CRUD on a taxonomy *leaf* node (S5).

Items will inherit the field schema of their leaf node (I1+I2). For v1 (per
MISSION §3) a "leaf" is either a top-level node with no active sub-categories,
or any sub-category. Field defs only attach to leaves; the routes here enforce
that invariant on create and on unarchive.

URL shape (matches S4 sub-cat conventions: parent-scoped for list/create,
flat-by-id for the rest):

- ``/admin/taxonomy/{node_id}/fields[?show=…]``       — list (active/archived).
- ``/admin/taxonomy/{node_id}/fields/new``             — form (only if leaf).
- ``POST /admin/taxonomy/{node_id}/fields``            — create.
- ``/admin/taxonomy/fields/{field_id}/edit``           — edit form.
- ``POST /admin/taxonomy/fields/{field_id}``           — update.
- ``POST /admin/taxonomy/fields/{field_id}/archive``   — archive.
- ``POST /admin/taxonomy/fields/{field_id}/unarchive`` — unarchive (leaf re-checked).

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
_OPTIONS_TYPES: frozenset[FieldType] = frozenset(
    {FieldType.SELECT, FieldType.MULTISELECT}
)


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
                detail=(
                    "options are only valid for select / multiselect fields"
                ),
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
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
            detail=(
                "a field whose key collides with this name already exists on this node"
            ),
        )


def _next_sort_order(db: Session, node_id: int) -> int:
    stmt = select(func.max(TaxonomyFieldDef.sort_order)).where(
        TaxonomyFieldDef.node_id == node_id
    )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="field not found"
        )
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
    stmt = stmt.order_by(
        _LIST_ORDER, TaxonomyFieldDef.sort_order, TaxonomyFieldDef.name
    )

    rows = list(db.execute(stmt).scalars().all())

    parent: TaxonomyNode | None = None
    if node.parent_id is not None:
        parent = db.get(TaxonomyNode, node.parent_id)

    return templates.TemplateResponse(
        request,
        "taxonomy_field_defs_list.html",
        {
            "current_user": _user,
            "node": node,
            "parent": parent,
            "fields": rows,
            "show": show,
            "is_leaf": _is_leaf(db, node),
            "back_url": _children_back_url(node),
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
    if not _is_leaf(db, node):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "fields can only be added to a leaf node — this node has "
                "sub-categories. Add fields to those sub-categories instead."
            ),
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
    if not _is_leaf(db, node):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "fields can only be added to a leaf node — this node has "
                "sub-categories"
            ),
        )

    fields = _normalise(name, type, options_text, required, sort_order)
    _validate_name(fields["name"])
    key = _derive_key(fields["name"])

    _check_name_unique(db, node_id=node.id, name=fields["name"])
    _check_key_unique(db, node_id=node.id, key=key)

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

    options_text = (
        "\n".join(field.options_json) if field.options_json is not None else ""
    )

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

    _check_name_unique(
        db, node_id=field.node_id, name=fields["name"], exclude_id=field.id
    )
    _check_key_unique(
        db, node_id=field.node_id, key=new_key, exclude_id=field.id
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
        # The leaf invariant has to hold on unarchive too — without this, a
        # def archived back when the node was a leaf could be silently
        # resurrected after the node grew sub-categories, leaving a dangling
        # def on a non-leaf. Symmetric with the create-time check.
        node = db.get(TaxonomyNode, field.node_id)
        assert node is not None
        if node.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="cannot unarchive a field on an archived node",
            )
        if not _is_leaf(db, node):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "cannot unarchive a field on a node that has sub-categories"
                ),
            )

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
