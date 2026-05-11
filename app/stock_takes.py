"""Stock take scheduling, counting, and commit (ST1 + ST2 + ST3 — DoD #5).

Manager + Office surface mounted at ``/admin/stock-takes``:

- ``GET /admin/stock-takes[?show=open|completed]`` — list (ST1).
- ``GET /admin/stock-takes/new`` — render the scheduling form (ST1).
- ``POST /admin/stock-takes`` — schedule a stock take (ST1).
- ``GET /admin/stock-takes/{id}`` — detail page (ST2 + ST3). Renders one of
  three branches gated on the derived status: ``scheduled`` (start form +
  scope preview), ``in_progress`` (count form + commit form when there are
  variances), ``completed`` (read-only summary with committed badges).
- ``POST /admin/stock-takes/{id}/start`` — freeze in-scope items into
  ``StockTakeLine`` rows + flip ``started_at`` (ST2).
- ``POST /admin/stock-takes/{id}/counts`` — save per-line ``counted_qty`` +
  derived ``variance`` (ST2). Sparse diff; no-op writes no audit.
- ``POST /admin/stock-takes/{id}/commit`` — for every line with non-zero
  variance build one ``StockMovement(type=ADJUSTMENT)`` via the cost engine
  (positive → ``record_receipt``; negative → ``consume_fifo``), flip
  ``committed=True``, set ``completed_at = now()``, write a
  ``stock_take.committed`` audit row (ST3).

Engine ownership: counts (ST2) never touch the cost engine. ST3 is the slice
that actions variances as adjustment movements via the engine. ``record_receipt``
and ``consume_fifo`` remain the single owners of ``cost_layers.qty_remaining``,
``item.current_qty``, and ``movement.total_cost`` — the commit route delegates
the FIFO arithmetic to them.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.cost_engine import (
    InsufficientStockError,
    consume_fifo,
    record_receipt,
)
from app.csv_export import csv_branch
from app.db import get_session
from app.models import (
    CostLayer,
    CostLayerSource,
    Item,
    Location,
    MovementType,
    Role,
    StockMovement,
    StockTake,
    StockTakeLine,
    TaxonomyNode,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/stock-takes", tags=["stock-takes"])


_VALID_SHOW = ("open", "completed")
_VALID_SCOPE_TYPES = ("all", "node", "location")
_NOTES_MAX = 2000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_show(raw: str | None) -> str:
    """Default to ``open`` when blank or unrecognised.

    Same posture as ``checkouts_admin._coerce_show`` and the items list's
    ``?show=active|archived`` — a stale shared link with a typo lands on the
    default rather than 400.
    """
    if raw in _VALID_SHOW:
        return raw
    return "open"


def _status_label(st: StockTake) -> str:
    """Lifecycle state derived from the timestamps (no enum column)."""
    if st.completed_at is not None:
        return "completed"
    if st.started_at is not None:
        return "in_progress"
    return "scheduled"


def _scope_label(st: StockTake, *, node: TaxonomyNode | None, location: Location | None) -> str:
    """User-facing scope label for the list view."""
    if st.scope_node_id is not None and node is not None:
        return f"Category: {node.name}"
    if st.scope_location_id is not None and location is not None:
        return f"Location: {location.name}"
    return "All items"


def _active_nodes(db: Session) -> list[TaxonomyNode]:
    """Active taxonomy nodes for the form ``<select>``.

    Returns top-level + sub-categories together; the v1 taxonomy is two
    levels deep and either level is a valid stock-take scope. Ordering puts
    parents first via NULLS-FIRST ``parent_id`` (top-level rows have
    ``parent_id IS NULL``), then by sort_order, then alphabetic.
    """
    stmt = (
        select(TaxonomyNode)
        .where(TaxonomyNode.archived_at.is_(None))
        .order_by(
            TaxonomyNode.parent_id.is_(None).desc(),
            TaxonomyNode.parent_id,
            TaxonomyNode.sort_order,
            TaxonomyNode.name,
        )
    )
    return list(db.execute(stmt).scalars().all())


def _active_locations(db: Session) -> list[Location]:
    """Active locations for the form ``<select>``."""
    stmt = select(Location).where(Location.archived_at.is_(None)).order_by(Location.name)
    return list(db.execute(stmt).scalars().all())


def _parse_scheduled_for(raw: str) -> date:
    """Strict ISO ``YYYY-MM-DD`` parse; blank or bad → 400."""
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scheduled_for is required",
        )
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scheduled_for must be an ISO date (YYYY-MM-DD)",
        ) from exc


def _parse_optional_notes(raw: str) -> str | None:
    """Strip + length-cap; blank → None; over-cap → 400."""
    notes = (raw or "").strip()
    if not notes:
        return None
    if len(notes) > _NOTES_MAX:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"notes must be at most {_NOTES_MAX} characters",
        )
    return notes


def _resolve_node(db: Session, raw: str) -> TaxonomyNode:
    """Parse + validate ``scope_node_id`` form input.

    Blank / non-int / unknown / archived all 400. The route only calls this
    when ``scope_type == "node"``.
    """
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_node_id is required when scope_type is node",
        )
    try:
        node_id = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_node_id must be an integer",
        ) from exc
    node = db.get(TaxonomyNode, node_id)
    if node is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_node_id does not reference a known node",
        )
    if node.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope category is archived",
        )
    return node


def _resolve_location(db: Session, raw: str) -> Location:
    """Parse + validate ``scope_location_id``. Same posture as ``_resolve_node``."""
    raw = (raw or "").strip()
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_location_id is required when scope_type is location",
        )
    try:
        loc_id = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_location_id must be an integer",
        ) from exc
    loc = db.get(Location, loc_id)
    if loc is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_location_id does not reference a known location",
        )
    if loc.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope location is archived",
        )
    return loc


def _flash(request: Request, message: str) -> None:
    """Stash a one-shot message in the session; rendered + cleared by base.html."""
    request.session["flash"] = message


def _can_start(st: StockTake) -> bool:
    """Only a strictly-scheduled (both timestamps null) row can be started."""
    return st.started_at is None and st.completed_at is None


def _can_count(st: StockTake) -> bool:
    """Only an in-progress row (started_at set, completed_at null) accepts counts."""
    return st.started_at is not None and st.completed_at is None


def _can_commit(st: StockTake) -> bool:
    """Only an in-progress row can be committed.

    Same predicate as ``_can_count`` (commit happens against the same
    lifecycle window). A scheduled row needs to be started first; a completed
    row is read-only.
    """
    return st.started_at is not None and st.completed_at is None


def _last_unit_cost(db: Session, item_id: int) -> Decimal | None:
    """Most recent cost layer's unit cost for an item, or ``None``.

    Used to default ``unit_cost_<line_id>`` on the commit form for positive
    variance lines. Newest by ``received_at`` (with id-tiebreak), regardless
    of ``qty_remaining`` — a fully-consumed layer is still the most recent
    unit-cost signal. Same shape as ``app.purchase_orders._last_unit_cost``.
    """
    stmt = (
        select(CostLayer.unit_cost)
        .where(CostLayer.item_id == item_id)
        .order_by(CostLayer.received_at.desc(), CostLayer.id.desc())
        .limit(1)
    )
    row = db.execute(stmt).first()
    return row[0] if row is not None else None


def _parse_unit_cost_for_commit(raw: str, *, line_id: int, required: bool) -> Decimal | None:
    """Parse ``unit_cost_<line_id>`` for the commit form.

    Blank → ``None`` when ``required=False`` (negative-variance line; the
    consumption price is per-layer, the input is ignored). Blank → 400 when
    ``required=True`` (positive-variance line; the operator must affirm a
    price even if it's zero). Non-numeric / negative → 400. Zero allowed
    (sample / gifted stock — same posture as M2 / M4).
    """
    text = (raw or "").strip()
    if text == "":
        if required:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(f"unit_cost for line {line_id} is required for a positive variance"),
            )
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unit_cost for line {line_id} must be a number",
        ) from exc
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unit_cost for line {line_id} cannot be negative",
        )
    return value


def _lines_with_non_zero_variance(
    rows: list[tuple[StockTakeLine, Item]],
) -> list[tuple[StockTakeLine, Item]]:
    """Filter the lines down to those that need an adjustment movement.

    Zero-variance and uncounted (variance is None) lines are excluded — the
    commit route doesn't write a movement for them, doesn't flip ``committed``
    on them, and doesn't list them in the audit row's ``movements`` snapshot.
    """
    return [(line, item) for line, item in rows if line.variance is not None and line.variance != 0]


def _resolve_scope_items(db: Session, st: StockTake) -> list[Item]:
    """Return the list of active items in scope for this stock take, ordered by sku.

    - Both scope ids null → all active items.
    - ``scope_node_id`` set → items at that node OR at any of its direct
      sub-categories (the v1 taxonomy is two levels deep, so the
      "is-descendant-of" predicate reduces to a single ``parent_id`` lookup).
    - ``scope_location_id`` set → items at that location.

    Archived items are always excluded (a stock take counts what's currently
    on the floor).
    """
    stmt = select(Item).where(Item.archived_at.is_(None))
    if st.scope_node_id is not None:
        child_ids = list(
            db.execute(select(TaxonomyNode.id).where(TaxonomyNode.parent_id == st.scope_node_id))
            .scalars()
            .all()
        )
        in_scope_node_ids = [st.scope_node_id, *child_ids]
        stmt = stmt.where(Item.taxonomy_node_id.in_(in_scope_node_ids))
    elif st.scope_location_id is not None:
        stmt = stmt.where(Item.location_id == st.scope_location_id)
    stmt = stmt.order_by(Item.sku)
    return list(db.execute(stmt).scalars().all())


def _compute_variance(counted: Decimal | None, system: Decimal) -> Decimal | None:
    """``counted - system`` when counted is set; else ``None``."""
    if counted is None:
        return None
    return counted - system


def _format_variance(value: Decimal | None) -> str:
    """Signed string (``"+1.5000"`` / ``"-2.0000"`` / ``"0.0000"``); empty when None."""
    if value is None:
        return ""
    if value > 0:
        return f"+{value}"
    return str(value)


def _variance_sign(value: Decimal | None) -> str:
    """``"pos"`` / ``"neg"`` / ``"zero"`` for the count-cell data attribute; ``""`` when None."""
    if value is None:
        return ""
    if value > 0:
        return "pos"
    if value < 0:
        return "neg"
    return "zero"


def _parse_optional_count(raw: str, *, line_id: int) -> Decimal | None:
    """Parse ``counted_<line_id>`` form field. Blank → None (uncount); negative → 400."""
    text = (raw or "").strip()
    if text == "":
        return None
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"counted_qty for line {line_id} must be a number",
        ) from exc
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"counted_qty for line {line_id} cannot be negative",
        )
    return value


def _lines_with_items(db: Session, st_id: int) -> list[tuple[StockTakeLine, Item]]:
    """Fetch lines joined to their item, ordered by ``Item.sku``."""
    stmt = (
        select(StockTakeLine, Item)
        .join(Item, StockTakeLine.item_id == Item.id)
        .where(StockTakeLine.stock_take_id == st_id)
        .order_by(Item.sku)
    )
    return [(line, item) for line, item in db.execute(stmt).all()]


def _count_progress(
    rows: list[tuple[StockTakeLine, Item]],
) -> dict[str, int]:
    """Per-status counters for the progress summary."""
    counted = sum(1 for line, _ in rows if line.counted_qty is not None)
    uncounted = len(rows) - counted
    with_variance = sum(1 for line, _ in rows if line.variance is not None and line.variance != 0)
    return {
        "counted": counted,
        "uncounted": uncounted,
        "with_variance": with_variance,
    }


def _detail_context(
    db: Session,
    st: StockTake,
    user: User,
    *,
    commit_error: str | None = None,
    commit_form_values: dict[int, str] | None = None,
) -> dict[str, Any]:
    """Build the template context for the detail page.

    Branches on the derived status (scheduled / in_progress / completed) and
    surfaces the right shape: scope items + start form for scheduled; count
    form + commit form for in-progress; read-only table for completed.

    ``commit_error`` + ``commit_form_values`` are populated only by the
    insufficient-stock re-render path on a failed commit POST. They preserve
    the operator's typed ``unit_cost_<line_id>`` inputs so a partial fix
    doesn't lose context.
    """
    node = db.get(TaxonomyNode, st.scope_node_id) if st.scope_node_id is not None else None
    location = db.get(Location, st.scope_location_id) if st.scope_location_id is not None else None
    status_label = _status_label(st)
    scope_label = _scope_label(st, node=node, location=location)
    creator_email: str | None = None
    if st.created_by is not None:
        creator = db.get(User, st.created_by)
        creator_email = creator.email if creator is not None else None

    ctx: dict[str, Any] = {
        "current_user": user,
        "st": st,
        "status": status_label,
        "scope_label": scope_label,
        "creator_email": creator_email,
        "commit_error": commit_error,
    }

    if status_label == "scheduled":
        scope_items = _resolve_scope_items(db, st)
        ctx["scope_items"] = scope_items
    else:
        rows = _lines_with_items(db, st.id)
        progress = _count_progress(rows)
        line_views: list[dict[str, Any]] = []
        for line, item in rows:
            line_views.append(
                {
                    "line_id": line.id,
                    "item_id": item.id,
                    "item_sku": item.sku,
                    "item_name": item.name,
                    "item_unit": item.unit,
                    "system_qty": line.system_qty,
                    "counted_qty": line.counted_qty,
                    "variance": line.variance,
                    "variance_str": _format_variance(line.variance),
                    "variance_sign": _variance_sign(line.variance),
                    "is_counted": line.counted_qty is not None,
                    "is_committed": line.committed,
                }
            )
        ctx["lines"] = line_views
        ctx["progress"] = progress

        # Commit-form view — only relevant on the in-progress branch. The
        # template still renders the count form alongside; the commit form is
        # an additional sub-section gated on ``commit_lines`` being non-empty.
        if status_label == "in_progress":
            variance_rows = _lines_with_non_zero_variance(rows)
            commit_views: list[dict[str, Any]] = []
            for line, item in variance_rows:
                # By construction `variance` is non-None on a variance row.
                assert line.variance is not None
                direction = "increase" if line.variance > 0 else "decrease"
                if direction == "increase":
                    if commit_form_values is not None and line.id in commit_form_values:
                        unit_cost_default = commit_form_values[line.id]
                    else:
                        last = _last_unit_cost(db, item.id)
                        unit_cost_default = str(last) if last is not None else ""
                else:
                    unit_cost_default = ""
                commit_views.append(
                    {
                        "line_id": line.id,
                        "item_id": item.id,
                        "item_sku": item.sku,
                        "item_name": item.name,
                        "item_unit": item.unit,
                        "variance": line.variance,
                        "variance_str": _format_variance(line.variance),
                        "direction": direction,
                        "unit_cost_default": unit_cost_default,
                    }
                )
            ctx["commit_lines"] = commit_views
    return ctx


def _list_rows(db: Session, *, show: str) -> list[dict[str, Any]]:
    """View-shaped rows for the list page.

    One query for stock_takes filtered by completion state, then a single
    extra round-trip apiece for the node + location lookup tables — at v1
    scale (a handful of stock takes per month) this is sub-millisecond.
    """
    stmt = select(StockTake)
    if show == "completed":
        stmt = stmt.where(StockTake.completed_at.is_not(None))
    else:
        stmt = stmt.where(StockTake.completed_at.is_(None))
    stmt = stmt.order_by(StockTake.scheduled_for.desc(), StockTake.id.desc())
    stock_takes = list(db.execute(stmt).scalars().all())

    node_ids = {st.scope_node_id for st in stock_takes if st.scope_node_id is not None}
    loc_ids = {st.scope_location_id for st in stock_takes if st.scope_location_id is not None}
    nodes = (
        {
            n.id: n
            for n in db.execute(select(TaxonomyNode).where(TaxonomyNode.id.in_(node_ids)))
            .scalars()
            .all()
        }
        if node_ids
        else {}
    )
    locations = (
        {
            loc.id: loc
            for loc in db.execute(select(Location).where(Location.id.in_(loc_ids))).scalars().all()
        }
        if loc_ids
        else {}
    )
    creator_ids = {st.created_by for st in stock_takes if st.created_by is not None}
    creators = (
        {
            u.id: u.email
            for u in db.execute(select(User).where(User.id.in_(creator_ids))).scalars().all()
        }
        if creator_ids
        else {}
    )

    rows: list[dict[str, Any]] = []
    for st in stock_takes:
        node = nodes.get(st.scope_node_id) if st.scope_node_id is not None else None
        loc = locations.get(st.scope_location_id) if st.scope_location_id is not None else None
        rows.append(
            {
                "id": st.id,
                "scope_label": _scope_label(st, node=node, location=loc),
                "scheduled_for": st.scheduled_for,
                "status": _status_label(st),
                "created_by_email": (
                    creators.get(st.created_by) if st.created_by is not None else None
                ),
                "created_at": st.created_at,
            }
        )
    return rows


_STOCK_TAKES_LIST_CSV_HEADERS: list[str] = [
    "id",
    "scope",
    "scheduled_for",
    "status",
    "created_by_email",
    "created_at",
]


def _csv_rows_for_stock_takes_list(
    rows: list[dict[str, Any]],
) -> list[list[Any]]:
    """Map view-shaped stock-take rows to CSV cell values.

    The ``created_by_email`` cell is empty (``None`` → ``""`` via
    ``csv_response``'s coercion) when the creator was deleted (FK
    ``ON DELETE SET NULL``), matching the ``—`` rendered in HTML.
    """
    return [
        [
            r["id"],
            r["scope_label"],
            r["scheduled_for"],
            r["status"],
            r["created_by_email"],
            r["created_at"],
        ]
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("")
def list_stock_takes(
    request: Request,
    format: str = "",
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    show = _coerce_show(request.query_params.get("show"))
    rows = _list_rows(db, show=show)
    if (
        resp := csv_branch(
            format,
            filename=f"stock_takes_{show}.csv",
            headers=_STOCK_TAKES_LIST_CSV_HEADERS,
            rows=_csv_rows_for_stock_takes_list(rows),
        )
    ) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "stock_takes_list.html",
        {
            "current_user": user,
            "show": show,
            "rows": rows,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_stock_take_form(
    request: Request,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "stock_take_form.html",
        {
            "current_user": user,
            "nodes": _active_nodes(db),
            "locations": _active_locations(db),
            "form": {
                "scope_type": "all",
                "scope_node_id": "",
                "scope_location_id": "",
                "scheduled_for": "",
                "notes": "",
            },
        },
    )


@router.post("")
def create_stock_take(
    request: Request,
    scope_type: str = Form(""),
    scope_node_id: str = Form(""),
    scope_location_id: str = Form(""),
    scheduled_for: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    if scope_type not in _VALID_SCOPE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope_type must be one of all, node, location",
        )

    parsed_scheduled = _parse_scheduled_for(scheduled_for)
    parsed_notes = _parse_optional_notes(notes)

    node_id: int | None = None
    location_id: int | None = None
    if scope_type == "node":
        node = _resolve_node(db, scope_node_id)
        node_id = node.id
    elif scope_type == "location":
        loc = _resolve_location(db, scope_location_id)
        location_id = loc.id
    # scope_type == "all" → both ids stay None (form inputs ignored).

    st = StockTake(
        scope_node_id=node_id,
        scope_location_id=location_id,
        scheduled_for=parsed_scheduled,
        notes=parsed_notes,
        created_by=user.id,
    )
    db.add(st)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="stock_take.created",
        entity_type="stock_take",
        entity_id=st.id,
        before=None,
        after={
            "scope_node_id": node_id,
            "scope_location_id": location_id,
            "scheduled_for": parsed_scheduled.isoformat(),
            "notes": parsed_notes,
        },
    )
    db.commit()
    _flash(
        request,
        f"Stock take scheduled for {parsed_scheduled.isoformat()}.",
    )
    return RedirectResponse(url="/admin/stock-takes", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# ST2: detail page + start + counts
# ---------------------------------------------------------------------------


def _get_stock_take_or_404(db: Session, st_id: int) -> StockTake:
    st = db.get(StockTake, st_id)
    if st is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="stock take not found")
    return st


@router.get("/{stock_take_id}", response_class=HTMLResponse)
def stock_take_detail(
    request: Request,
    stock_take_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Detail page — three render branches gated on status.

    ``scheduled``  → scope summary + items-in-scope table + Start counting form.
    ``in_progress`` → count form with per-line inputs + progress summary.
    ``completed``   → read-only summary (forward-looking; ST3 writes this state).
    """
    st = _get_stock_take_or_404(db, stock_take_id)
    ctx = _detail_context(db, st, user)
    return templates.TemplateResponse(request, "stock_take_detail.html", ctx)


@router.post("/{stock_take_id}/start")
def start_stock_take(
    request: Request,
    stock_take_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Freeze in-scope items into ``StockTakeLine`` rows + flip ``started_at``.

    Validation order is atomic — every 400 lands *before* any DB write so a
    failed start leaves no orphan lines and no audit trail.
    """
    st = _get_stock_take_or_404(db, stock_take_id)
    if not _can_start(st):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "stock take is already started"
                if st.started_at is not None and st.completed_at is None
                else "stock take is already completed"
            ),
        )

    items = _resolve_scope_items(db, st)
    if not items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="scope contains no items to count",
        )

    started_at = datetime.now(UTC)
    st.started_at = started_at

    line_snapshot: list[dict[str, Any]] = []
    for item in items:
        line = StockTakeLine(
            stock_take_id=st.id,
            item_id=item.id,
            system_qty=item.current_qty,
        )
        db.add(line)
        db.flush()
        line_snapshot.append(
            {
                "line_id": line.id,
                "item_id": item.id,
                "system_qty": str(item.current_qty),
            }
        )

    record_audit(
        db,
        actor=user,
        action="stock_take.started",
        entity_type="stock_take",
        entity_id=st.id,
        before={"started_at": None},
        after={
            "started_at": started_at.isoformat(),
            "lines": line_snapshot,
        },
    )
    db.commit()
    _flash(
        request,
        f"Stock take started — {len(items)} item(s) to count.",
    )
    return RedirectResponse(
        url=f"/admin/stock-takes/{st.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/{stock_take_id}/counts")
async def update_stock_take_counts(
    request: Request,
    stock_take_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Save per-line ``counted_qty`` + derived ``variance``.

    Per-line ``counted_<line_id>`` come in via ``request.form()`` because the
    line ids aren't known at function-signature time. Same precedent as
    PO2b's edit + PO5's receive routes. Lines whose key is *missing* from the
    form are left unchanged — defends against stale tabs that hold removed
    line ids.

    Sparse diff: only lines whose ``counted_qty`` actually changed are
    written + audited; a no-op submit writes no audit row + flashes "no
    changes" (same posture as PO2b's no-op submit).
    """
    st = _get_stock_take_or_404(db, stock_take_id)
    if not _can_count(st):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stock take is not in progress; start counting first",
        )

    form_data = await request.form()
    rows = _lines_with_items(db, st.id)

    # First pass: parse every present field so a single bad input rejects
    # before any mutation.
    parsed: list[tuple[StockTakeLine, Decimal | None, bool]] = []
    for line, _item in rows:
        key = f"counted_{line.id}"
        if key not in form_data:
            # Missing key → leave unchanged.
            parsed.append((line, line.counted_qty, False))
            continue
        raw = str(form_data[key] or "")
        new_counted = _parse_optional_count(raw, line_id=line.id)
        parsed.append((line, new_counted, True))

    # Second pass: build the sparse diff before any DB mutation. Decimal
    # comparison is scale-aware (``Decimal("10") == Decimal("10.0000")``), so
    # the diff correctly identifies a no-op submit.
    line_before: list[dict[str, Any]] = []
    line_after: list[dict[str, Any]] = []
    changed: list[tuple[StockTakeLine, Decimal | None, Decimal | None]] = []
    for line, new_counted, was_present in parsed:
        if not was_present:
            continue
        if line.counted_qty == new_counted:
            continue
        new_variance = _compute_variance(new_counted, line.system_qty)
        line_before.append(
            {
                "line_id": line.id,
                "counted_qty": (str(line.counted_qty) if line.counted_qty is not None else None),
            }
        )
        line_after.append(
            {
                "line_id": line.id,
                "counted_qty": (str(new_counted) if new_counted is not None else None),
                "variance": (str(new_variance) if new_variance is not None else None),
            }
        )
        changed.append((line, new_counted, new_variance))

    if not changed:
        _flash(request, "No changes.")
        return RedirectResponse(
            url=f"/admin/stock-takes/{st.id}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    for line, new_counted, new_variance in changed:
        line.counted_qty = new_counted
        line.variance = new_variance

    record_audit(
        db,
        actor=user,
        action="stock_take.counted",
        entity_type="stock_take",
        entity_id=st.id,
        before={"lines": line_before},
        after={"lines": line_after},
    )
    db.commit()
    _flash(
        request,
        f"Saved {len(changed)} count(s).",
    )
    return RedirectResponse(
        url=f"/admin/stock-takes/{st.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


# ---------------------------------------------------------------------------
# ST3: commit variances as adjustment movements
# ---------------------------------------------------------------------------


@router.post("/{stock_take_id}/commit")
async def commit_stock_take(
    request: Request,
    stock_take_id: int,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Commit non-zero variances as adjustment movements (ST3).

    For every line with ``variance != 0``: builds one
    :class:`~app.models.StockMovement` of type ``ADJUSTMENT`` with the
    stock-take id stamped on it (FK active since ST1's migration), routes
    through the cost engine (``record_receipt`` for positive variance with
    source ``POSITIVE_ADJUSTMENT``; ``consume_fifo`` for negative variance),
    and flips ``StockTakeLine.committed=True``. Sets
    ``StockTake.completed_at`` and writes a ``stock_take.committed`` audit
    row carrying the per-movement snapshot.

    Atomic-on-error: if any negative-variance line raises
    :class:`~app.cost_engine.InsufficientStockError`, the whole transaction
    is rolled back (no movement / layer / consumption rows; ``committed``
    stays False on every line; ``completed_at`` stays None) and the detail
    page re-renders with the error block + the operator's typed unit-cost
    inputs preserved. Same atomic-on-raise contract as M3 / M4.
    """
    st = _get_stock_take_or_404(db, stock_take_id)
    if not _can_commit(st):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="stock take is not in progress; commit requires an in-progress count",
        )

    rows = _lines_with_items(db, st.id)
    variance_rows = _lines_with_non_zero_variance(rows)
    if not variance_rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="no variances to commit",
        )

    form_data = await request.form()
    # First pass: parse every per-line unit_cost_<id> input — for *positive*
    # variance lines it's required; for negative variance lines blank → None
    # (consumption price is per-layer; the input is ignored). This pass runs
    # before any DB write so a parse failure rejects atomically.
    parsed_unit_costs: dict[int, Decimal | None] = {}
    raw_unit_costs: dict[int, str] = {}
    for line, _item in variance_rows:
        # Variance is non-None on every variance row by the filter above.
        assert line.variance is not None
        key = f"unit_cost_{line.id}"
        raw = str(form_data.get(key) or "")
        raw_unit_costs[line.id] = raw.strip()
        required = line.variance > 0
        parsed_unit_costs[line.id] = _parse_unit_cost_for_commit(
            raw, line_id=line.id, required=required
        )

    # Second pass: build movements + layers / consumptions through the engine.
    # If the negative path raises ``InsufficientStockError`` we roll back the
    # entire transaction (every successful prior engine call this batch
    # included) so a single shortage can't half-commit a stock take.
    committed_at = datetime.now(UTC)
    movement_snapshots: list[dict[str, Any]] = []

    for line, item in variance_rows:
        assert line.variance is not None
        direction = "increase" if line.variance > 0 else "decrease"
        qty_abs = line.variance if line.variance > 0 else -line.variance
        movement = StockMovement(
            item_id=item.id,
            type=MovementType.ADJUSTMENT,
            qty=qty_abs,
            user_id=user.id,
            reason=f"stock take #{st.id}",
            note=None,
            stock_take_id=st.id,
        )
        db.add(movement)
        db.flush()

        if direction == "increase":
            unit_cost = parsed_unit_costs[line.id]
            assert unit_cost is not None  # required=True path
            record_receipt(
                db,
                item=item,
                qty=qty_abs,
                unit_cost=unit_cost,
                source=CostLayerSource.POSITIVE_ADJUSTMENT,
                movement=movement,
                received_at=committed_at,
            )
        else:
            try:
                consume_fifo(db, item=item, qty=qty_abs, movement=movement)
            except InsufficientStockError as exc:
                db.rollback()
                # Re-load the stock take after rollback.
                st = _get_stock_take_or_404(db, stock_take_id)
                error = (
                    f"Not enough stock for {item.sku}: requested "
                    f"{exc.requested}, only {exc.available} available."
                )
                ctx = _detail_context(
                    db,
                    st,
                    user,
                    commit_error=error,
                    commit_form_values=raw_unit_costs,
                )
                return templates.TemplateResponse(
                    request,
                    "stock_take_detail.html",
                    ctx,
                    status_code=status.HTTP_400_BAD_REQUEST,
                )

        line.committed = True
        movement_snapshots.append(
            {
                "line_id": line.id,
                "item_id": item.id,
                "movement_id": movement.id,
                "variance": str(line.variance),
                "direction": direction,
                "unit_cost": (str(parsed_unit_costs[line.id]) if direction == "increase" else None),
                "total_cost": (
                    str(movement.total_cost) if movement.total_cost is not None else None
                ),
            }
        )

    st.completed_at = committed_at

    record_audit(
        db,
        actor=user,
        action="stock_take.committed",
        entity_type="stock_take",
        entity_id=st.id,
        before={"completed_at": None},
        after={
            "completed_at": committed_at.isoformat(),
            "movements": movement_snapshots,
        },
    )
    db.commit()
    _flash(
        request,
        f"Stock take committed — {len(movement_snapshots)} adjustment(s) recorded.",
    )
    return RedirectResponse(
        url=f"/admin/stock-takes/{st.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )
