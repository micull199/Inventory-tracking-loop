"""FIFO cost-layer engine (M1).

Pure compute: given an item, a quantity, and a flushed-for-id stock movement,
the engine creates the appropriate cost-layer rows (for receipts) or
consumption rows (for outs / negative adjustments), updates ``item.current_qty``,
and sets ``movement.total_cost``. No HTTP, no audit-log writes, no role checks
— route handlers (M2+) handle those concerns and delegate the cost arithmetic
here.

Why a separate module: every stock-mutating route (manual in/out, adjustment,
PO receipt, stock-take commit) goes through the same FIFO arithmetic.
Centralising it means the rules ("oldest layer first", "qty_remaining decrements
not edits", "total_cost is the sum across consumed layers") have one
implementation and one test surface.

Public surface:

- :func:`record_receipt` — create a layer for an "in" / positive-adjustment
  movement.
- :func:`consume_fifo` — consume layers FIFO for an "out" / negative-adjustment
  movement.
- :func:`open_value` — sum of (qty_remaining * unit_cost) across the item's
  open layers; used by the dashboard (R1) for total inventory value.
- :class:`InsufficientStockError` — raised by :func:`consume_fifo` when the
  open layers don't sum to at least the requested qty. Route handlers map
  this to a 400.

Invariants the engine maintains:

1. Receipts only ever ADD a row to ``cost_layers`` and INCREMENT
   ``item.current_qty``.
2. Consumes only INSERT rows into ``cost_layer_consumptions``, DECREMENT
   ``cost_layers.qty_remaining``, and DECREMENT ``item.current_qty``.
3. The engine never UPDATEs ``cost_layers.qty_received``, ``unit_cost``,
   ``received_at``, or ``source``. Those columns are immutable post-insert.
4. The engine never DELETEs from any of the three tables.
5. ``movement.total_cost`` is set exactly once per engine call and equals
   ``qty * unit_cost`` for receipts or ``sum(layer.unit_cost * qty_taken)``
   across consumed layers for consumes.
6. FIFO order is ``(received_at ASC, id ASC)`` — backdated receipts can land
   ahead of existing layers, ties are broken deterministically.

The engine assumes the caller has flushed the ``StockMovement`` row so it
has an id (consumption rows FK into ``stock_movements.id`` and the source-
movement FK on ``cost_layers`` likewise needs a real id). The route layer
typically does ``db.add(movement); db.flush()`` before calling in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CostLayer,
    CostLayerConsumption,
    CostLayerSource,
    Item,
    StockMovement,
)


class InsufficientStockError(Exception):
    """Raised when an out / negative-adjustment exceeds open layer qty.

    The message includes the requested qty and the available qty so the route
    layer can surface it to the operator without re-deriving either.
    """

    def __init__(self, *, item_id: int, requested: Decimal, available: Decimal) -> None:
        self.item_id = item_id
        self.requested = requested
        self.available = available
        super().__init__(
            f"item {item_id}: cannot consume {requested}; only {available} available"
        )


def record_receipt(
    db: Session,
    *,
    item: Item,
    qty: Decimal,
    unit_cost: Decimal,
    source: CostLayerSource,
    movement: StockMovement,
    received_at: datetime | None = None,
) -> CostLayer:
    """Create a FIFO cost layer for a receipt and bump ``item.current_qty``.

    The caller must have flushed ``movement`` so its id is populated; the new
    layer's ``source_movement_id`` references it. ``movement.total_cost`` is
    set to ``qty * unit_cost``.

    ``received_at=None`` defaults to the current UTC time. An explicit
    ``received_at`` lets a backdated receipt land ahead of existing layers
    (FIFO order is by ``received_at`` then ``id``).

    Raises :class:`ValueError` on ``qty <= 0`` or ``unit_cost < 0``.
    Zero ``unit_cost`` is allowed (gifted / sample stock).
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive; got {qty}")
    if unit_cost < 0:
        raise ValueError(f"unit_cost cannot be negative; got {unit_cost}")
    if movement.id is None:
        raise ValueError(
            "movement must be flushed (have an id) before record_receipt"
        )

    layer = CostLayer(
        item_id=item.id,
        qty_received=qty,
        qty_remaining=qty,
        unit_cost=unit_cost,
        received_at=received_at if received_at is not None else datetime.now(UTC),
        source=source,
        source_movement_id=movement.id,
    )
    db.add(layer)
    db.flush()

    item.current_qty = (item.current_qty or Decimal("0")) + qty
    movement.total_cost = qty * unit_cost
    return layer


def consume_fifo(
    db: Session,
    *,
    item: Item,
    qty: Decimal,
    movement: StockMovement,
) -> Decimal:
    """Consume ``qty`` units of ``item`` from open cost layers, oldest first.

    Walks the item's layers with ``qty_remaining > 0`` in FIFO order
    (``received_at ASC, id ASC``). For each one, takes
    ``min(qty_remaining, remaining_to_consume)`` and writes a
    :class:`~app.models.CostLayerConsumption` row tied to ``movement``;
    decrements the layer's ``qty_remaining``; accumulates the cost.

    Returns the total cost-of-goods consumed (sum of ``qty_taken * unit_cost``
    across consumed layers). Side effects: ``movement.total_cost`` is set to
    that total; ``item.current_qty`` is decremented by ``qty``.

    Raises :class:`InsufficientStockError` if the open layers don't sum to at
    least ``qty``. The error is raised *before* any rows are written or
    columns mutated, so the call is atomic — the caller can roll back the
    transaction or render a 400 without partial state.

    Raises :class:`ValueError` on ``qty <= 0``.
    """
    if qty <= 0:
        raise ValueError(f"qty must be positive; got {qty}")
    if movement.id is None:
        raise ValueError(
            "movement must be flushed (have an id) before consume_fifo"
        )

    layers = list(
        db.execute(
            select(CostLayer)
            .where(CostLayer.item_id == item.id)
            .where(CostLayer.qty_remaining > 0)
            .order_by(CostLayer.received_at.asc(), CostLayer.id.asc())
        )
        .scalars()
        .all()
    )
    available = sum((layer.qty_remaining for layer in layers), Decimal("0"))
    if available < qty:
        raise InsufficientStockError(
            item_id=item.id, requested=qty, available=available
        )

    remaining = qty
    total_cost = Decimal("0")
    for layer in layers:
        if remaining <= 0:
            break
        take = layer.qty_remaining if layer.qty_remaining < remaining else remaining
        consumption = CostLayerConsumption(
            layer_id=layer.id,
            movement_id=movement.id,
            qty_consumed=take,
            unit_cost_at_consumption=layer.unit_cost,
        )
        db.add(consumption)
        layer.qty_remaining = layer.qty_remaining - take
        total_cost += take * layer.unit_cost
        remaining -= take
    db.flush()

    item.current_qty = (item.current_qty or Decimal("0")) - qty
    movement.total_cost = total_cost
    return total_cost


def open_value(db: Session, item: Item) -> Decimal:
    """Total monetary value of ``item``'s open FIFO layers.

    Returns ``sum(qty_remaining * unit_cost)`` across layers with
    ``qty_remaining > 0`` for the item. Returns ``Decimal("0")`` for an item
    with no open layers (or no layers at all).

    Pure read; no side effects. The dashboard (R1) calls this once per item
    to compute the total-inventory-value figure.
    """
    rows = (
        db.execute(
            select(CostLayer.qty_remaining, CostLayer.unit_cost)
            .where(CostLayer.item_id == item.id)
            .where(CostLayer.qty_remaining > 0)
        )
        .all()
    )
    total = Decimal("0")
    for qty_remaining, unit_cost in rows:
        total += qty_remaining * unit_cost
    return total
