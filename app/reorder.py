"""Reorder dashboard (PO1).

Read-only surface listing items where ``current_qty <= reorder_threshold``,
grouped by supplier. The first slice of the PO/reorder track. No new tables,
no audit, no movement type — exercises the existing supplier + threshold +
``current_qty`` columns. Unblocks PO2 (draft PO from a low-stock selection),
which will write against the same query.

Route surface (mounted at ``/admin/reorder``):

- ``GET /admin/reorder`` — Manager + Office. Renders the dashboard.

The route is read-only by design. PO2 lands the first write surface (draft PO
creation); reading low-stock data is a sufficiently different concern from
*acting* on it that splitting them is the right shape — and matches MISSION
§3's split between "items below threshold appear on a reorder dashboard" and
"generate a draft PO grouped by supplier from low-stock items".

The query: ``Item LEFT JOIN Supplier`` on ``Item.supplier_id``, where the item
is active (``archived_at IS NULL``) and at-or-below threshold
(``current_qty <= reorder_threshold``). The ``<=`` is deliberate: an item at
*exactly* the threshold is at the trigger point, not safely above it. Items
with ``supplier_id IS NULL`` group under a synthetic "(no supplier)" bucket
so the user sees them alongside everything else needing reorder rather than
having them silently disappear. Archived suppliers still group their items
(the trigger is item-side, not supplier-side) but the supplier label carries
an "(archived)" suffix.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import Item, Role, Supplier, User
from app.template_env import templates

router = APIRouter(prefix="/admin/reorder", tags=["reorder"])


_NO_SUPPLIER_LABEL = "(no supplier)"


_REORDER_CSV_HEADERS: list[str] = [
    "supplier_id",
    "supplier_name",
    "supplier_archived",
    "item_id",
    "sku",
    "name",
    "unit",
    "current_qty",
    "threshold",
    "reorder_qty",
    "deficit",
]


def _csv_rows_for_reorder(groups: list[dict[str, Any]]) -> list[list[object]]:
    """Flatten the grouped reorder view into one CSV row per item.

    The HTML template groups items by supplier into sections; the CSV preserves
    the supplier-grouping context as columns so a downstream consumer can
    re-group / filter without losing the relationship. ``supplier_id`` is
    empty for the no-supplier bucket; ``supplier_name`` then renders the
    ``(no supplier)`` literal that matches the HTML label. Decimals coerce
    via ``csv_response``'s default ``str(value)`` so ``5.0000`` survives.
    """
    rows: list[list[object]] = []
    for group in groups:
        supplier_id = group["supplier_id"]
        supplier_name = group["supplier_label"]
        supplier_archived = group["supplier_archived"]
        for r in group["rows"]:
            item: Item = r["item"]
            rows.append(
                [
                    supplier_id,
                    supplier_name,
                    supplier_archived,
                    item.id,
                    item.sku,
                    item.name,
                    item.unit,
                    r["current_qty"],
                    r["threshold"],
                    r["reorder_qty"],
                    r["deficit"],
                ]
            )
    return rows


def _build_groups(db: Session) -> list[dict[str, Any]]:
    """Group at-or-below-threshold items by supplier.

    Output is a list of group dicts in display order (alphabetical by supplier
    name, then the "(no supplier)" bucket last). Each group carries
    ``supplier_id`` (``None`` for the no-supplier bucket), ``supplier_label``,
    ``supplier_archived`` (``False`` for active suppliers and the no-supplier
    bucket), and ``rows`` — a list of view-shaped dicts per item, ordered by
    SKU within the group.
    """
    # ``reorder_threshold > 0`` filters out the noise case: a freshly-created
    # item with ``current_qty = 0`` and ``threshold = 0`` would otherwise
    # qualify (0 ≤ 0) and render a useless "suggested 0, deficit 0" row.
    # Items the manager hasn't decided a threshold on yet shouldn't pollute
    # the dashboard. Once a threshold is set (> 0), the ``≤`` predicate
    # surfaces the item the moment stock drops to or below it.
    stmt = (
        select(Item, Supplier)
        .outerjoin(Supplier, Item.supplier_id == Supplier.id)
        .where(Item.archived_at.is_(None))
        .where(Item.reorder_threshold > 0)
        .where(Item.current_qty <= Item.reorder_threshold)
        .order_by(Item.sku)
    )

    # Bucket items by supplier_id (or None) so we can render a section per
    # supplier. Iterating once over the result keeps it a single round-trip.
    buckets: dict[int | None, dict[str, Any]] = {}
    for item, supplier in db.execute(stmt).all():
        sup_id = supplier.id if supplier is not None else None
        if sup_id not in buckets:
            if supplier is None:
                buckets[sup_id] = {
                    "supplier_id": None,
                    "supplier_label": _NO_SUPPLIER_LABEL,
                    "supplier_archived": False,
                    "rows": [],
                }
            else:
                archived = supplier.archived_at is not None
                label = f"{supplier.name} (archived)" if archived else supplier.name
                buckets[sup_id] = {
                    "supplier_id": supplier.id,
                    "supplier_label": label,
                    "supplier_archived": archived,
                    "rows": [],
                }
        buckets[sup_id]["rows"].append(
            {
                "item": item,
                "current_qty": item.current_qty,
                "threshold": item.reorder_threshold,
                "reorder_qty": item.reorder_qty,
                "deficit": item.reorder_threshold - item.current_qty,
            }
        )

    # Display order: suppliers alphabetically, then the no-supplier bucket
    # last. Sorting on the loaded label (without the "(archived)" suffix) so
    # an archived supplier doesn't sort to the bottom of the supplier list.
    sup_groups = [g for g in buckets.values() if g["supplier_id"] is not None]
    sup_groups.sort(key=lambda g: g["supplier_label"].removesuffix(" (archived)"))
    no_sup = [g for g in buckets.values() if g["supplier_id"] is None]
    return sup_groups + no_sup


@router.get("")
def reorder_dashboard(
    request: Request,
    format: str = "",
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """List active items at-or-below their reorder threshold, grouped by supplier."""
    groups = _build_groups(db)
    if (
        resp := csv_branch(
            format,
            filename="reorder.csv",
            headers=_REORDER_CSV_HEADERS,
            rows=_csv_rows_for_reorder(groups),
        )
    ) is not None:
        return resp

    return templates.TemplateResponse(
        request,
        "reorder_dashboard.html",
        {
            "current_user": user,
            "groups": groups,
        },
    )
