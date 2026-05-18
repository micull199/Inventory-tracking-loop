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
    Item,
    ItemStone,
    Location,
    Metal,
    MetalSpotPrice,
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


# ---------------------------------------------------------------------------
# Stone-cost-per-ring report (spec §10.3, Strategy A "loaded cost")
# ---------------------------------------------------------------------------

_STONE_COST_CSV_HEADERS: list[str] = [
    "item_id",
    "sku",
    "name",
    "mount_cost",
    "stone_count",
    "loaded_stones_cost",
    "owned_stones_cost",
    "loaded_cost",
    "owned_cost",
]


def _load_stone_cost_rows(db: Session) -> list[dict[str, Any]]:
    """Build the per-item loaded / owned cost rows.

    Only items with at least one active ``item_stones`` linkage make
    the list — the report is "stones-on-rings", not "every item".
    The ``ix_item_stones_active_item_id`` covering partial index keeps
    the join cheap as the linkage history grows.
    """
    from app.stones import compute_item_stone_costs

    items = list(
        db.execute(
            select(Item)
            .join(ItemStone, ItemStone.item_id == Item.id)
            .where(ItemStone.unset_at.is_(None))
            .where(Item.archived_at.is_(None))
            .distinct()
            .order_by(Item.sku)
        ).scalars().all()
    )
    return [
        {"item": item, **compute_item_stone_costs(db, item)}
        for item in items
    ]


def _csv_rows_for_stone_cost(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            r["item"].id,
            r["item"].sku,
            r["item"].name,
            r["mount_cost"],
            r["stone_count"],
            r["loaded_stones_cost"],
            r["owned_stones_cost"],
            r["loaded_cost"],
            r["owned_cost"],
        ]
        for r in rows
    ]


@router.get("/stone-cost-per-ring")
def stone_cost_per_ring(
    request: Request,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Per-item loaded + owned cost summary.

    Loaded cost = mount + every active stone's acquisition cost.
    Owned cost = mount + owned active stones only (excludes memo +
    consignment, which belong to the supplier until paid for). The
    report proves the spec §10.3 Strategy A model — costs stay
    separated and the engine doesn't need to know about stones.

    HTML by default; ``?format=csv`` for a spreadsheet download.
    """
    rows = _load_stone_cost_rows(db)
    if (
        resp := csv_branch(
            request.query_params.get("format", ""),
            filename="stone_cost_per_ring.csv",
            headers=_STONE_COST_CSV_HEADERS,
            rows=_csv_rows_for_stone_cost(rows),
        )
    ) is not None:
        return resp

    # Totals row for the HTML view's footer.
    totals = {
        "mount_cost": sum((r["mount_cost"] for r in rows), Decimal("0")),
        "stone_count": sum(r["stone_count"] for r in rows),
        "loaded_stones_cost": sum(
            (r["loaded_stones_cost"] for r in rows), Decimal("0")
        ),
        "owned_stones_cost": sum(
            (r["owned_stones_cost"] for r in rows), Decimal("0")
        ),
        "loaded_cost": sum((r["loaded_cost"] for r in rows), Decimal("0")),
        "owned_cost": sum((r["owned_cost"] for r in rows), Decimal("0")),
    }
    return templates.TemplateResponse(
        request,
        "stone_cost_per_ring.html",
        {
            "current_user": user,
            "rows": rows,
            "totals": totals,
        },
    )


# ---------------------------------------------------------------------------
# Metal pool reconciliation (S2 — pure-metal accounting)
# ---------------------------------------------------------------------------
#
# Aggregates ``items.pure_metal_weight_g`` per ``metal_master`` row +
# the latest spot price so the manager can value the precious-metal
# pool at the most recent fixing. Stale prices (more than
# ``_METAL_PRICE_STALE_DAYS`` old) are flagged so operators know to
# enter a fresh fixing.
#
# Known limitation (documented in the report header): ``pure_metal_weight_g``
# is derived from the *primary* metal only (``items.metal_id``). Two-tone
# pieces with ``secondary_metal_id`` set contribute only to their primary;
# the spec §2.3 footprint doesn't carry separate primary / secondary weights.
# Splitting that out is a follow-up schema change, not in scope here.

_METAL_PRICE_STALE_DAYS = 7

_METAL_POOL_CSV_HEADERS: list[str] = [
    "metal_code",
    "name",
    "alloy_family",
    "purity_pct",
    "item_count",
    "total_pure_weight_g",
    "latest_price_per_gram",
    "latest_price_date",
    "days_since_price",
    "estimated_value",
]


def _load_metal_pool_rows(db: Session) -> list[dict[str, Any]]:
    """Build the per-metal pool reconciliation rows.

    Each metal in ``metal_master`` (active + archived) gets a row when
    at least one item references it as ``metal_id``. The latest spot
    price comes from ``metal_spot_prices`` ordered by ``as_of_date``
    desc. ``estimated_value`` is ``total_pure_weight_g *
    latest_price_per_gram`` — ``None`` when there's no price recorded.
    """
    from sqlalchemy import func as sa_func

    today = datetime.now(UTC).date()
    # Per-metal weight sums. The single-table aggregate keeps the query
    # cheap; the WHERE filters archived items so the pool value reflects
    # what's actually held.
    weight_rows = db.execute(
        select(
            Item.metal_id,
            sa_func.count(Item.id).label("item_count"),
            sa_func.coalesce(
                sa_func.sum(Item.pure_metal_weight_g), 0
            ).label("total_weight"),
        )
        .where(Item.metal_id.is_not(None))
        .where(Item.archived_at.is_(None))
        .group_by(Item.metal_id)
    ).all()
    by_metal_id: dict[int, dict[str, Any]] = {
        row.metal_id: {
            "item_count": int(row.item_count),
            "total_pure_weight_g": Decimal(str(row.total_weight or 0)),
        }
        for row in weight_rows
    }
    if not by_metal_id:
        return []

    metals = db.execute(
        select(Metal).where(Metal.id.in_(by_metal_id.keys()))
        .order_by(Metal.alloy_family, Metal.metal_code)
    ).scalars().all()

    rows: list[dict[str, Any]] = []
    for metal in metals:
        latest_price = db.execute(
            select(MetalSpotPrice)
            .where(MetalSpotPrice.metal_id == metal.id)
            .order_by(MetalSpotPrice.as_of_date.desc())
            .limit(1)
        ).scalar_one_or_none()
        weight = by_metal_id[metal.id]["total_pure_weight_g"]
        if latest_price is not None:
            estimated_value = weight * latest_price.price_per_gram
            days_since = (today - latest_price.as_of_date).days
            is_stale = days_since > _METAL_PRICE_STALE_DAYS
        else:
            estimated_value = None
            days_since = None
            is_stale = True  # no price at all → flag as stale
        rows.append(
            {
                "metal": metal,
                "item_count": by_metal_id[metal.id]["item_count"],
                "total_pure_weight_g": weight,
                "latest_price": latest_price,
                "days_since_price": days_since,
                "is_stale": is_stale,
                "estimated_value": estimated_value,
            }
        )
    return rows


def _csv_rows_for_metal_pool(rows: list[dict[str, Any]]) -> list[list[Any]]:
    return [
        [
            r["metal"].metal_code,
            r["metal"].name,
            r["metal"].alloy_family.value,
            r["metal"].purity_pct,
            r["item_count"],
            r["total_pure_weight_g"],
            r["latest_price"].price_per_gram if r["latest_price"] else "",
            r["latest_price"].as_of_date if r["latest_price"] else "",
            r["days_since_price"] if r["days_since_price"] is not None else "",
            r["estimated_value"] if r["estimated_value"] is not None else "",
        ]
        for r in rows
    ]


@router.get("/metal-pool")
def metal_pool(
    request: Request,
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Per-metal pool reconciliation against the latest spot price.

    Aggregates ``items.pure_metal_weight_g`` for every metal with at
    least one active item, surfaces the most recent spot fixing, and
    estimates the pool value. Stale prices (older than 7 days, or no
    price at all) render with a warning chip so operators know to
    enter a fresh fixing.

    HTML by default; ``?format=csv`` for a spreadsheet download.
    """
    rows = _load_metal_pool_rows(db)
    if (
        resp := csv_branch(
            request.query_params.get("format", ""),
            filename="metal_pool.csv",
            headers=_METAL_POOL_CSV_HEADERS,
            rows=_csv_rows_for_metal_pool(rows),
        )
    ) is not None:
        return resp
    totals = {
        "metal_count": len(rows),
        "item_count": sum(r["item_count"] for r in rows),
        "total_pure_weight_g": sum(
            (r["total_pure_weight_g"] for r in rows), Decimal("0")
        ),
        "estimated_value": sum(
            (
                r["estimated_value"]
                for r in rows
                if r["estimated_value"] is not None
            ),
            Decimal("0"),
        ),
        "stale_count": sum(1 for r in rows if r["is_stale"]),
    }
    return templates.TemplateResponse(
        request,
        "metal_pool.html",
        {
            "current_user": user,
            "rows": rows,
            "totals": totals,
            "stale_days_threshold": _METAL_PRICE_STALE_DAYS,
        },
    )
