"""Manager-owned taxonomy CRUD routes.

The taxonomy is a one-to-three-level hierarchy used to classify items. Each
top-level node carries an ``archetype`` (``bulk`` / ``unique`` /
``unique_variant``) that governs item behaviour throughout the tree; the
archetype is inherited at read time by walking ``parent_id`` up to depth 0
(see ``app/sku.py``). Every node also carries an ``sku_prefix`` (1-8 alnum,
uppercased) that is composed with ancestor prefixes to form item SKUs.

URL shapes (all under ``/admin/taxonomy``):

- Top-level (depth 0): ``/`` (list + new), ``/{id}/edit``, ``/{id}``,
  ``/{id}/archive``, ``/{id}/unarchive``.
- Sub-category (depth 1): ``/{parent_id}/children`` (list + new), and the flat
  ``/sub/{id}/edit``, ``/sub/{id}``, ``/sub/{id}/archive``,
  ``/sub/{id}/unarchive`` for edit / archive / unarchive.
- Sub-sub-category (depth 2): ``/{parent_id}/sub/{sub_id}/grandchildren`` (list
  + new). Edit/archive of a depth-2 node reuses the flat ``/sub/{id}/...``
  shape, which already handles any non-top-level node — the only difference
  between depth 1 and depth 2 is whether ``parent.parent_id`` is null.

Depth limit (max three levels, depth 0..2) is enforced by ``_get_parent_node``:
a candidate parent at depth 2 is rejected with a 400. Container-or-leaf
invariant: a node cannot host children if it already has active items
attached, and the items form rejects a destination node that has active
children (see ``app/items.py``).

Archetype constraints (unique-variant only):
- Depth-2 nodes under a ``unique_variant`` top-level are system-managed.
  The taxonomy admin never lets a manager create depth-2 nodes under that
  archetype; ``app/items.py`` mints them automatically (one per item) via
  ``app.sku.create_unique_variant_leaf``.

Access: ``Manager`` and ``Admin``. Workshop and Office both 403 — Office is a
sibling role, not a subset, per MISSION §3 ("Office cannot manage the taxonomy").
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import (
    Archetype,
    Item,
    Location,
    Role,
    Supplier,
    TaxonomyNode,
    TaxonomyStage,
    TrackingMode,
    User,
)
from app.sku import effective_archetype, node_depth
from app.template_env import templates

router = APIRouter(prefix="/admin/taxonomy", tags=["taxonomy"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Fields tracked in audit diffs. ``parent_id`` is intentionally omitted —
# the routes never let the user change it after create. ``defaults_json`` is
# a dict whose change is captured atomically (the audit row carries the
# whole before/after blob; the dict is small). ``archetype`` and
# ``sku_prefix`` join the vocabulary so the taxonomy-refinement edits show
# up in the audit log.
_FIELDS: tuple[str, ...] = (
    "name",
    "sort_order",
    "defaults_json",
    "archetype",
    "sku_prefix",
)

# Keys recognised inside ``defaults_json``. Matches the items create form's
# field names so the values can be substituted verbatim into the form
# context. Kept narrow on purpose — adding a key here only makes sense if
# the items form learns to consume it.
_DEFAULT_KEYS: tuple[str, ...] = (
    "unit",
    "tracking_mode",
    "requires_checkout",
    "reorder_threshold",
    "reorder_qty",
    "supplier_id",
    "location_id",
)

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


def _coerce_defaults(
    db: Session,
    *,
    default_unit: str,
    default_tracking_mode: str,
    default_requires_checkout: bool,
    default_reorder_threshold: str,
    default_reorder_qty: str,
    default_supplier_id: str,
    default_location_id: str,
) -> dict[str, Any] | None:
    """Validate raw form values for the per-category defaults block.

    Returns a dict containing only the keys the user actually set, or
    ``None`` if every field is blank (which means "no defaults" and stores
    SQL NULL rather than ``{}``). 400s on type / FK / range failures so the
    user gets a clear error rather than a silent drop on save.

    ``default_requires_checkout`` is a checkbox — always present (False when
    unchecked). It only lands in the dict when True so the absence-vs-False
    distinction (defaults silent vs explicit "off") doesn't pollute the
    audit diff.
    """
    out: dict[str, Any] = {}

    unit = (default_unit or "").strip()
    if unit:
        if len(unit) > 32:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default unit too long (max 32 chars)",
            )
        out["unit"] = unit

    tm = (default_tracking_mode or "").strip()
    if tm:
        if tm not in {m.value for m in TrackingMode}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid default tracking_mode",
            )
        out["tracking_mode"] = tm

    if default_requires_checkout:
        out["requires_checkout"] = True

    for key, raw in (
        ("reorder_threshold", default_reorder_threshold),
        ("reorder_qty", default_reorder_qty),
    ):
        v = (raw or "").strip()
        if v:
            try:
                d = Decimal(v)
            except InvalidOperation as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"default {key} must be a number",
                ) from exc
            if d < 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"default {key} cannot be negative",
                )
            # Store as the canonical Decimal string so round-trips don't drift
            # (Decimal("1.0") preserves trailing zero; float would lose it).
            out[key] = str(d)

    sid = (default_supplier_id or "").strip()
    if sid:
        try:
            sid_int = int(sid)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default supplier_id must be an integer",
            ) from exc
        sup = db.get(Supplier, sid_int)
        if sup is None or sup.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default supplier not found or archived",
            )
        out["supplier_id"] = sid_int

    lid = (default_location_id or "").strip()
    if lid:
        try:
            lid_int = int(lid)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default location_id must be an integer",
            ) from exc
        loc = db.get(Location, lid_int)
        if loc is None or loc.archived_at is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="default location not found or archived",
            )
        out["location_id"] = lid_int

    return out if out else None


def _defaults_form_view(
    defaults: dict[str, Any] | None,
) -> dict[str, str | bool]:
    """Render-shape the stored defaults dict for the form template.

    Each key gets a stable str / bool the template can drop into the form
    inputs (``value=`` / ``selected`` / ``checked``). Missing keys render as
    empty strings (or False) so the inputs render blank.
    """
    src = defaults or {}
    return {
        "unit": str(src.get("unit", "")),
        "tracking_mode": str(src.get("tracking_mode", "")),
        "requires_checkout": bool(src.get("requires_checkout", False)),
        "reorder_threshold": str(src.get("reorder_threshold", "")),
        "reorder_qty": str(src.get("reorder_qty", "")),
        "supplier_id": str(src.get("supplier_id", "")),
        "location_id": str(src.get("location_id", "")),
    }


def _supplier_options(db: Session) -> list[dict[str, Any]]:
    """Active suppliers sorted by name, for the defaults `<select>`."""
    rows = (
        db.execute(select(Supplier).where(Supplier.archived_at.is_(None)).order_by(Supplier.name))
        .scalars()
        .all()
    )
    return [{"id": s.id, "label": s.name} for s in rows]


def _location_options(db: Session) -> list[dict[str, Any]]:
    """Active locations sorted by name, for the defaults `<select>`."""
    rows = (
        db.execute(select(Location).where(Location.archived_at.is_(None)).order_by(Location.name))
        .scalars()
        .all()
    )
    return [{"id": loc.id, "label": loc.name} for loc in rows]


def _validate_name(name: str) -> str:
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    return name


def _check_top_name_unique(db: Session, name: str, *, exclude_id: int | None = None) -> None:
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
    stmt = select(func.max(TaxonomyNode.sort_order)).where(TaxonomyNode.parent_id.is_(None))
    current_max = db.execute(stmt).scalar()
    if current_max is None:
        return 0
    return int(current_max) + _SORT_ORDER_STEP


def _next_child_sort_order(db: Session, parent_id: int) -> int:
    """Default sort_order for a new sub-cat under ``parent_id``."""
    stmt = select(func.max(TaxonomyNode.sort_order)).where(TaxonomyNode.parent_id == parent_id)
    current_max = db.execute(stmt).scalar()
    if current_max is None:
        return 0
    return int(current_max) + _SORT_ORDER_STEP


def _get_top_level_parent(db: Session, parent_id: int) -> TaxonomyNode:
    """Load a parent node, requiring it to be top-level (depth-0 only).

    Retained for the depth-1 create route which still wants the parent to be
    a depth-0 node. Returns the row; 404s if the id doesn't exist or 400s if
    the candidate parent is itself a sub-category. The depth-2 create route
    uses ``_get_parent_node`` (which accepts depth 0 or 1).
    """
    parent = db.get(TaxonomyNode, parent_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    if parent.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="parent must be a top-level category",
        )
    return parent


def _get_parent_node(db: Session, parent_id: int) -> TaxonomyNode:
    """Load a candidate parent for a new child node.

    Accepts depth 0 or 1; rejects depth 2 (the depth limit) with a 400.
    Also rejects a parent that already has active items attached — an
    "un-leafing" would orphan those items' SKU paths. Mirrors the existing
    field-defs gate: if the parent has any active field def, the sub-cat
    create form already 400s in the legacy code path.
    """
    parent = db.get(TaxonomyNode, parent_id)
    if parent is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    if node_depth(db, parent) >= 2:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="depth limit reached — categories can be at most 3 levels deep",
        )
    if _has_active_items(db, parent.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add a sub-category here: this category has items "
                "attached. Move or archive them first."
            ),
        )
    return parent


def _has_active_items(db: Session, node_id: int) -> bool:
    """True if any non-archived item references this node id directly."""
    stmt = select(Item.id).where(Item.taxonomy_node_id == node_id).where(Item.archived_at.is_(None))
    return db.execute(stmt).first() is not None


def _has_descendant_items(db: Session, node_id: int) -> bool:
    """True if any active item lives on this node or any descendant.

    Walks down children + grandchildren (max two levels under the given
    node). Used to "lock" archetype + sku_prefix on edit after items exist
    anywhere under the tree.
    """
    if _has_active_items(db, node_id):
        return True
    # Collect descendants up to two levels (depth-2 is the floor). Active
    # only — an archived child is structurally inert. We still check
    # archived items underneath each descendant because the lock guards
    # against silent SKU drift, which an archived item still embodies.
    child_ids = list(
        db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id == node_id)).scalars().all()
    )
    if not child_ids:
        return False
    grandchild_ids = list(
        db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id.in_(child_ids)))
        .scalars()
        .all()
    )
    all_descendant_ids = child_ids + grandchild_ids
    if not all_descendant_ids:
        return False
    stmt = select(Item.id).where(Item.taxonomy_node_id.in_(all_descendant_ids))
    return db.execute(stmt).first() is not None


def _disambiguate_top_prefix(db: Session, base: str) -> str:
    """If ``base`` collides with an existing top-level prefix, append 2, 3, ...

    Mirrors the migration's sibling disambiguation. Capped at the column
    width (8 chars). Used only when a route auto-derives the prefix from the
    node name; callers that explicitly pass a prefix bypass this and 400 on
    a sibling collision instead.
    """
    taken = set(
        db.execute(select(TaxonomyNode.sku_prefix).where(TaxonomyNode.parent_id.is_(None)))
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = str(n)
        allowed = 8 - len(suffix)
        candidate = ((base[:allowed] if allowed > 0 else "") + suffix)[:8]
        if candidate not in taken:
            return candidate
        n += 1


def _disambiguate_child_prefix(db: Session, *, parent_id: int, base: str) -> str:
    """Sibling-scoped equivalent of ``_disambiguate_top_prefix``."""
    taken = set(
        db.execute(select(TaxonomyNode.sku_prefix).where(TaxonomyNode.parent_id == parent_id))
        .scalars()
        .all()
    )
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = str(n)
        allowed = 8 - len(suffix)
        candidate = ((base[:allowed] if allowed > 0 else "") + suffix)[:8]
        if candidate not in taken:
            return candidate
        n += 1


def _check_sku_prefix_unique_top(
    db: Session, prefix: str, *, exclude_id: int | None = None
) -> None:
    """Reject a top-level ``sku_prefix`` already used by another depth-0 node.

    Scoped across active + archived rows so an archived sibling's prefix is
    not silently reusable (its items still carry it). Belt-and-braces over
    the partial unique index ``uq_taxonomy_sku_prefix_top``.
    """
    stmt = (
        select(TaxonomyNode.id)
        .where(TaxonomyNode.parent_id.is_(None))
        .where(TaxonomyNode.sku_prefix == prefix)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyNode.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"another top-level category already uses SKU prefix {prefix!r}"),
        )


def _check_sku_prefix_unique_child(
    db: Session,
    *,
    parent_id: int,
    prefix: str,
    exclude_id: int | None = None,
) -> None:
    """Reject a child ``sku_prefix`` already used by a sibling under ``parent_id``."""
    stmt = (
        select(TaxonomyNode.id)
        .where(TaxonomyNode.parent_id == parent_id)
        .where(TaxonomyNode.sku_prefix == prefix)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyNode.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"another sub-category under this parent already uses SKU prefix {prefix!r}"),
        )


def _validate_archetype(raw: str, *, default: Archetype | None = None) -> Archetype:
    """Coerce an incoming form value to an ``Archetype``.

    Blank input falls back to ``default`` when supplied (the create-top-level
    route uses ``Archetype.BULK`` so legacy clients that don't know about
    archetypes silently land on the conservative quantity-tracked default).
    Otherwise a blank input 400s.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        if default is not None:
            return default
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="archetype is required",
        )
    try:
        return Archetype(cleaned)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("archetype must be one of: unique, bulk, unique_variant"),
        ) from exc


def _normalise_sku_prefix(raw: str, *, default_from_name: str | None = None) -> str:
    """Strip + uppercase + validate a submitted ``sku_prefix``.

    Returns the cleaned value (1-8 uppercase alphanumerics).

    Blank-input handling:
    - If ``default_from_name`` is supplied, derive a default via the same
      rule the model + migration use (first three alpha chars uppercased,
      falling back to alnum, then ``"CAT"``). This keeps the create routes
      forgiving when the legacy form omits the prefix.
    - Otherwise 400.

    Explicit invalid input (non-alnum, too long) always 400s — those error
    paths are what the integration tests in section 6 of the plan exercise.
    """
    cleaned = (raw or "").strip().upper()
    if not cleaned:
        if default_from_name is not None:
            from app.models import _derive_sku_prefix as _derive

            return _derive(default_from_name)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SKU prefix is required",
        )
    if not cleaned.isalnum():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SKU prefix must contain only alphanumeric characters",
        )
    if len(cleaned) > 8:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SKU prefix must be 8 characters or fewer",
        )
    return cleaned


def _get_sub_category(db: Session, node_id: int) -> TaxonomyNode:
    """Load a sub-category by id; 404 if missing or if it's actually top-level."""
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="sub-category not found",
        )
    return node


def _diff(node: TaxonomyNode, new: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
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

    # A top-level node is a leaf (and therefore eligible to own field defs)
    # iff it has no active children. Computed here so the template can show a
    # per-row "Fields" link only on leaves.
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
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": None,
            "form": {
                "name": "",
                "sort_order": "",
                "archetype": "",
                "sku_prefix": "",
                "defaults": _defaults_form_view(None),
            },
            "title": "New category",
            "action": "/admin/taxonomy",
            "depth": 0,
            "archetype_options": [a.value for a in Archetype],
            "archetype_locked": False,
            "sku_prefix_locked": False,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
        },
    )


@router.post("")
def create_taxonomy(
    request: Request,
    name: str = Form(""),
    sort_order: str = Form(""),
    archetype: str = Form(""),
    sku_prefix: str = Form(""),
    default_unit: str = Form(""),
    default_tracking_mode: str = Form(""),
    default_requires_checkout: bool = Form(False),
    default_reorder_threshold: str = Form(""),
    default_reorder_qty: str = Form(""),
    default_supplier_id: str = Form(""),
    default_location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_top_name_unique(db, fields["name"])
    # Lenient defaults so callers that don't yet know about archetypes / SKU
    # prefixes (legacy POSTs from the pre-refinement form) keep working. The
    # taxonomy-refinement template prompts for both explicitly; missing here
    # only happens for hand-rolled POSTs and ad-hoc seed scripts.
    archetype_value = _validate_archetype(archetype, default=Archetype.BULK)
    raw_prefix = (sku_prefix or "").strip()
    if raw_prefix == "":
        # Auto-derive from name + disambiguate against active+archived
        # siblings. Mirrors the migration's backfill so the runtime path
        # matches the bootstrap path. Explicit user-supplied prefixes do
        # NOT disambiguate — a sibling collision is a user error and
        # should surface as a 400.
        prefix_value = _normalise_sku_prefix("", default_from_name=fields["name"])
        prefix_value = _disambiguate_top_prefix(db, prefix_value)
    else:
        prefix_value = _normalise_sku_prefix(raw_prefix)
    _check_sku_prefix_unique_top(db, prefix_value)
    if fields["sort_order"] is None:
        fields["sort_order"] = _next_top_sort_order(db)
    fields["defaults_json"] = _coerce_defaults(
        db,
        default_unit=default_unit,
        default_tracking_mode=default_tracking_mode,
        default_requires_checkout=default_requires_checkout,
        default_reorder_threshold=default_reorder_threshold,
        default_reorder_qty=default_reorder_qty,
        default_supplier_id=default_supplier_id,
        default_location_id=default_location_id,
    )

    node = TaxonomyNode(
        parent_id=None,
        name=fields["name"],
        sort_order=fields["sort_order"],
        defaults_json=fields["defaults_json"],
        archetype=archetype_value,
        sku_prefix=prefix_value,
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
            "defaults_json": node.defaults_json,
            "archetype": node.archetype.value if node.archetype else None,
            "sku_prefix": node.sku_prefix,
        },
    )
    db.commit()
    _flash(request, f"Category “{node.name}” created.")
    return RedirectResponse(url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    locked = _has_descendant_items(db, node.id)
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": node,
            "form": {
                "name": node.name,
                "sort_order": str(node.sort_order),
                "archetype": node.archetype.value if node.archetype else "",
                "sku_prefix": node.sku_prefix,
                "defaults": _defaults_form_view(node.defaults_json),
            },
            "title": f"Edit {node.name}",
            "action": f"/admin/taxonomy/{node.id}",
            "depth": 0,
            "archetype_options": [a.value for a in Archetype],
            "archetype_locked": locked,
            "sku_prefix_locked": locked,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
        },
    )


@router.post("/{node_id}")
def update_taxonomy(
    request: Request,
    node_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    archetype: str = Form(""),
    sku_prefix: str = Form(""),
    default_unit: str = Form(""),
    default_tracking_mode: str = Form(""),
    default_requires_checkout: bool = Form(False),
    default_reorder_threshold: str = Form(""),
    default_reorder_qty: str = Form(""),
    default_supplier_id: str = Form(""),
    default_location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_top_name_unique(db, fields["name"], exclude_id=node.id)

    locked = _has_descendant_items(db, node.id)

    # Archetype handling: blank input means "leave unchanged". Once any
    # descendant item exists, the archetype is locked — silently swallow any
    # submitted change to the same current value, otherwise 400.
    raw_archetype = (archetype or "").strip()
    if raw_archetype == "":
        archetype_value = node.archetype
    else:
        archetype_value = _validate_archetype(raw_archetype)
        if locked and archetype_value != node.archetype:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("cannot change archetype: items exist under this category"),
            )
    fields["archetype"] = archetype_value

    raw_prefix = (sku_prefix or "").strip()
    if raw_prefix == "":
        # Blank means "leave alone" — same posture as ``sort_order``.
        prefix_value = node.sku_prefix
    else:
        prefix_value = _normalise_sku_prefix(raw_prefix)
        if locked and prefix_value != node.sku_prefix:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("cannot change SKU prefix: items exist under this category"),
            )
        if prefix_value != node.sku_prefix:
            _check_sku_prefix_unique_top(db, prefix_value, exclude_id=node.id)
    fields["sku_prefix"] = prefix_value

    if fields["sort_order"] is None:
        # A blank sort_order on edit means "leave alone", not "reset". Without
        # this the field would silently snap back to whatever default the form
        # carried (zero).
        fields["sort_order"] = node.sort_order
    fields["defaults_json"] = _coerce_defaults(
        db,
        default_unit=default_unit,
        default_tracking_mode=default_tracking_mode,
        default_requires_checkout=default_requires_checkout,
        default_reorder_threshold=default_reorder_threshold,
        default_reorder_qty=default_reorder_qty,
        default_supplier_id=default_supplier_id,
        default_location_id=default_location_id,
    )

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

    return RedirectResponse(url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

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

    return RedirectResponse(url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{node_id}/unarchive")
def unarchive_taxonomy(
    request: Request,
    node_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = db.get(TaxonomyNode, node_id)
    if node is None or node.parent_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")

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

    return RedirectResponse(url="/admin/taxonomy", status_code=status.HTTP_303_SEE_OTHER)


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


_SUB_CSV_HEADERS: list[str] = [
    "id",
    "sort_order",
    "name",
]


def _csv_rows_for_sub_categories(rows: list[TaxonomyNode]) -> list[list[Any]]:
    """Map sub-category ``TaxonomyNode`` rows to CSV cell values.

    Mirrors ``_csv_rows_for_taxonomy``: three columns matching the HTML
    table's "Order" + "Name" columns, with ``id`` at the front for joining.
    Parent context is encoded in the filename, not as a per-row column —
    every row in a given file shares the same parent.
    """
    return [[n.id, n.sort_order, n.name] for n in rows]


@router.get("/{parent_id}/children")
def list_sub_categories(
    request: Request,
    parent_id: int,
    show: str = "active",
    format: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
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

    if (
        resp := csv_branch(
            format,
            filename=f"subcategories_parent_{parent.id}_{show}.csv",
            headers=_SUB_CSV_HEADERS,
            rows=_csv_rows_for_sub_categories(rows),
        )
    ) is not None:
        return resp

    # A depth-1 sub-category is a leaf if it has no active grandchildren.
    # Used by the template to show a per-row "Fields" link only on leaves.
    leaf_ids: set[int] = set()
    if rows:
        active_grandparent_ids = set(
            db.execute(
                select(TaxonomyNode.parent_id)
                .where(TaxonomyNode.parent_id.in_([n.id for n in rows]))
                .where(TaxonomyNode.archived_at.is_(None))
            )
            .scalars()
            .all()
        )
        leaf_ids = {n.id for n in rows if n.id not in active_grandparent_ids}

    return templates.TemplateResponse(
        request,
        "taxonomy_children_list.html",
        {
            "current_user": _user,
            "parent": parent,
            "nodes": rows,
            "show": show,
            "leaf_ids": leaf_ids,
            "inherited_archetype": (ea.value if (ea := effective_archetype(db, parent)) else None),
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
    # Field defs at this level remain valid — sub-categories created under
    # them will inherit those fields automatically (see
    # ``app.items._get_active_field_defs``). Items attached here still block
    # the un-leafing because they'd be orphaned at the schema level.
    if _has_active_items(db, parent.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add a sub-category here: this category has items "
                "attached. Move or archive them first."
            ),
        )
    inherited = effective_archetype(db, parent)
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": None,
            "parent": parent,
            "form": {
                "name": "",
                "sort_order": "",
                "sku_prefix": "",
                "defaults": _defaults_form_view(None),
            },
            "title": f"New sub-category under {parent.name}",
            "action": f"/admin/taxonomy/{parent.id}/children",
            "back_url": f"/admin/taxonomy/{parent.id}/children",
            "depth": 1,
            "inherited_archetype": inherited.value if inherited else None,
            "archetype_locked": True,
            "sku_prefix_locked": False,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
        },
    )


@router.post("/{parent_id}/children")
def create_sub_category(
    request: Request,
    parent_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    sku_prefix: str = Form(""),
    default_unit: str = Form(""),
    default_tracking_mode: str = Form(""),
    default_requires_checkout: bool = Form(False),
    default_reorder_threshold: str = Form(""),
    default_reorder_qty: str = Form(""),
    default_supplier_id: str = Form(""),
    default_location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    parent = _get_top_level_parent(db, parent_id)
    if parent.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add sub-categories under an archived category",
        )
    # Field defs at this level remain valid — see the GET form for context.
    if _has_active_items(db, parent.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add a sub-category here: this category has items "
                "attached. Move or archive them first."
            ),
        )

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_child_name_unique(db, parent_id=parent.id, name=fields["name"])
    # Same lenient default as the top-level route — see comment there for
    # the rationale.
    raw_prefix = (sku_prefix or "").strip()
    if raw_prefix == "":
        prefix_value = _normalise_sku_prefix("", default_from_name=fields["name"])
        prefix_value = _disambiguate_child_prefix(db, parent_id=parent.id, base=prefix_value)
    else:
        prefix_value = _normalise_sku_prefix(raw_prefix)
    _check_sku_prefix_unique_child(db, parent_id=parent.id, prefix=prefix_value)
    if fields["sort_order"] is None:
        fields["sort_order"] = _next_child_sort_order(db, parent.id)
    fields["defaults_json"] = _coerce_defaults(
        db,
        default_unit=default_unit,
        default_tracking_mode=default_tracking_mode,
        default_requires_checkout=default_requires_checkout,
        default_reorder_threshold=default_reorder_threshold,
        default_reorder_qty=default_reorder_qty,
        default_supplier_id=default_supplier_id,
        default_location_id=default_location_id,
    )

    node = TaxonomyNode(
        parent_id=parent.id,
        name=fields["name"],
        sort_order=fields["sort_order"],
        defaults_json=fields["defaults_json"],
        sku_prefix=prefix_value,
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
            "defaults_json": node.defaults_json,
            "sku_prefix": node.sku_prefix,
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
    locked = _has_descendant_items(db, node.id)
    inherited = effective_archetype(db, node)
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
                "sku_prefix": node.sku_prefix,
                "defaults": _defaults_form_view(node.defaults_json),
            },
            "title": f"Edit {node.name}",
            "action": f"/admin/taxonomy/sub/{node.id}",
            "back_url": f"/admin/taxonomy/{parent.id}/children",
            "depth": node_depth(db, node),
            "inherited_archetype": inherited.value if inherited else None,
            "archetype_locked": True,
            "sku_prefix_locked": locked,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
        },
    )


@router.post("/sub/{node_id}")
def update_sub_category(
    request: Request,
    node_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    sku_prefix: str = Form(""),
    default_unit: str = Form(""),
    default_tracking_mode: str = Form(""),
    default_requires_checkout: bool = Form(False),
    default_reorder_threshold: str = Form(""),
    default_reorder_qty: str = Form(""),
    default_supplier_id: str = Form(""),
    default_location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = _get_sub_category(db, node_id)
    assert node.parent_id is not None  # narrowed by _get_sub_category

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_child_name_unique(db, parent_id=node.parent_id, name=fields["name"], exclude_id=node.id)

    locked = _has_descendant_items(db, node.id)

    raw_prefix = (sku_prefix or "").strip()
    if raw_prefix == "":
        prefix_value = node.sku_prefix
    else:
        prefix_value = _normalise_sku_prefix(raw_prefix)
        if locked and prefix_value != node.sku_prefix:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=("cannot change SKU prefix: items exist under this sub-category"),
            )
        if prefix_value != node.sku_prefix:
            _check_sku_prefix_unique_child(
                db,
                parent_id=node.parent_id,
                prefix=prefix_value,
                exclude_id=node.id,
            )
    fields["sku_prefix"] = prefix_value
    # Sub-cats never own ``archetype`` — keep the existing NULL/value as-is
    # so the audit diff doesn't pretend it changed.
    fields["archetype"] = node.archetype

    if fields["sort_order"] is None:
        fields["sort_order"] = node.sort_order
    fields["defaults_json"] = _coerce_defaults(
        db,
        default_unit=default_unit,
        default_tracking_mode=default_tracking_mode,
        default_requires_checkout=default_requires_checkout,
        default_reorder_threshold=default_reorder_threshold,
        default_reorder_qty=default_reorder_qty,
        default_supplier_id=default_supplier_id,
        default_location_id=default_location_id,
    )

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


# ===========================================================================
# Sub-sub-category routes (depth 2)
# ===========================================================================
#
# Depth-2 nodes are only manually creatable under a ``bulk`` or ``unique``
# top-level tree. Under a ``unique_variant`` tree the depth-2 leaves are
# system-managed: one auto-leaf per item, minted by ``app.items.create_item``
# via ``app.sku.create_unique_variant_leaf``. Manual depth-2 creates under a
# ``unique_variant`` parent are rejected with a clear 400.
#
# Edit / archive / unarchive of a depth-2 node reuse the existing
# ``/sub/{node_id}/...`` shape — ``_get_sub_category`` already accepts any
# node with a non-null ``parent_id``.


def _get_depth1_subcat_under(
    db: Session, parent_id: int, sub_id: int
) -> tuple[TaxonomyNode, TaxonomyNode]:
    """Resolve the (depth-0 parent, depth-1 sub-cat) pair from the URL.

    Returns ``(parent, sub)`` on success. 404 if either id is missing or if
    ``sub.parent_id != parent.id`` (URL-vs-DB mismatch). Used by every
    grandchildren route so the breadcrumb in the URL stays honest.
    """
    parent = db.get(TaxonomyNode, parent_id)
    if parent is None or parent.parent_id is not None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    sub = db.get(TaxonomyNode, sub_id)
    if sub is None or sub.parent_id != parent.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="sub-category not found",
        )
    return parent, sub


_GRAND_CSV_HEADERS: list[str] = [
    "id",
    "sort_order",
    "name",
    "sku_prefix",
]


def _csv_rows_for_grandchildren(rows: list[TaxonomyNode]) -> list[list[Any]]:
    """Map depth-2 ``TaxonomyNode`` rows to CSV cell values."""
    return [[n.id, n.sort_order, n.name, n.sku_prefix] for n in rows]


@router.get("/{parent_id}/sub/{sub_id}/grandchildren")
def list_grandchildren(
    request: Request,
    parent_id: int,
    sub_id: int,
    show: str = "active",
    format: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    parent, sub = _get_depth1_subcat_under(db, parent_id, sub_id)

    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(TaxonomyNode).where(TaxonomyNode.parent_id == sub.id)
    if show == "active":
        stmt = stmt.where(TaxonomyNode.archived_at.is_(None))
    else:
        stmt = stmt.where(TaxonomyNode.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, TaxonomyNode.sort_order, TaxonomyNode.name)

    rows = list(db.execute(stmt).scalars().all())

    if (
        resp := csv_branch(
            format,
            filename=(f"sub_sub_categories_parent_{parent.id}_sub_{sub.id}_{show}.csv"),
            headers=_GRAND_CSV_HEADERS,
            rows=_csv_rows_for_grandchildren(rows),
        )
    ) is not None:
        return resp

    return templates.TemplateResponse(
        request,
        "taxonomy_grandchildren_list.html",
        {
            "current_user": _user,
            "grandparent": parent,
            "parent": sub,
            "nodes": rows,
            "show": show,
            "inherited_archetype": (ea.value if (ea := effective_archetype(db, sub)) else None),
        },
    )


@router.get(
    "/{parent_id}/sub/{sub_id}/grandchildren/new",
    response_class=HTMLResponse,
)
def new_grandchild_form(
    request: Request,
    parent_id: int,
    sub_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    parent, sub = _get_depth1_subcat_under(db, parent_id, sub_id)
    if sub.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add a sub-sub-category under an archived parent",
        )
    inherited = effective_archetype(db, sub)
    if inherited == Archetype.UNIQUE_VARIANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "depth-2 categories under unique-variant trees are created "
                "automatically when items are added"
            ),
        )
    # Field defs at this level remain valid — sub-sub-categories created
    # here will inherit them automatically.
    if _has_active_items(db, sub.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add a sub-sub-category here: this sub-category has "
                "items attached. Move or archive them first."
            ),
        )
    return templates.TemplateResponse(
        request,
        "taxonomy_form.html",
        {
            "current_user": _user,
            "node": None,
            "parent": sub,
            "grandparent": parent,
            "form": {
                "name": "",
                "sort_order": "",
                "sku_prefix": "",
                "defaults": _defaults_form_view(None),
            },
            "title": f"New sub-sub-category under {sub.name}",
            "action": (f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren"),
            "back_url": (f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren"),
            "depth": 2,
            "inherited_archetype": inherited.value if inherited else None,
            "archetype_locked": True,
            "sku_prefix_locked": False,
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "tracking_modes": [m.value for m in TrackingMode],
        },
    )


@router.post("/{parent_id}/sub/{sub_id}/grandchildren")
def create_grandchild(
    request: Request,
    parent_id: int,
    sub_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    sku_prefix: str = Form(""),
    default_unit: str = Form(""),
    default_tracking_mode: str = Form(""),
    default_requires_checkout: bool = Form(False),
    default_reorder_threshold: str = Form(""),
    default_reorder_qty: str = Form(""),
    default_supplier_id: str = Form(""),
    default_location_id: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    parent, sub = _get_depth1_subcat_under(db, parent_id, sub_id)
    if sub.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot add a sub-sub-category under an archived parent",
        )
    inherited = effective_archetype(db, sub)
    if inherited == Archetype.UNIQUE_VARIANT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "depth-2 categories under unique-variant trees are created "
                "automatically when items are added"
            ),
        )
    # Field defs at this level remain valid — sub-sub-categories created
    # here will inherit them automatically.
    if _has_active_items(db, sub.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "cannot add a sub-sub-category here: this sub-category has "
                "items attached. Move or archive them first."
            ),
        )

    fields = _normalise(name, sort_order)
    _validate_name(fields["name"])
    _check_child_name_unique(db, parent_id=sub.id, name=fields["name"])
    raw_prefix = (sku_prefix or "").strip()
    if raw_prefix == "":
        prefix_value = _normalise_sku_prefix("", default_from_name=fields["name"])
        prefix_value = _disambiguate_child_prefix(db, parent_id=sub.id, base=prefix_value)
    else:
        prefix_value = _normalise_sku_prefix(raw_prefix)
    _check_sku_prefix_unique_child(db, parent_id=sub.id, prefix=prefix_value)
    if fields["sort_order"] is None:
        fields["sort_order"] = _next_child_sort_order(db, sub.id)
    fields["defaults_json"] = _coerce_defaults(
        db,
        default_unit=default_unit,
        default_tracking_mode=default_tracking_mode,
        default_requires_checkout=default_requires_checkout,
        default_reorder_threshold=default_reorder_threshold,
        default_reorder_qty=default_reorder_qty,
        default_supplier_id=default_supplier_id,
        default_location_id=default_location_id,
    )

    node = TaxonomyNode(
        parent_id=sub.id,
        name=fields["name"],
        sort_order=fields["sort_order"],
        defaults_json=fields["defaults_json"],
        sku_prefix=prefix_value,
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
            "parent_id": sub.id,
            "defaults_json": node.defaults_json,
            "sku_prefix": node.sku_prefix,
        },
    )
    db.commit()
    _flash(request, f"Sub-sub-category “{node.name}” created.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{parent.id}/sub/{sub.id}/grandchildren",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# Lifecycle stages (per top-level node)
# ---------------------------------------------------------------------------
#
# Each top-level taxonomy node owns an ordered list of stages. Items inside
# that category default to the ``is_initial`` stage on create; transitions are
# recorded as ``STAGE_CHANGE`` movements in ``app/movements.py``. See the
# `Plan: Lifecycle stages` design for the full semantics.

_STAGE_LIST_ORDER = case((TaxonomyStage.archived_at.is_(None), 0), else_=1)


def _get_top_level_node(db: Session, node_id: int) -> TaxonomyNode:
    """Load a top-level taxonomy node or 404 / 400.

    Stages are scoped to depth-0 nodes only; rejecting a sub-category here
    keeps the URL surface unambiguous and matches the field-defs admin posture
    (fields live on leaves, stages live on the top of the tree).
    """
    node = db.get(TaxonomyNode, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")
    if node.parent_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stages are owned by the top-level category",
        )
    return node


def _validate_stage_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    if len(cleaned) > 64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stage name must be 64 characters or fewer",
        )
    return cleaned


def _parse_sort_order(raw: str, *, default: int) -> int:
    text_val = (raw or "").strip()
    if text_val == "":
        return default
    try:
        return int(text_val)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="sort_order must be a whole number",
        ) from exc


def _check_stage_name_unique(
    db: Session,
    *,
    top_level_node_id: int,
    name: str,
    exclude_id: int | None = None,
) -> None:
    stmt = (
        select(TaxonomyStage.id)
        .where(TaxonomyStage.top_level_node_id == top_level_node_id)
        .where(TaxonomyStage.name == name)
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyStage.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a stage with that name already exists for this category",
        )


def _next_stage_sort_order(db: Session, top_level_node_id: int) -> int:
    stmt = select(func.max(TaxonomyStage.sort_order)).where(
        TaxonomyStage.top_level_node_id == top_level_node_id
    )
    current_max = db.execute(stmt).scalar()
    if current_max is None:
        return 0
    return int(current_max) + _SORT_ORDER_STEP


def _clear_other_initial(
    db: Session, *, top_level_node_id: int, exclude_id: int | None
) -> None:
    """Unset ``is_initial`` on any other active stage under the same top-level node.

    The partial unique index ``uq_taxonomy_stage_initial_active`` enforces the
    invariant at the DB layer; this helper makes the route-side write
    succeed-instead-of-IntegrityError when the user reassigns initial without
    first un-checking it elsewhere.
    """
    stmt = select(TaxonomyStage).where(
        TaxonomyStage.top_level_node_id == top_level_node_id,
        TaxonomyStage.is_initial.is_(True),
        TaxonomyStage.archived_at.is_(None),
    )
    if exclude_id is not None:
        stmt = stmt.where(TaxonomyStage.id != exclude_id)
    for row in db.execute(stmt).scalars().all():
        row.is_initial = False


def _get_stage(db: Session, stage_id: int) -> TaxonomyStage:
    stage = db.get(TaxonomyStage, stage_id)
    if stage is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="stage not found")
    return stage


@router.get("/{node_id}/stages", response_class=HTMLResponse)
def list_stages(
    request: Request,
    node_id: int,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    node = _get_top_level_node(db, node_id)
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(TaxonomyStage).where(TaxonomyStage.top_level_node_id == node.id)
    if show == "active":
        stmt = stmt.where(TaxonomyStage.archived_at.is_(None))
    else:
        stmt = stmt.where(TaxonomyStage.archived_at.is_not(None))
    stmt = stmt.order_by(_STAGE_LIST_ORDER, TaxonomyStage.sort_order, TaxonomyStage.name)

    rows = list(db.execute(stmt).scalars().all())

    return templates.TemplateResponse(
        request,
        "taxonomy_stages_list.html",
        {
            "current_user": _user,
            "node": node,
            "stages": rows,
            "show": show,
        },
    )


@router.get("/{node_id}/stages/new", response_class=HTMLResponse)
def new_stage_form(
    request: Request,
    node_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    node = _get_top_level_node(db, node_id)
    has_initial = (
        db.execute(
            select(TaxonomyStage.id)
            .where(TaxonomyStage.top_level_node_id == node.id)
            .where(TaxonomyStage.is_initial.is_(True))
            .where(TaxonomyStage.archived_at.is_(None))
        ).first()
        is not None
    )
    return templates.TemplateResponse(
        request,
        "taxonomy_stages_form.html",
        {
            "current_user": _user,
            "node": node,
            "stage": None,
            "form": {
                "name": "",
                "sort_order": "",
                "is_initial": not has_initial,
            },
            "title": "New stage",
            "action": f"/admin/taxonomy/{node.id}/stages",
        },
    )


@router.post("/{node_id}/stages")
def create_stage(
    request: Request,
    node_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    is_initial: bool = Form(False),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    node = _get_top_level_node(db, node_id)
    clean_name = _validate_stage_name(name)
    _check_stage_name_unique(db, top_level_node_id=node.id, name=clean_name)
    sort_value = _parse_sort_order(sort_order, default=_next_stage_sort_order(db, node.id))

    if is_initial:
        _clear_other_initial(db, top_level_node_id=node.id, exclude_id=None)

    stage = TaxonomyStage(
        top_level_node_id=node.id,
        name=clean_name,
        sort_order=sort_value,
        is_initial=is_initial,
    )
    db.add(stage)
    db.flush()
    record_audit(
        db,
        actor=user,
        action="taxonomy_stage.created",
        entity_type="taxonomy_stage",
        entity_id=stage.id,
        before=None,
        after={
            "top_level_node_id": node.id,
            "name": stage.name,
            "sort_order": stage.sort_order,
            "is_initial": stage.is_initial,
        },
    )
    db.commit()
    _flash(request, f"Stage “{stage.name}” created.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/stages",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/stages/{stage_id}/edit", response_class=HTMLResponse)
def edit_stage_form(
    request: Request,
    stage_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stage = _get_stage(db, stage_id)
    node = _get_top_level_node(db, stage.top_level_node_id)
    return templates.TemplateResponse(
        request,
        "taxonomy_stages_form.html",
        {
            "current_user": _user,
            "node": node,
            "stage": stage,
            "form": {
                "name": stage.name,
                "sort_order": str(stage.sort_order),
                "is_initial": stage.is_initial,
            },
            "title": f"Edit stage — {stage.name}",
            "action": f"/admin/taxonomy/stages/{stage.id}",
        },
    )


@router.post("/stages/{stage_id}")
def update_stage(
    request: Request,
    stage_id: int,
    name: str = Form(""),
    sort_order: str = Form(""),
    is_initial: bool = Form(False),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stage = _get_stage(db, stage_id)
    node = _get_top_level_node(db, stage.top_level_node_id)

    clean_name = _validate_stage_name(name)
    _check_stage_name_unique(
        db, top_level_node_id=node.id, name=clean_name, exclude_id=stage.id
    )
    sort_value = _parse_sort_order(sort_order, default=stage.sort_order)

    before = {
        "name": stage.name,
        "sort_order": stage.sort_order,
        "is_initial": stage.is_initial,
    }
    after = {
        "name": clean_name,
        "sort_order": sort_value,
        "is_initial": is_initial,
    }
    if before == after:
        db.rollback()
        return RedirectResponse(
            url=f"/admin/taxonomy/{node.id}/stages",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    if is_initial and not stage.is_initial:
        _clear_other_initial(db, top_level_node_id=node.id, exclude_id=stage.id)
    stage.name = clean_name
    stage.sort_order = sort_value
    stage.is_initial = is_initial

    record_audit(
        db,
        actor=user,
        action="taxonomy_stage.updated",
        entity_type="taxonomy_stage",
        entity_id=stage.id,
        before=before,
        after=after,
    )
    db.commit()
    _flash(request, f"Stage “{stage.name}” updated.")
    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/stages",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/stages/{stage_id}/archive")
def archive_stage(
    request: Request,
    stage_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stage = _get_stage(db, stage_id)
    node = _get_top_level_node(db, stage.top_level_node_id)

    if stage.archived_at is None:
        stage.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="taxonomy_stage.archived",
            entity_type="taxonomy_stage",
            entity_id=stage.id,
            before={"archived_at": None},
            after={"archived_at": stage.archived_at},
        )
        db.commit()
        _flash(request, f"Stage “{stage.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/stages",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/stages/{stage_id}/unarchive")
def unarchive_stage(
    request: Request,
    stage_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stage = _get_stage(db, stage_id)
    node = _get_top_level_node(db, stage.top_level_node_id)

    if stage.archived_at is not None:
        previous = stage.archived_at
        stage.archived_at = None
        # If another active stage is already initial for this category, leave
        # ``is_initial`` on this row alone — the partial unique index would
        # 500 the unarchive. The manager toggles initial back on via edit.
        has_other_initial = (
            db.execute(
                select(TaxonomyStage.id)
                .where(TaxonomyStage.top_level_node_id == node.id)
                .where(TaxonomyStage.id != stage.id)
                .where(TaxonomyStage.is_initial.is_(True))
                .where(TaxonomyStage.archived_at.is_(None))
            ).first()
            is not None
        )
        if stage.is_initial and has_other_initial:
            stage.is_initial = False
        record_audit(
            db,
            actor=user,
            action="taxonomy_stage.unarchived",
            entity_type="taxonomy_stage",
            entity_id=stage.id,
            before={"archived_at": previous},
            after={"archived_at": None, "is_initial": stage.is_initial},
        )
        db.commit()
        _flash(request, f"Stage “{stage.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url=f"/admin/taxonomy/{node.id}/stages",
        status_code=status.HTTP_303_SEE_OTHER,
    )
