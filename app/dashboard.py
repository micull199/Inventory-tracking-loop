"""Reporting dashboard (R1).

First link in DoD #7's chain. Read-only surface that aggregates outputs from
the existing engine + tables: total inventory value (FIFO), low-stock count,
open-PO count, top consumed items over a rolling window, and cost-of-goods-
consumed for a date range. The "overdue checkouts" widget is a placeholder
(0) until C-series lands; the test-id is pinned now so C-series doesn't have
to re-template.

Route surface (mounted at ``/admin/dashboard``):

- ``GET /admin/dashboard[?top_days=N&cogs_start=YYYY-MM-DD&cogs_end=YYYY-MM-DD]``
  — Manager + Office. Renders the dashboard with the active filter params.

Workshop is excluded from the route (MISSION §3 "Workshop... cannot see
aggregated cost data or reports"). Admin is allowed via the role-bypass on
``require_role``.

The route is read-only: no audit, no movement type, no new tables. All
aggregations come from joins against the existing cost engine columns
(``cost_layers``, ``cost_layer_consumptions``, ``stock_movements``) plus
``items`` and ``purchase_orders``. At v1 scale the queries are single-shot
SELECT aggregations — no N+1 concerns.

COGS definition (MISSION §3): "sum of the cost of all out and negative-
adjustment movements over a date range". Negative adjustments don't carry
a sign on ``stock_movements.qty`` (the column is always positive — direction
is encoded by whether the movement consumed layers or created one). We
identify them by joining to ``cost_layer_consumptions``: a movement that
appears in that table consumed layers, regardless of its ``type``.

Top-consumed window default is 30 days. Operators rarely want a different
window in v1; the ``?top_days=N`` URL param + form let them widen / narrow
without leaving the page. Out-of-range / non-int values silently coerce to
the default — same posture as M6's pagination clamping (a stale link is a
friendlier UX than a 400).

COGS date range default is the last 30 days (``now() - 30d`` to ``now()``).
Bad-format dates 400 (same posture as PO2b's edit-date validation: the
operator typed something that isn't ISO, the form should refuse rather than
silently coerce).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_session
from app.models import (
    CostLayer,
    CostLayerConsumption,
    Item,
    MovementType,
    POStatus,
    PurchaseOrder,
    Role,
    StockMovement,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/dashboard", tags=["dashboard"])


# ---------------------------------------------------------------------------
# Defaults + helpers
# ---------------------------------------------------------------------------

_DEFAULT_TOP_DAYS = 30
_TOP_DAYS_MIN = 1
_TOP_DAYS_MAX = 365
_TOP_LIMIT = 10
_DEFAULT_COGS_DAYS = 30
_OPEN_PO_STATUSES = (
    POStatus.DRAFT,
    POStatus.SENT,
    POStatus.PARTIALLY_RECEIVED,
)


def _coerce_top_days(raw: str | None) -> int:
    """Return a valid top_days, coercing non-int / out-of-range to default.

    A stale shared link with ``?top_days=foo`` lands on the default rather
    than a 400, matching M6's pagination posture.
    """
    if raw is None or raw == "":
        return _DEFAULT_TOP_DAYS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_TOP_DAYS
    if value < _TOP_DAYS_MIN or value > _TOP_DAYS_MAX:
        return _DEFAULT_TOP_DAYS
    return value


def _parse_cogs_date(raw: str | None, *, field_name: str) -> date | None:
    """Parse an ISO date or 400. ``None`` / blank → ``None`` (use default)."""
    if raw is None:
        return None
    text = raw.strip()
    if text == "":
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be YYYY-MM-DD",
        ) from exc


def _resolve_cogs_range(
    cogs_start: date | None, cogs_end: date | None
) -> tuple[date, date]:
    """Resolve the COGS date range. Defaults: last 30 days through today."""
    today = datetime.now(UTC).date()
    end = cogs_end if cogs_end is not None else today
    start = (
        cogs_start
        if cogs_start is not None
        else end - timedelta(days=_DEFAULT_COGS_DAYS)
    )
    return start, end


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------


def _total_inventory_value(db: Session) -> Decimal:
    """Sum ``qty_remaining * unit_cost`` across active items' open layers.

    Archived items don't contribute — they're not on the books. A fully-
    consumed layer (``qty_remaining=0``) contributes 0 by arithmetic, no
    extra filter needed (and including the row keeps the query single-shot).
    """
    stmt = (
        select(
            func.coalesce(
                func.sum(CostLayer.qty_remaining * CostLayer.unit_cost),
                0,
            )
        )
        .select_from(CostLayer)
        .join(Item, CostLayer.item_id == Item.id)
        .where(Item.archived_at.is_(None))
    )
    total = db.execute(stmt).scalar_one()
    if isinstance(total, Decimal):
        return total
    return Decimal(str(total))


def _low_stock_count(db: Session) -> int:
    """Count active items at-or-below their reorder threshold."""
    stmt = (
        select(func.count(Item.id))
        .where(Item.archived_at.is_(None))
        .where(Item.current_qty <= Item.reorder_threshold)
    )
    return int(db.execute(stmt).scalar_one())


def _open_pos_count(db: Session) -> int:
    """Count POs with status in (draft, sent, partially_received)."""
    stmt = select(func.count(PurchaseOrder.id)).where(
        PurchaseOrder.status.in_(_OPEN_PO_STATUSES)
    )
    return int(db.execute(stmt).scalar_one())


def _top_consumed(db: Session, *, days: int) -> list[dict[str, Any]]:
    """Top items by total OUT-movement qty over the last ``days`` days.

    Returned shape: ``[{item_id, sku, name, qty (Decimal)}]`` newest-first
    (largest sum first). LIMIT 10 — the dashboard widget is a snapshot, not a
    paginated list.
    """
    cutoff = datetime.now(UTC) - timedelta(days=days)
    qty_sum = func.sum(StockMovement.qty).label("qty_sum")
    stmt = (
        select(Item.id, Item.sku, Item.name, qty_sum)
        .select_from(StockMovement)
        .join(Item, StockMovement.item_id == Item.id)
        .where(StockMovement.type == MovementType.OUT)
        .where(StockMovement.created_at >= cutoff)
        .group_by(Item.id, Item.sku, Item.name)
        .order_by(qty_sum.desc(), Item.sku.asc())
        .limit(_TOP_LIMIT)
    )
    return [
        {"item_id": row[0], "sku": row[1], "name": row[2], "qty": row[3]}
        for row in db.execute(stmt).all()
    ]


def _cogs(db: Session, *, start: date, end: date) -> Decimal:
    """Sum of total_cost across consuming movements in [start, end].

    A "consuming" movement is one that drained cost layers — i.e. has at
    least one row in ``cost_layer_consumptions``. That covers OUT plus
    negative-adjustment ADJUSTMENT movements (both go through
    ``consume_fifo``). Adjustment increases create a ``cost_layer`` and have
    no consumption rows, so they're correctly excluded.

    The date range is interpreted as ``[start 00:00 UTC, end+1 00:00 UTC)``
    (i.e. ``end`` is inclusive of the entire day).
    """
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=UTC)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=UTC) + timedelta(
        days=1
    )
    stmt = (
        select(
            func.coalesce(
                func.sum(StockMovement.total_cost),
                0,
            )
        )
        .where(
            StockMovement.id.in_(
                select(CostLayerConsumption.movement_id).distinct()
            )
        )
        .where(StockMovement.created_at >= start_dt)
        .where(StockMovement.created_at < end_dt)
    )
    total = db.execute(stmt).scalar_one()
    if isinstance(total, Decimal):
        return total
    return Decimal(str(total))


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Render the reporting dashboard."""
    top_days_raw = request.query_params.get("top_days")
    cogs_start_raw = request.query_params.get("cogs_start")
    cogs_end_raw = request.query_params.get("cogs_end")

    top_days = _coerce_top_days(top_days_raw)
    cogs_start_in = _parse_cogs_date(cogs_start_raw, field_name="cogs_start")
    cogs_end_in = _parse_cogs_date(cogs_end_raw, field_name="cogs_end")
    cogs_start, cogs_end = _resolve_cogs_range(cogs_start_in, cogs_end_in)

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "current_user": user,
            "total_value": _total_inventory_value(db),
            "low_stock_count": _low_stock_count(db),
            "open_pos_count": _open_pos_count(db),
            "overdue_checkouts": 0,
            "top_consumed": _top_consumed(db, days=top_days),
            "top_days": top_days,
            "cogs_amount": _cogs(db, start=cogs_start, end=cogs_end),
            "cogs_start": cogs_start.isoformat(),
            "cogs_end": cogs_end.isoformat(),
        },
    )
