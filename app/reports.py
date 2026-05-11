"""Reports surface (R4 — stock-take variance trend; R5 — CSV export).

Read-only reports that aggregate engine + history outputs. R4 is the first
report on this prefix — a per-stock-take variance trend showing how far the
workshop's counts drifted from the system over a rolling window. R5 adds a
``?format=csv`` branch on the same route so the report is downloadable as
a spreadsheet.

Route surface (mounted at ``/admin/reports``):

- ``GET /admin/reports/variance-trend[?days=N&format=csv|html]`` — Manager
  + Office. Renders a per-stock-take aggregation of committed line variances
  over the last ``days`` days (default 90, clamped [1, 365]; bad /
  out-of-range coerces to the default — same posture as R1's ``top_days``).
  ``?format=csv`` triggers the CSV branch; anything else (blank, ``html``,
  garbage) renders HTML — same silent-coerce posture as the ``days`` param.

The report is read-only by design — no audit, no mutations, no engine touches.
The numbers come from joins against ``stock_takes`` + ``stock_take_lines`` +
``taxonomy_nodes`` + ``locations``. Only **completed** stock takes contribute
(``completed_at IS NOT NULL``); only **committed** lines on those stock takes
contribute (``committed=True AND variance != 0``). The intent is "what was
actioned through the cost engine", not "what was observed but not yet
adjusted" — the audit log of ``stock_take.committed`` rows is the canonical
record this report rolls up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import (
    Location,
    Role,
    StockTake,
    StockTakeLine,
    TaxonomyNode,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/reports", tags=["reports"])


# ---------------------------------------------------------------------------
# Defaults + helpers
# ---------------------------------------------------------------------------

_DEFAULT_DAYS = 90
_DAYS_MIN = 1
_DAYS_MAX = 365


def _coerce_days(raw: str | None) -> int:
    """Return a valid window size, coercing non-int / out-of-range to default.

    Same posture as R1's ``_coerce_top_days``: a stale shared link with
    ``?days=foo`` lands on the default rather than 400.
    """
    if raw is None or raw == "":
        return _DEFAULT_DAYS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_DAYS
    if value < _DAYS_MIN or value > _DAYS_MAX:
        return _DEFAULT_DAYS
    return value


def _window_cutoff(days: int) -> datetime:
    """Lower bound on ``StockTake.completed_at`` for the report window."""
    return datetime.now(UTC) - timedelta(days=days)


_ZERO = Decimal("0")


def _aggregate_lines(lines: list[StockTakeLine]) -> dict[str, Any]:
    """Roll up a list of stock-take lines into a per-stock-take summary.

    Only ``committed=True`` lines with a non-zero ``variance`` contribute.
    Uncommitted lines (variance recorded but not actioned) and zero-variance
    lines are excluded — the report tracks "what was actioned", not "what was
    observed but not adjusted".

    Output keys:

    - ``lines_with_variance``: count of contributing lines.
    - ``positive_variance``: sum of ``variance`` across lines where it is > 0.
    - ``negative_variance_abs``: sum of ``abs(variance)`` across lines where
      it is < 0 (the absolute magnitude — easier to read than a signed sum).
    - ``net_variance``: signed sum of variance (positive contributions cancel
      negative ones — useful as a "did we drift up or down on net" signal).
    - ``abs_variance``: sum of ``abs(variance)`` across all contributing lines
      (the total movement, irrespective of direction — useful as a "how much
      counting error happened" signal).
    """
    lines_with_variance = 0
    positive = _ZERO
    negative_abs = _ZERO
    net = _ZERO
    absolute = _ZERO
    for line in lines:
        if not line.committed:
            continue
        if line.variance is None or line.variance == 0:
            continue
        lines_with_variance += 1
        net += line.variance
        absolute += abs(line.variance)
        if line.variance > 0:
            positive += line.variance
        else:
            negative_abs += abs(line.variance)
    return {
        "lines_with_variance": lines_with_variance,
        "positive_variance": positive,
        "negative_variance_abs": negative_abs,
        "net_variance": net,
        "abs_variance": absolute,
    }


def _combine_aggregates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce the per-stock-take aggregates into an overall totals dict."""
    totals = {
        "stock_take_count": len(rows),
        "lines_with_variance": 0,
        "positive_variance": _ZERO,
        "negative_variance_abs": _ZERO,
        "net_variance": _ZERO,
        "abs_variance": _ZERO,
    }
    for row in rows:
        agg = row["aggregate"]
        totals["lines_with_variance"] += agg["lines_with_variance"]
        totals["positive_variance"] += agg["positive_variance"]
        totals["negative_variance_abs"] += agg["negative_variance_abs"]
        totals["net_variance"] += agg["net_variance"]
        totals["abs_variance"] += agg["abs_variance"]
    return totals


def _scope_label(st: StockTake, *, node: TaxonomyNode | None, location: Location | None) -> str:
    """Human-readable scope label.

    Local copy of ``app.stock_takes._scope_label`` to keep the modules
    decoupled — same shape, same outputs. If the rule changes, both must move
    in lockstep; flagged as a duplication-tax acceptable at v1 scale.
    """
    if st.scope_node_id is not None and node is not None:
        return f"Category: {node.name}"
    if st.scope_location_id is not None and location is not None:
        return f"Location: {location.name}"
    return "All items"


def _load_trend_rows(db: Session, *, days: int) -> list[dict[str, Any]]:
    """Build view-shaped rows for the variance-trend table.

    Three round-trips: stock takes filtered by completion + window, then a
    batched lookup of taxonomy nodes / locations / lines. At v1 scale (a
    handful of completed stock takes per quarter) this is sub-millisecond.
    """
    cutoff = _window_cutoff(days)
    stocktakes = list(
        db.execute(
            select(StockTake)
            .where(StockTake.completed_at.is_not(None))
            .where(StockTake.completed_at >= cutoff)
            .order_by(StockTake.completed_at.desc(), StockTake.id.desc())
        )
        .scalars()
        .all()
    )
    if not stocktakes:
        return []

    node_ids = {st.scope_node_id for st in stocktakes if st.scope_node_id is not None}
    loc_ids = {st.scope_location_id for st in stocktakes if st.scope_location_id is not None}
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

    # Batched lines fetch. Group in Python by ``stock_take_id``.
    st_ids = [st.id for st in stocktakes]
    lines_by_st: dict[int, list[StockTakeLine]] = {st_id: [] for st_id in st_ids}
    for line in (
        db.execute(select(StockTakeLine).where(StockTakeLine.stock_take_id.in_(st_ids)))
        .scalars()
        .all()
    ):
        lines_by_st.setdefault(line.stock_take_id, []).append(line)

    rows: list[dict[str, Any]] = []
    for st in stocktakes:
        node = nodes.get(st.scope_node_id) if st.scope_node_id is not None else None
        loc = locations.get(st.scope_location_id) if st.scope_location_id is not None else None
        agg = _aggregate_lines(lines_by_st.get(st.id, []))
        rows.append(
            {
                "id": st.id,
                "scope_label": _scope_label(st, node=node, location=loc),
                "scheduled_for": st.scheduled_for,
                "completed_at": st.completed_at,
                "aggregate": agg,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


_CSV_HEADERS: list[str] = [
    "stock_take_id",
    "scope",
    "scheduled_for",
    "completed_at",
    "lines_with_variance",
    "positive_variance",
    "negative_variance_abs",
    "net_variance",
    "abs_variance",
]


def _csv_rows_for_trend(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Map view-shaped trend rows to CSV cell values.

    The shape mirrors the HTML table one-for-one — same nine columns, same
    order. Decimal / datetime / date coercion is handled by ``csv_response``;
    we just hand it the raw values.
    """
    out: list[list[Any]] = []
    for row in rows:
        agg = row["aggregate"]
        out.append(
            [
                row["id"],
                row["scope_label"],
                row["scheduled_for"],
                row["completed_at"],
                agg["lines_with_variance"],
                agg["positive_variance"],
                agg["negative_variance_abs"],
                agg["net_variance"],
                agg["abs_variance"],
            ]
        )
    return out


@router.get("/variance-trend")
def variance_trend(
    request: Request,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Render the variance trend report (HTML default; ``?format=csv`` for CSV)."""
    days = _coerce_days(request.query_params.get("days"))
    rows = _load_trend_rows(db, days=days)
    if (
        resp := csv_branch(
            request.query_params.get("format", ""),
            filename=f"variance_trend_{days}d.csv",
            headers=_CSV_HEADERS,
            rows=_csv_rows_for_trend(rows),
        )
    ) is not None:
        return resp
    totals = _combine_aggregates(rows)
    return templates.TemplateResponse(
        request,
        "variance_trend.html",
        {
            "current_user": user,
            "days": days,
            "rows": rows,
            "totals": totals,
        },
    )
