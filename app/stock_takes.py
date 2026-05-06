"""Stock take scheduling and scope (ST1 — DoD #5).

Manager + Office surface mounted at ``/admin/stock-takes``:

- ``GET /admin/stock-takes[?show=open|completed]`` — list. ``open`` (default)
  shows scheduled + in-progress (``completed_at IS NULL``); ``completed`` shows
  rows with a non-null ``completed_at``. Unrecognised values silently coerce
  to ``open``.
- ``GET /admin/stock-takes/new`` — render the new-stock-take form.
- ``POST /admin/stock-takes`` — validate + insert + audit + redirect.

ST1 only writes the ``scheduled`` state. ST2 will add the start /
in-progress / counting flow; ST3 will add the commit-variances-as-adjustments
flow that flips the row to ``completed``.

Engine isolation: the only DB writes are the ``StockTake`` insert and the
``stock_take.created`` audit row. No ``stock_movements``, no ``cost_layers``,
no ``cost_layer_consumptions``, no ``StockTakeLine`` rows yet (those land in
ST2 when the operator starts the count).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import Location, Role, StockTake, TaxonomyNode, User
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
    stmt = (
        select(Location)
        .where(Location.archived_at.is_(None))
        .order_by(Location.name)
    )
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
    loc_ids = {
        st.scope_location_id for st in stock_takes if st.scope_location_id is not None
    }
    nodes = (
        {n.id: n for n in db.execute(
            select(TaxonomyNode).where(TaxonomyNode.id.in_(node_ids))
        ).scalars().all()}
        if node_ids
        else {}
    )
    locations = (
        {loc.id: loc for loc in db.execute(
            select(Location).where(Location.id.in_(loc_ids))
        ).scalars().all()}
        if loc_ids
        else {}
    )
    creator_ids = {st.created_by for st in stock_takes if st.created_by is not None}
    creators = (
        {u.id: u.email for u in db.execute(
            select(User).where(User.id.in_(creator_ids))
        ).scalars().all()}
        if creator_ids
        else {}
    )

    rows: list[dict[str, Any]] = []
    for st in stock_takes:
        node = nodes.get(st.scope_node_id) if st.scope_node_id is not None else None
        loc = (
            locations.get(st.scope_location_id)
            if st.scope_location_id is not None
            else None
        )
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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def list_stock_takes(
    request: Request,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    show = _coerce_show(request.query_params.get("show"))
    rows = _list_rows(db, show=show)
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
    return RedirectResponse(
        url="/admin/stock-takes", status_code=status.HTTP_303_SEE_OTHER
    )
