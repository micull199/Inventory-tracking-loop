"""Stones — allocator, CRUD admin, and lifecycle routes.

Spec §1 (architectural additions). Stones are tracked entities with
non-quantity lifecycles; every status transition writes a paired
``stone_events`` ledger row + updates the denormalised columns on
``Stone`` in one transaction. Same posture as ``items.current_qty``
from ``cost_layers``.

Module structure:

1. **Allocator** — ``allocate_stone_code`` (``STN-NNNNNN``) atomic
   ``UPDATE … RETURNING`` round-trip; mirrors the design-code allocator.
2. **Helpers** — status transition guard, event recorder, total-carat
   recalc, lifecycle-event detection on edit.
3. **CRUD routes** — list / new / create / edit / update at
   ``/admin/stones``. Edit detects cert / ownership changes and writes
   ``cert_updated`` / ``ownership_changed`` events automatically.
4. **Lifecycle routes** — set / unset / sell / lost /
   returned_to_supplier / relocate. Each writes a ledger row, updates
   ``Stone.status`` / ``current_item_id`` / ``current_location_id``,
   maintains ``Item.centre_stone_id`` and ``Item.total_carat_weight``
   where applicable.

Access: Manager + Admin only — Workshop / Office both 403 per MISSION
§3 conventions. Same role surface as items.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.app_settings_store import (
    stones_coloured_stone_ct_threshold,
    stones_cost_floor_aud,
)
from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import (
    Item,
    ItemStone,
    Location,
    Role,
    SequenceCounter,
    Stone,
    StoneEvent,
    StoneLab,
    StoneOrigin,
    StoneOwnership,
    StonePosition,
    StoneShape,
    StoneStatus,
    StoneType,
    Supplier,
    TrackingTrigger,
    User,
)
from app.template_env import templates

router = APIRouter(prefix="/admin/stones", tags=["stones"])


# ---------------------------------------------------------------------------
# Allocator
# ---------------------------------------------------------------------------

_STONE_CODE_COUNTER_NAME = "stone_code"
_STONE_CODE_PAD_WIDTH = 6


def allocate_stone_code(db: Session) -> str:
    """Atomically allocate the next ``STN-NNNNNN`` stone code.

    Mirrors ``app.designs.allocate_design_code``: single
    ``UPDATE … RETURNING`` round-trip serialises concurrent allocators
    on the shared ``sequence_counters`` row.
    """
    stmt = (
        sa.update(SequenceCounter)
        .where(SequenceCounter.name == _STONE_CODE_COUNTER_NAME)
        .values(next_value=SequenceCounter.next_value + 1)
        .returning(SequenceCounter.next_value)
    )
    result = db.execute(stmt)
    rows = result.fetchall()
    if len(rows) != 1:
        raise RuntimeError(
            "allocate_stone_code: counter row missing — was migration 0026 "
            f"applied? (expected 1 matched row, got {len(rows)})"
        )
    allocated = int(rows[0][0]) - 1
    return f"STN-{allocated:0{_STONE_CODE_PAD_WIDTH}d}"


# ---------------------------------------------------------------------------
# Transition graph (spec §1.1)
# ---------------------------------------------------------------------------
#
# - ``available → reserved | set | sold | returned_to_supplier | lost``
# - ``reserved  → available | set | sold``
# - ``set       → available (via unset) | sold (with the ring)``
# - terminals: ``sold``, ``returned_to_supplier``, ``lost`` — no automatic
#   transitions out. An admin reset would be a manual data-edit op.

_TRANSITION_GRAPH: dict[StoneStatus, set[StoneStatus]] = {
    StoneStatus.AVAILABLE: {
        StoneStatus.RESERVED,
        StoneStatus.SET,
        StoneStatus.SOLD,
        StoneStatus.RETURNED_TO_SUPPLIER,
        StoneStatus.LOST,
    },
    StoneStatus.RESERVED: {
        StoneStatus.AVAILABLE,
        StoneStatus.SET,
        StoneStatus.SOLD,
    },
    StoneStatus.SET: {
        StoneStatus.AVAILABLE,
        StoneStatus.SOLD,
    },
    StoneStatus.SOLD: set(),
    StoneStatus.RETURNED_TO_SUPPLIER: set(),
    StoneStatus.LOST: set(),
}


def _guard_transition(from_status: StoneStatus, to_status: StoneStatus) -> None:
    """Raise ``HTTPException(400)`` if the spec doesn't allow this transition.

    Routes call this before mutating ``Stone.status``. The error message
    surfaces both states so the operator can tell which leg of the graph
    they violated.
    """
    if to_status not in _TRANSITION_GRAPH.get(from_status, set()):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"stone status cannot transition from {from_status.value!r} "
                f"to {to_status.value!r}"
            ),
        )


# ---------------------------------------------------------------------------
# Event recorder + denorm maintainers
# ---------------------------------------------------------------------------


def _record_stone_event(
    db: Session,
    stone: Stone,
    *,
    event_type: str,
    actor: User | None = None,
    from_item_id: int | None = None,
    to_item_id: int | None = None,
    from_location_id: int | None = None,
    to_location_id: int | None = None,
    from_status: StoneStatus | None = None,
    to_status: StoneStatus | None = None,
    note: str | None = None,
) -> StoneEvent:
    """Append-only ledger write.

    Caller is responsible for the denormalised field updates on the
    ``Stone`` row in the same transaction — this helper just records the
    event. Returns the flushed row so audit + integration tests can
    assert against it.
    """
    event = StoneEvent(
        stone_id=stone.id,
        event_type=event_type,
        from_item_id=from_item_id,
        to_item_id=to_item_id,
        from_location_id=from_location_id,
        to_location_id=to_location_id,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor.id if actor is not None else None,
        note=note,
    )
    db.add(event)
    db.flush()
    return event


def compute_item_stone_costs(db: Session, item: Item) -> dict[str, Any]:
    """Return the loaded + owned cost components for an item.

    Spec §10.3 (locked 2026-05-18, Strategy A): stone cost stays
    *separate* from the ring's FIFO cost layers. The reporter computes:

    - ``mount_cost``: sum of ``qty_remaining * unit_cost`` across the
      item's open cost layers (the ring shell + bench labour the FIFO
      engine already tracks).
    - ``loaded_stones_cost``: sum of ``acquisition_cost`` across every
      stone currently set into the item (regardless of ownership).
    - ``owned_stones_cost``: same sum but only for ``ownership=owned``
      stones; excludes memo + consignment (which belong to the supplier
      until paid for).
    - ``loaded_cost`` = ``mount_cost + loaded_stones_cost`` — useful for
      customer-facing display and total inventory value.
    - ``owned_cost``  = ``mount_cost + owned_stones_cost`` — what UC
      actually has on the books.

    Uses the ``ix_item_stones_active_item_id`` partial index added in
    migration 0045 so the active-stones lookup is constant-time even
    on big history tables.
    """
    from sqlalchemy import func as _func

    from app.models import CostLayer

    mount_cost: Decimal = db.execute(
        select(
            _func.coalesce(
                _func.sum(CostLayer.qty_remaining * CostLayer.unit_cost),
                0,
            )
        ).where(CostLayer.item_id == item.id)
    ).scalar() or Decimal("0")
    # Postgres returns Numeric; SQLite returns int when zero — coerce.
    mount_cost = Decimal(str(mount_cost))

    stones = list(
        db.execute(
            select(Stone)
            .join(ItemStone, ItemStone.stone_id == Stone.id)
            .where(ItemStone.item_id == item.id)
            .where(ItemStone.unset_at.is_(None))
        ).scalars().all()
    )
    loaded_stones_cost = sum(
        ((s.acquisition_cost or Decimal("0")) for s in stones),
        Decimal("0"),
    )
    owned_stones_cost = sum(
        (
            (s.acquisition_cost or Decimal("0"))
            for s in stones
            if s.ownership is StoneOwnership.OWNED
        ),
        Decimal("0"),
    )
    return {
        "mount_cost": mount_cost,
        "stone_count": len(stones),
        "loaded_stones_cost": loaded_stones_cost,
        "owned_stones_cost": owned_stones_cost,
        "loaded_cost": mount_cost + loaded_stones_cost,
        "owned_cost": mount_cost + owned_stones_cost,
    }


def _recalculate_total_carat(db: Session, item: Item) -> None:
    """Re-derive ``item.total_carat_weight`` from active stones + melee.

    Sum of every tracked stone with an active ``item_stones`` row
    pointing at ``item``, plus ``item.melee_total_ct``. Called whenever
    a stone is set into / unset from an item, or whenever melee fields
    change (a future stones-on-items form).
    """
    melee = item.melee_total_ct or Decimal("0")
    tracked = db.execute(
        select(Stone.carat_weight)
        .join(ItemStone, ItemStone.stone_id == Stone.id)
        .where(ItemStone.item_id == item.id)
        .where(ItemStone.unset_at.is_(None))
    ).scalars().all()
    item.total_carat_weight = sum(tracked, melee)


# ---------------------------------------------------------------------------
# Lifecycle primitives — composed by the route handlers
# ---------------------------------------------------------------------------


def _set_stone_into_item(
    db: Session,
    stone: Stone,
    item: Item,
    *,
    position: StonePosition,
    position_index: int,
    actor: User | None,
    note: str | None = None,
) -> ItemStone:
    """Set a stone into an item slot. Writes linkage + event + denorm.

    Raises ``HTTPException(400)`` on a transition violation, an
    already-set stone, or a slot collision. The partial unique indexes
    on ``item_stones`` are belt-and-braces — the application checks
    here surface a clearer error than a deferred constraint failure.
    """
    if stone.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stone {stone.stone_code!r} is archived",
        )
    _guard_transition(stone.status, StoneStatus.SET)
    if stone.current_item_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"stone {stone.stone_code!r} is already set in another item "
                f"(id {stone.current_item_id})"
            ),
        )
    # Position slot guard.
    collision = db.execute(
        select(ItemStone)
        .where(ItemStone.item_id == item.id)
        .where(ItemStone.position == position)
        .where(ItemStone.position_index == position_index)
        .where(ItemStone.unset_at.is_(None))
    ).scalar_one_or_none()
    if collision is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"slot {position.value}[{position_index}] is occupied "
                f"by stone id {collision.stone_id}"
            ),
        )

    link = ItemStone(
        item_id=item.id,
        stone_id=stone.id,
        position=position,
        position_index=position_index,
    )
    db.add(link)
    # Flush so the recalc query below sees the new linkage row. The
    # session has ``autoflush=False`` per project convention, so pending
    # writes don't reach the SELECT otherwise.
    db.flush()

    prior_status = stone.status
    stone.status = StoneStatus.SET
    stone.current_item_id = item.id
    if position is StonePosition.CENTRE:
        item.centre_stone_id = stone.id
    _recalculate_total_carat(db, item)

    _record_stone_event(
        db,
        stone,
        event_type="set",
        actor=actor,
        to_item_id=item.id,
        from_status=prior_status,
        to_status=StoneStatus.SET,
        note=note,
    )
    db.flush()
    return link


def _unset_stone_from_item(
    db: Session,
    stone: Stone,
    *,
    actor: User | None,
    note: str | None = None,
) -> ItemStone:
    """Unset the currently-active linkage for a SET stone.

    Restores ``Stone.status = AVAILABLE``, clears ``current_item_id``,
    soft-ends the ``item_stones`` row (``unset_at = now()``), clears
    ``item.centre_stone_id`` when the unset slot was ``centre``, and
    recalculates ``item.total_carat_weight``.

    Raises ``HTTPException(400)`` if the stone isn't currently set.
    """
    if stone.status is not StoneStatus.SET or stone.current_item_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stone {stone.stone_code!r} is not currently set",
        )
    item = db.get(Item, stone.current_item_id)
    if item is None:  # pragma: no cover — FK RESTRICT prevents this
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="stone marked as set but parent item is missing",
        )
    link = db.execute(
        select(ItemStone)
        .where(ItemStone.stone_id == stone.id)
        .where(ItemStone.unset_at.is_(None))
    ).scalar_one_or_none()
    if link is None:  # pragma: no cover — denorm drift if reachable
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="stone marked as set but no active item_stones row found",
        )

    link.unset_at = datetime.now(UTC)
    was_centre = link.position is StonePosition.CENTRE
    # Flush the soft-end so the recalc query below excludes this row.
    db.flush()

    prior_status = stone.status
    stone.status = StoneStatus.AVAILABLE
    stone.current_item_id = None
    if was_centre:
        item.centre_stone_id = None
    _recalculate_total_carat(db, item)

    _record_stone_event(
        db,
        stone,
        event_type="unset",
        actor=actor,
        from_item_id=item.id,
        from_status=prior_status,
        to_status=StoneStatus.AVAILABLE,
        note=note,
    )
    db.flush()
    return link


def _terminal_transition(
    db: Session,
    stone: Stone,
    *,
    new_status: StoneStatus,
    event_type: str,
    actor: User | None,
    note: str | None = None,
) -> None:
    """Move a stone to a terminal status (``sold`` / ``lost`` / ``returned``).

    If the stone is currently SET, auto-soft-ends the linkage row first
    (spec §1.1: ``set → sold (with the ring)``). For ``lost`` /
    ``returned``, the spec requires unset first — but to keep operator
    flows simple, we run the unset path here automatically so the data
    is consistent without the operator having to do two POSTs.

    Raises ``HTTPException(400)`` on a transition violation (e.g.
    selling an already-sold stone).
    """
    prior_status = stone.status

    if stone.status is StoneStatus.SET:
        # Auto-unset so the linkage row is correctly closed and
        # ``item.centre_stone_id`` / ``total_carat_weight`` stay
        # consistent. We bypass the standard transition guard inside the
        # unset path because we're transitioning all the way through
        # AVAILABLE to the terminal state in one atomic flow.
        item = db.get(Item, stone.current_item_id) if stone.current_item_id else None
        link = db.execute(
            select(ItemStone)
            .where(ItemStone.stone_id == stone.id)
            .where(ItemStone.unset_at.is_(None))
        ).scalar_one_or_none()
        if link is not None and item is not None:
            link.unset_at = datetime.now(UTC)
            was_centre = link.position is StonePosition.CENTRE
            stone.current_item_id = None
            if was_centre:
                item.centre_stone_id = None
            # Same autoflush=False reasoning as the standalone unset path.
            db.flush()
            _recalculate_total_carat(db, item)
            # Intermediate ledger row so history shows the unset
            # explicitly before the terminal event.
            _record_stone_event(
                db,
                stone,
                event_type="unset",
                actor=actor,
                from_item_id=item.id,
                from_status=StoneStatus.SET,
                to_status=StoneStatus.AVAILABLE,
                note=note,
            )
        # Treat the stone as AVAILABLE for the guard check below.
        stone.status = StoneStatus.AVAILABLE

    _guard_transition(stone.status, new_status)
    transition_from = stone.status
    stone.status = new_status
    _record_stone_event(
        db,
        stone,
        event_type=event_type,
        actor=actor,
        from_status=prior_status if prior_status is not transition_from else transition_from,
        to_status=new_status,
        note=note,
    )
    db.flush()


def _relocate_stone(
    db: Session,
    stone: Stone,
    *,
    new_location_id: int | None,
    actor: User | None,
    note: str | None = None,
) -> StoneEvent | None:
    """Move a stone to a different location. Writes a ``relocated`` event.

    No-op when the stone is already at ``new_location_id`` (no event row
    written) — the spec's posture is "every transition writes a row",
    but a no-change relocate isn't a transition.
    """
    if stone.current_location_id == new_location_id:
        return None
    prior = stone.current_location_id
    stone.current_location_id = new_location_id
    return _record_stone_event(
        db,
        stone,
        event_type="relocated",
        actor=actor,
        from_location_id=prior,
        to_location_id=new_location_id,
        note=note,
    )


# ---------------------------------------------------------------------------
# Edit-detection helpers (cert_updated, ownership_changed)
# ---------------------------------------------------------------------------


_CERT_FIELDS: tuple[str, ...] = ("lab", "cert_number", "cert_url")
_OWNERSHIP_FIELDS: tuple[str, ...] = ("ownership", "memo_due_date")


def _detect_lifecycle_events_on_edit(
    stone: Stone, new_fields: dict[str, Any]
) -> list[str]:
    """Return event_types implied by the diff on a stone edit.

    Writes a ``cert_updated`` event whenever any of ``lab`` /
    ``cert_number`` / ``cert_url`` changes (and at least one ends up
    populated), and ``ownership_changed`` whenever ``ownership`` or
    ``memo_due_date`` changes. Both events are append-only ledger
    rows and don't gate the edit itself — they're an audit trail of
    *why* the stone's state evolved.
    """
    events: list[str] = []
    cert_changed = any(
        getattr(stone, f) != new_fields.get(f) for f in _CERT_FIELDS
    )
    if cert_changed and any(new_fields.get(f) for f in _CERT_FIELDS):
        events.append("cert_updated")
    ownership_changed = any(
        getattr(stone, f) != new_fields.get(f) for f in _OWNERSHIP_FIELDS
    )
    if ownership_changed:
        events.append("ownership_changed")
    return events


# ---------------------------------------------------------------------------
# Form parsing helpers
# ---------------------------------------------------------------------------


def _parse_optional_decimal(raw: str, field_name: str) -> Decimal | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc


def _parse_required_decimal(raw: str, field_name: str) -> Decimal:
    parsed = _parse_optional_decimal(raw, field_name)
    if parsed is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    return parsed


def _parse_optional_date(raw: str, field_name: str) -> date | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an ISO date (YYYY-MM-DD)",
        ) from exc


def _parse_enum(
    enum_cls: type, raw: str, *, field_name: str, optional: bool = False
) -> Any:
    text = (raw or "").strip()
    if not text:
        if optional:
            return None
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    try:
        return enum_cls(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown {field_name}: {text!r}",
        ) from exc


def _resolve_optional_fk(
    db: Session,
    raw: str,
    *,
    model: type,
    archived_field: str,
    label: str,
    current_id: int | None = None,
) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        target_id = int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} id must be an integer",
        ) from exc
    obj = db.get(model, target_id)
    if obj is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} id {target_id} does not exist",
        )
    archived_at = getattr(obj, archived_field, None)
    if archived_at is not None and target_id != current_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{label} is archived",
        )
    return target_id


def _resolve_required_shape(
    db: Session, raw: str, *, current_id: int | None = None
) -> int:
    resolved = _resolve_optional_fk(
        db, raw, model=StoneShape, archived_field="archived_at",
        label="stone shape", current_id=current_id,
    )
    if resolved is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="shape is required",
        )
    return resolved


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


_FIELDS: tuple[str, ...] = (
    "stone_type",
    "shape_id",
    "length_mm",
    "width_mm",
    "depth_mm",
    "carat_weight",
    "colour_grade",
    "clarity_grade",
    "cut_grade",
    "polish",
    "symmetry",
    "fluorescence",
    "lab",
    "cert_number",
    "cert_url",
    "origin",
    "treatment",
    "supplier_id",
    "ownership",
    "memo_due_date",
    "acquisition_cost",
    "acquisition_date",
    "current_location_id",
    "notes",
    # Spec §10.1: tracking trigger + override reason. Both editable
    # through the same audit-diffed path as the rest of the row.
    "tracking_trigger",
    "tracking_override_reason",
)


# Stone types treated as "coloured" for the §10.1 coloured-stone
# threshold. Diamonds + lab diamonds are excluded — they fall under
# the cert / cost rules instead. ``other`` is grouped with coloured
# stones for the conservative default.
_COLOURED_STONE_TYPES: frozenset[StoneType] = frozenset(
    {StoneType.SAPPHIRE, StoneType.RUBY, StoneType.EMERALD, StoneType.OTHER}
)


def _compute_auto_tracking_trigger(
    db: Session, fields: dict[str, Any]
) -> TrackingTrigger | None:
    """Return the auto-applicable tracking trigger, or ``None``.

    Spec §10.1 precedence:
        cert  →  coloured_stone_threshold  →  cost_threshold

    Returns ``None`` when no auto-rule applies — at which point the
    route layer demands a ``manual_override`` + reason from the
    operator. Settings are re-read from ``app_settings`` on each call
    so a tuned threshold takes effect without a redeploy.
    """
    if fields.get("lab") and fields.get("cert_number"):
        return TrackingTrigger.CERT

    stone_type = fields.get("stone_type")
    carat_weight = fields.get("carat_weight")
    if (
        stone_type in _COLOURED_STONE_TYPES
        and carat_weight is not None
        and carat_weight >= stones_coloured_stone_ct_threshold(db)
    ):
        return TrackingTrigger.COLOURED_STONE_THRESHOLD

    acquisition_cost = fields.get("acquisition_cost")
    if (
        acquisition_cost is not None
        and acquisition_cost >= stones_cost_floor_aud(db)
    ):
        return TrackingTrigger.COST_THRESHOLD

    return None


def _resolve_tracking_trigger(
    db: Session,
    fields: dict[str, Any],
    *,
    requested_trigger: str,
    requested_reason: str,
    enforce: bool,
) -> tuple[TrackingTrigger | None, str | None]:
    """Resolve the tracking trigger + override reason for a save.

    ``enforce=True`` is used on create — a stone must satisfy an
    auto-trigger OR the operator must explicitly request
    ``manual_override`` with a non-empty reason. ``enforce=False`` is
    used on edit — legacy stones (rows from before migration 0045) may
    have a NULL trigger today; the edit path lets them stay that way
    until someone backfills.

    The operator can always force-pick a trigger via the form. We honor
    the picked value (with the reason gate for ``manual_override``);
    otherwise we use the auto-computed one.
    """
    requested_trigger = (requested_trigger or "").strip()
    requested_reason = (requested_reason or "").strip()

    auto = _compute_auto_tracking_trigger(db, fields)

    # Operator picked manual_override. Validate the reason.
    if requested_trigger == TrackingTrigger.MANUAL_OVERRIDE.value:
        if not requested_reason:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "manual_override requires a tracking_override_reason "
                    "explaining why this stone is tracked"
                ),
            )
        return TrackingTrigger.MANUAL_OVERRIDE, requested_reason

    # Operator picked one of the auto-triggers explicitly — honor it
    # only if the auto-computer agrees (otherwise a typo on the form
    # could declare cert=true on a stone with no certificate).
    if requested_trigger and requested_trigger != "":
        try:
            picked = TrackingTrigger(requested_trigger)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"unknown tracking_trigger {requested_trigger!r}",
            ) from exc
        if picked is not TrackingTrigger.MANUAL_OVERRIDE and picked != auto:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"tracking_trigger {picked.value!r} doesn't match the "
                    f"stone's data — auto-detected "
                    f"{auto.value if auto else 'no trigger'}. Adjust the "
                    f"stone or pick manual_override + reason."
                ),
            )
        return picked, None

    # No operator pick — use auto.
    if auto is not None:
        return auto, None

    if enforce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "no auto-trigger fires for this stone (no cert, below carat / "
                "cost thresholds). Either complete the cert / carat / cost "
                "fields or pick tracking_trigger=manual_override + provide a "
                "tracking_override_reason."
            ),
        )
    return None, None


def _normalise(
    db: Session,
    form: dict[str, str],
    *,
    current: Stone | None = None,
    enforce_tracking_trigger: bool = True,
) -> dict[str, Any]:
    """Strip + parse the form into the stored-value shape.

    Raises ``HTTPException(400)`` on any validation failure. The
    ``current`` arg is the existing row on edit (``None`` on create) —
    used so an archived shape / supplier / location reference survives
    an edit that didn't touch the FK.

    ``enforce_tracking_trigger`` controls whether a non-null trigger is
    required: ``True`` for create (spec §10.1 — every fresh stone must
    have a known reason for being tracked), ``False`` for edit (legacy
    rows can keep their NULL trigger until someone backfills).
    """
    stone_type = _parse_enum(StoneType, form.get("stone_type") or "", field_name="stone_type")
    shape_id = _resolve_required_shape(
        db,
        form.get("shape_id") or "",
        current_id=current.shape_id if current else None,
    )
    carat_weight = _parse_required_decimal(
        form.get("carat_weight") or "", "carat_weight"
    )
    if carat_weight <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="carat_weight must be greater than zero",
        )
    ownership = _parse_enum(
        StoneOwnership, form.get("ownership") or "owned", field_name="ownership"
    )
    memo_due_date = _parse_optional_date(form.get("memo_due_date") or "", "memo_due_date")
    if ownership is StoneOwnership.MEMO and memo_due_date is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="memo_due_date is required when ownership is memo",
        )

    out: dict[str, Any] = {
        "stone_type": stone_type,
        "shape_id": shape_id,
        "length_mm": _parse_optional_decimal(form.get("length_mm") or "", "length_mm"),
        "width_mm": _parse_optional_decimal(form.get("width_mm") or "", "width_mm"),
        "depth_mm": _parse_optional_decimal(form.get("depth_mm") or "", "depth_mm"),
        "carat_weight": carat_weight,
        "colour_grade": (form.get("colour_grade") or "").strip() or None,
        "clarity_grade": (form.get("clarity_grade") or "").strip() or None,
        "cut_grade": (form.get("cut_grade") or "").strip() or None,
        "polish": (form.get("polish") or "").strip() or None,
        "symmetry": (form.get("symmetry") or "").strip() or None,
        "fluorescence": (form.get("fluorescence") or "").strip() or None,
        "lab": _parse_enum(StoneLab, form.get("lab") or "", field_name="lab", optional=True),
        "cert_number": (form.get("cert_number") or "").strip() or None,
        "cert_url": (form.get("cert_url") or "").strip() or None,
        "origin": _parse_enum(
            StoneOrigin, form.get("origin") or "natural", field_name="origin"
        ),
        "treatment": (form.get("treatment") or "").strip() or None,
        "supplier_id": _resolve_optional_fk(
            db, form.get("supplier_id") or "",
            model=Supplier, archived_field="archived_at",
            label="supplier",
            current_id=current.supplier_id if current else None,
        ),
        "ownership": ownership,
        "memo_due_date": memo_due_date,
        "acquisition_cost": _parse_optional_decimal(
            form.get("acquisition_cost") or "", "acquisition_cost"
        ),
        "acquisition_date": _parse_optional_date(
            form.get("acquisition_date") or "", "acquisition_date"
        ),
        "current_location_id": _resolve_optional_fk(
            db, form.get("current_location_id") or "",
            model=Location, archived_field="archived_at",
            label="location",
            current_id=current.current_location_id if current else None,
        ),
        "notes": (form.get("notes") or "").strip() or None,
    }
    # Tracking trigger resolution depends on every other field above,
    # so it runs last and uses the now-coerced values via ``out``.
    trigger, reason = _resolve_tracking_trigger(
        db,
        out,
        requested_trigger=form.get("tracking_trigger") or "",
        requested_reason=form.get("tracking_override_reason") or "",
        enforce=enforce_tracking_trigger,
    )
    out["tracking_trigger"] = trigger
    out["tracking_override_reason"] = reason
    return out


def _diff(stone: Stone, new: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(stone, f)
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
# Option builders (with archived-FK preservation)
# ---------------------------------------------------------------------------


def _shape_options(db: Session, *, current_id: int | None = None) -> list[dict[str, Any]]:
    rows: list[StoneShape] = list(
        db.execute(
            select(StoneShape)
            .where(StoneShape.archived_at.is_(None))
            .order_by(StoneShape.sort_order, StoneShape.name)
        ).scalars().all()
    )
    if current_id is not None and not any(s.id == current_id for s in rows):
        current = db.get(StoneShape, current_id)
        if current is not None:
            rows.append(current)
    return [
        {
            "id": s.id,
            "label": s.name + (" (archived)" if s.archived_at is not None else ""),
        }
        for s in rows
    ]


def _supplier_options(db: Session, *, current_id: int | None = None) -> list[dict[str, Any]]:
    rows: list[Supplier] = list(
        db.execute(
            select(Supplier)
            .where(Supplier.archived_at.is_(None))
            .order_by(Supplier.name)
        ).scalars().all()
    )
    if current_id is not None and not any(s.id == current_id for s in rows):
        current = db.get(Supplier, current_id)
        if current is not None:
            rows.append(current)
    return [
        {
            "id": s.id,
            "label": s.name + (" (archived)" if s.archived_at is not None else ""),
        }
        for s in rows
    ]


def _location_options(db: Session, *, current_id: int | None = None) -> list[dict[str, Any]]:
    rows: list[Location] = list(
        db.execute(
            select(Location)
            .where(Location.archived_at.is_(None))
            .order_by(Location.name)
        ).scalars().all()
    )
    if current_id is not None and not any(loc.id == current_id for loc in rows):
        current = db.get(Location, current_id)
        if current is not None:
            rows.append(current)
    return [
        {
            "id": loc.id,
            "label": loc.name + (" (archived)" if loc.archived_at is not None else ""),
        }
        for loc in rows
    ]


def _empty_form_view() -> dict[str, str]:
    return {
        "stone_type": "diamond",
        "shape_id": "",
        "length_mm": "",
        "width_mm": "",
        "depth_mm": "",
        "carat_weight": "",
        "colour_grade": "",
        "clarity_grade": "",
        "cut_grade": "",
        "polish": "",
        "symmetry": "",
        "fluorescence": "",
        "lab": "",
        "cert_number": "",
        "cert_url": "",
        "origin": "natural",
        "treatment": "",
        "supplier_id": "",
        "ownership": "owned",
        "memo_due_date": "",
        "acquisition_cost": "",
        "acquisition_date": "",
        "current_location_id": "",
        "notes": "",
        # Spec §10.1: tracking trigger left blank on the form by default
        # so the operator sees the auto-detection take effect. They only
        # set this when manually overriding.
        "tracking_trigger": "",
        "tracking_override_reason": "",
    }


def _form_view_for(stone: Stone) -> dict[str, str]:
    def _s(v: Any) -> str:
        return str(v) if v is not None else ""

    def _iso(v: date | datetime | None) -> str:
        return v.isoformat() if v is not None else ""

    return {
        "stone_type": stone.stone_type.value,
        "shape_id": str(stone.shape_id),
        "length_mm": _s(stone.length_mm),
        "width_mm": _s(stone.width_mm),
        "depth_mm": _s(stone.depth_mm),
        "carat_weight": _s(stone.carat_weight),
        "colour_grade": stone.colour_grade or "",
        "clarity_grade": stone.clarity_grade or "",
        "cut_grade": stone.cut_grade or "",
        "polish": stone.polish or "",
        "symmetry": stone.symmetry or "",
        "fluorescence": stone.fluorescence or "",
        "lab": stone.lab.value if stone.lab else "",
        "cert_number": stone.cert_number or "",
        "cert_url": stone.cert_url or "",
        "origin": stone.origin.value,
        "treatment": stone.treatment or "",
        "supplier_id": _s(stone.supplier_id),
        "ownership": stone.ownership.value,
        "memo_due_date": _iso(stone.memo_due_date),
        "acquisition_cost": _s(stone.acquisition_cost),
        "acquisition_date": _iso(stone.acquisition_date),
        "current_location_id": _s(stone.current_location_id),
        "notes": stone.notes or "",
        "tracking_trigger": stone.tracking_trigger.value if stone.tracking_trigger else "",
        "tracking_override_reason": stone.tracking_override_reason or "",
    }


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

_LIST_ORDER = case((Stone.archived_at.is_(None), 0), else_=1)


_STATUS_FILTER_VALUES: frozenset[str] = frozenset(s.value for s in StoneStatus)


@router.get("")
def list_stones(
    request: Request,
    show: str = "active",
    status_filter: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"
    selected_status = (
        status_filter if status_filter in _STATUS_FILTER_VALUES else ""
    )

    stmt = select(Stone)
    if show == "active":
        stmt = stmt.where(Stone.archived_at.is_(None))
    else:
        stmt = stmt.where(Stone.archived_at.is_not(None))
    if selected_status:
        stmt = stmt.where(Stone.status == StoneStatus(selected_status))
    stmt = stmt.order_by(_LIST_ORDER, Stone.stone_code)

    rows = list(db.execute(stmt).scalars().all())
    # Pre-resolve the shape + location names so the template doesn't
    # have to know about the joined lookup tables.
    shape_by_id: dict[int, str] = {
        s.id: s.name
        for s in db.execute(select(StoneShape)).scalars().all()
    }
    loc_by_id: dict[int, str] = {
        loc.id: loc.name
        for loc in db.execute(select(Location)).scalars().all()
    }
    item_by_id: dict[int, str] = {
        item.id: f"{item.sku} — {item.name}"
        for item in db.execute(
            select(Item).where(Item.id.in_({s.current_item_id for s in rows if s.current_item_id}))
        ).scalars().all()
    }
    view_rows = [
        {
            "stone": s,
            "shape_label": shape_by_id.get(s.shape_id, ""),
            "location_label": loc_by_id.get(s.current_location_id, "") if s.current_location_id else "",
            "item_label": item_by_id.get(s.current_item_id, "") if s.current_item_id else "",
        }
        for s in rows
    ]
    return templates.TemplateResponse(
        request,
        "stones_list.html",
        {
            "current_user": _user,
            "rows": view_rows,
            "show": show,
            "selected_status": selected_status,
            "status_options": [s.value for s in StoneStatus],
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_stone_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "stones_form.html",
        {
            "current_user": _user,
            "stone": None,
            "form": _empty_form_view(),
            "title": "New stone",
            "action": "/admin/stones",
            "shape_options": _shape_options(db),
            "supplier_options": _supplier_options(db),
            "location_options": _location_options(db),
            "stone_types": [t.value for t in StoneType],
            "labs": [lab.value for lab in StoneLab],
            "origins": [o.value for o in StoneOrigin],
            "ownerships": [o.value for o in StoneOwnership],
        },
    )


def _form_field_set() -> dict[str, str]:
    """Field name → empty string, so a route handler can ingest as kwargs.

    Defined once so create + update keep the same signature shape.
    """
    return _empty_form_view()


@router.post("")
def create_stone(
    request: Request,
    stone_type: str = Form("diamond"),
    shape_id: str = Form(""),
    length_mm: str = Form(""),
    width_mm: str = Form(""),
    depth_mm: str = Form(""),
    carat_weight: str = Form(""),
    colour_grade: str = Form(""),
    clarity_grade: str = Form(""),
    cut_grade: str = Form(""),
    polish: str = Form(""),
    symmetry: str = Form(""),
    fluorescence: str = Form(""),
    lab: str = Form(""),
    cert_number: str = Form(""),
    cert_url: str = Form(""),
    origin: str = Form("natural"),
    treatment: str = Form(""),
    supplier_id: str = Form(""),
    ownership: str = Form("owned"),
    memo_due_date: str = Form(""),
    acquisition_cost: str = Form(""),
    acquisition_date: str = Form(""),
    current_location_id: str = Form(""),
    notes: str = Form(""),
    tracking_trigger: str = Form(""),
    tracking_override_reason: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise(
        db,
        {
            "stone_type": stone_type,
            "shape_id": shape_id,
            "length_mm": length_mm,
            "width_mm": width_mm,
            "depth_mm": depth_mm,
            "carat_weight": carat_weight,
            "colour_grade": colour_grade,
            "clarity_grade": clarity_grade,
            "cut_grade": cut_grade,
            "polish": polish,
            "symmetry": symmetry,
            "fluorescence": fluorescence,
            "lab": lab,
            "cert_number": cert_number,
            "cert_url": cert_url,
            "origin": origin,
            "treatment": treatment,
            "supplier_id": supplier_id,
            "ownership": ownership,
            "memo_due_date": memo_due_date,
            "acquisition_cost": acquisition_cost,
            "acquisition_date": acquisition_date,
            "current_location_id": current_location_id,
            "notes": notes,
            "tracking_trigger": tracking_trigger,
            "tracking_override_reason": tracking_override_reason,
        },
        enforce_tracking_trigger=True,
    )
    stone_code = allocate_stone_code(db)
    stone = Stone(stone_code=stone_code, status=StoneStatus.AVAILABLE, **fields)
    db.add(stone)
    db.flush()

    # Initial event so the ledger has a "created" marker — same posture
    # as the items table's audit row on create, but in the domain-specific
    # event log so future stone-history views are self-contained.
    _record_stone_event(
        db,
        stone,
        event_type="created",
        actor=user,
        to_status=StoneStatus.AVAILABLE,
        to_location_id=stone.current_location_id,
    )
    record_audit(
        db,
        actor=user,
        action="stone.created",
        entity_type="stone",
        entity_id=stone.id,
        before=None,
        after={"stone_code": stone_code, **{f: fields[f] for f in _FIELDS}},
    )
    db.commit()
    _flash(request, f"Stone {stone.stone_code} created.")
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{stone_id}/edit", response_class=HTMLResponse)
def edit_stone_form(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stone = db.get(Stone, stone_id)
    if stone is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone not found"
        )
    return templates.TemplateResponse(
        request,
        "stones_form.html",
        {
            "current_user": _user,
            "stone": stone,
            "form": _form_view_for(stone),
            "title": f"Edit {stone.stone_code}",
            "action": f"/admin/stones/{stone.id}",
            "shape_options": _shape_options(db, current_id=stone.shape_id),
            "supplier_options": _supplier_options(db, current_id=stone.supplier_id),
            "location_options": _location_options(db, current_id=stone.current_location_id),
            "stone_types": [t.value for t in StoneType],
            "labs": [lab.value for lab in StoneLab],
            "origins": [o.value for o in StoneOrigin],
            "ownerships": [o.value for o in StoneOwnership],
        },
    )


@router.post("/{stone_id}")
def update_stone(
    request: Request,
    stone_id: int,
    stone_type: str = Form("diamond"),
    shape_id: str = Form(""),
    length_mm: str = Form(""),
    width_mm: str = Form(""),
    depth_mm: str = Form(""),
    carat_weight: str = Form(""),
    colour_grade: str = Form(""),
    clarity_grade: str = Form(""),
    cut_grade: str = Form(""),
    polish: str = Form(""),
    symmetry: str = Form(""),
    fluorescence: str = Form(""),
    lab: str = Form(""),
    cert_number: str = Form(""),
    cert_url: str = Form(""),
    origin: str = Form("natural"),
    treatment: str = Form(""),
    supplier_id: str = Form(""),
    ownership: str = Form("owned"),
    memo_due_date: str = Form(""),
    acquisition_cost: str = Form(""),
    acquisition_date: str = Form(""),
    current_location_id: str = Form(""),
    notes: str = Form(""),
    tracking_trigger: str = Form(""),
    tracking_override_reason: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = db.get(Stone, stone_id)
    if stone is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone not found"
        )

    fields = _normalise(
        db,
        {
            "stone_type": stone_type,
            "shape_id": shape_id,
            "length_mm": length_mm,
            "width_mm": width_mm,
            "depth_mm": depth_mm,
            "carat_weight": carat_weight,
            "colour_grade": colour_grade,
            "clarity_grade": clarity_grade,
            "cut_grade": cut_grade,
            "polish": polish,
            "symmetry": symmetry,
            "fluorescence": fluorescence,
            "lab": lab,
            "cert_number": cert_number,
            "cert_url": cert_url,
            "origin": origin,
            "treatment": treatment,
            "supplier_id": supplier_id,
            "ownership": ownership,
            "memo_due_date": memo_due_date,
            "acquisition_cost": acquisition_cost,
            "acquisition_date": acquisition_date,
            "current_location_id": current_location_id,
            "notes": notes,
            "tracking_trigger": tracking_trigger,
            "tracking_override_reason": tracking_override_reason,
        },
        current=stone,
        # Edit doesn't enforce a trigger — legacy stones (rows from
        # before migration 0045) may have NULL today; we let them stay
        # that way until someone backfills.
        enforce_tracking_trigger=False,
    )

    # Detect lifecycle events implied by the edit *before* mutating the
    # row — the helpers compare the pre-edit Stone against the new
    # values.
    implied_events = _detect_lifecycle_events_on_edit(stone, fields)

    diff = _diff(stone, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(stone, f, fields[f])
        for event_type in implied_events:
            _record_stone_event(
                db,
                stone,
                event_type=event_type,
                actor=user,
            )
        record_audit(
            db,
            actor=user,
            action="stone.updated",
            entity_type="stone",
            entity_id=stone.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Stone {stone.stone_code} updated.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# History view (read-only stone_events timeline)
# ---------------------------------------------------------------------------


@router.get("/{stone_id}/history", response_class=HTMLResponse)
def stone_history(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Stone-specific event timeline rendered chronologically.

    Surfaces the ``stone_events`` ledger that the lifecycle handlers
    (set / unset / sell / lost / returned / relocated / cert_updated /
    ownership_changed) populate. Per-event row shows the actor, the
    state diff (status, item, location), and any operator note — same
    posture as the items detail page's movement timeline, but
    domain-specific for stones.
    """
    stone = _require_stone(db, stone_id)
    events = list(
        db.execute(
            select(StoneEvent)
            .where(StoneEvent.stone_id == stone.id)
            .order_by(StoneEvent.id)
        ).scalars().all()
    )
    # Resolve every actor / item / location label up-front so the
    # template doesn't have to chase FKs row-by-row.
    actor_ids = {e.actor_id for e in events if e.actor_id is not None}
    actor_by_id: dict[int, str] = {}
    if actor_ids:
        actors = db.execute(
            select(User).where(User.id.in_(actor_ids))
        ).scalars().all()
        actor_by_id = {u.id: u.name or u.email for u in actors}
    item_ids = {
        e.from_item_id for e in events if e.from_item_id is not None
    } | {e.to_item_id for e in events if e.to_item_id is not None}
    item_by_id: dict[int, str] = {}
    if item_ids:
        items = db.execute(
            select(Item).where(Item.id.in_(item_ids))
        ).scalars().all()
        item_by_id = {it.id: f"{it.sku} — {it.name}" for it in items}
    loc_ids = {
        e.from_location_id for e in events if e.from_location_id is not None
    } | {e.to_location_id for e in events if e.to_location_id is not None}
    loc_by_id: dict[int, str] = {}
    if loc_ids:
        locs = db.execute(
            select(Location).where(Location.id.in_(loc_ids))
        ).scalars().all()
        loc_by_id = {loc.id: loc.name for loc in locs}

    view_rows = [
        {
            "event": e,
            "actor_label": actor_by_id.get(e.actor_id, "") if e.actor_id else "system",
            "from_item_label": item_by_id.get(e.from_item_id, "") if e.from_item_id else "",
            "to_item_label": item_by_id.get(e.to_item_id, "") if e.to_item_id else "",
            "from_location_label": (
                loc_by_id.get(e.from_location_id, "") if e.from_location_id else ""
            ),
            "to_location_label": (
                loc_by_id.get(e.to_location_id, "") if e.to_location_id else ""
            ),
        }
        for e in events
    ]
    return templates.TemplateResponse(
        request,
        "stones_history.html",
        {
            "current_user": _user,
            "stone": stone,
            "rows": view_rows,
        },
    )


# ---------------------------------------------------------------------------
# Lifecycle routes
# ---------------------------------------------------------------------------


def _require_stone(db: Session, stone_id: int) -> Stone:
    stone = db.get(Stone, stone_id)
    if stone is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stone not found"
        )
    return stone


# ---- Set ----


@router.get("/{stone_id}/set", response_class=HTMLResponse)
def set_stone_form(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stone = _require_stone(db, stone_id)
    # Only available / reserved stones are settable; spec §1.1.
    if stone.status not in (StoneStatus.AVAILABLE, StoneStatus.RESERVED):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"stone {stone.stone_code!r} is {stone.status.value}; cannot set",
        )
    # Active items only — settable into something operators can still
    # work on. Archived items would silently complicate centre_stone_id
    # maintenance.
    items: list[Item] = list(
        db.execute(
            select(Item).where(Item.archived_at.is_(None)).order_by(Item.sku)
        ).scalars().all()
    )
    return templates.TemplateResponse(
        request,
        "stones_set_form.html",
        {
            "current_user": _user,
            "stone": stone,
            "item_options": [
                {"id": it.id, "label": f"{it.sku} — {it.name}"} for it in items
            ],
            "positions": [p.value for p in StonePosition],
        },
    )


@router.post("/{stone_id}/set")
def set_stone(
    request: Request,
    stone_id: int,
    item_id: str = Form(""),
    position: str = Form(""),
    position_index: str = Form("0"),
    note: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = _require_stone(db, stone_id)
    try:
        item_id_int = int((item_id or "").strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="item_id must be an integer",
        ) from exc
    item = db.get(Item, item_id_int)
    if item is None or item.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="item not found or archived",
        )
    pos = _parse_enum(StonePosition, position, field_name="position")
    try:
        pos_idx = int((position_index or "0").strip())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="position_index must be an integer",
        ) from exc
    _set_stone_into_item(
        db, stone, item,
        position=pos, position_index=pos_idx,
        actor=user, note=note.strip() or None,
    )
    record_audit(
        db,
        actor=user,
        action="stone.set",
        entity_type="stone",
        entity_id=stone.id,
        before={"status": StoneStatus.AVAILABLE.value, "current_item_id": None},
        after={
            "status": StoneStatus.SET.value,
            "current_item_id": item.id,
            "position": pos.value,
            "position_index": pos_idx,
        },
    )
    db.commit()
    _flash(request, f"Stone {stone.stone_code} set into {item.sku}.")
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


# ---- Unset ----


@router.post("/{stone_id}/unset")
def unset_stone(
    request: Request,
    stone_id: int,
    note: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = _require_stone(db, stone_id)
    prior_item_id = stone.current_item_id
    _unset_stone_from_item(db, stone, actor=user, note=note.strip() or None)
    record_audit(
        db,
        actor=user,
        action="stone.unset",
        entity_type="stone",
        entity_id=stone.id,
        before={"status": StoneStatus.SET.value, "current_item_id": prior_item_id},
        after={"status": StoneStatus.AVAILABLE.value, "current_item_id": None},
    )
    db.commit()
    _flash(request, f"Stone {stone.stone_code} unset.")
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


# ---- Sell / lost / returned_to_supplier ----


def _terminal_action(
    db: Session,
    stone: Stone,
    *,
    new_status: StoneStatus,
    event_type: str,
    actor: User,
    note: str,
) -> StoneStatus:
    """Run the terminal-transition state change. Returns the prior status.

    Helper rather than a full Response builder so each route can call
    ``record_audit`` directly in its body — the audit-coverage sweep
    (``tests/integration/test_audit_coverage.py``) requires a literal
    ``record_audit(`` call in each mutation route's source.
    """
    prior_status = stone.status
    _terminal_transition(
        db, stone,
        new_status=new_status, event_type=event_type,
        actor=actor, note=note.strip() or None,
    )
    return prior_status


@router.get("/{stone_id}/sell", response_class=HTMLResponse)
def sell_stone_form(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stone = _require_stone(db, stone_id)
    return templates.TemplateResponse(
        request,
        "stones_action_form.html",
        {
            "current_user": _user,
            "stone": stone,
            "title": f"Sell stone {stone.stone_code}",
            "action": f"/admin/stones/{stone.id}/sell",
            "verb": "Sell",
            "warning": (
                "Sold stones become terminal — they can't be moved back to available."
                + (" Active linkage to an item will be unset automatically."
                   if stone.status is StoneStatus.SET else "")
            ),
        },
    )


@router.post("/{stone_id}/sell")
def sell_stone(
    request: Request,
    stone_id: int,
    note: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = _require_stone(db, stone_id)
    prior_status = _terminal_action(
        db, stone,
        new_status=StoneStatus.SOLD, event_type="sold",
        actor=user, note=note,
    )
    record_audit(
        db, actor=user, action="stone.sold",
        entity_type="stone", entity_id=stone.id,
        before={"status": prior_status.value},
        after={"status": StoneStatus.SOLD.value},
    )
    db.commit()
    _flash(request, f"Stone {stone.stone_code} sold.")
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{stone_id}/lost", response_class=HTMLResponse)
def lost_stone_form(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stone = _require_stone(db, stone_id)
    return templates.TemplateResponse(
        request,
        "stones_action_form.html",
        {
            "current_user": _user,
            "stone": stone,
            "title": f"Mark stone {stone.stone_code} lost",
            "action": f"/admin/stones/{stone.id}/lost",
            "verb": "Mark lost",
            "warning": "Lost is terminal — operators can't restore an active state.",
        },
    )


@router.post("/{stone_id}/lost")
def mark_stone_lost(
    request: Request,
    stone_id: int,
    note: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = _require_stone(db, stone_id)
    prior_status = _terminal_action(
        db, stone,
        new_status=StoneStatus.LOST, event_type="lost",
        actor=user, note=note,
    )
    record_audit(
        db, actor=user, action="stone.lost",
        entity_type="stone", entity_id=stone.id,
        before={"status": prior_status.value},
        after={"status": StoneStatus.LOST.value},
    )
    db.commit()
    _flash(request, f"Stone {stone.stone_code} marked lost.")
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{stone_id}/return", response_class=HTMLResponse)
def return_stone_form(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stone = _require_stone(db, stone_id)
    return templates.TemplateResponse(
        request,
        "stones_action_form.html",
        {
            "current_user": _user,
            "stone": stone,
            "title": f"Return stone {stone.stone_code} to supplier",
            "action": f"/admin/stones/{stone.id}/return",
            "verb": "Return to supplier",
            "warning": "Returned-to-supplier is terminal.",
        },
    )


@router.post("/{stone_id}/return")
def return_stone(
    request: Request,
    stone_id: int,
    note: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = _require_stone(db, stone_id)
    prior_status = _terminal_action(
        db, stone,
        new_status=StoneStatus.RETURNED_TO_SUPPLIER, event_type="returned",
        actor=user, note=note,
    )
    record_audit(
        db, actor=user, action="stone.returned",
        entity_type="stone", entity_id=stone.id,
        before={"status": prior_status.value},
        after={"status": StoneStatus.RETURNED_TO_SUPPLIER.value},
    )
    db.commit()
    _flash(request, f"Stone {stone.stone_code} returned to supplier.")
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )


# ---- Relocate ----


@router.get("/{stone_id}/relocate", response_class=HTMLResponse)
def relocate_stone_form(
    request: Request,
    stone_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    stone = _require_stone(db, stone_id)
    return templates.TemplateResponse(
        request,
        "stones_relocate_form.html",
        {
            "current_user": _user,
            "stone": stone,
            "location_options": _location_options(db, current_id=stone.current_location_id),
            "current_location_id": stone.current_location_id,
        },
    )


@router.post("/{stone_id}/relocate")
def relocate_stone_route(
    request: Request,
    stone_id: int,
    current_location_id: str = Form(""),
    note: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    stone = _require_stone(db, stone_id)
    new_location_id = _resolve_optional_fk(
        db,
        current_location_id,
        model=Location,
        archived_field="archived_at",
        label="location",
        current_id=stone.current_location_id,
    )
    prior = stone.current_location_id
    event = _relocate_stone(
        db, stone,
        new_location_id=new_location_id, actor=user, note=note.strip() or None,
    )
    if event is not None:
        record_audit(
            db,
            actor=user,
            action="stone.relocated",
            entity_type="stone",
            entity_id=stone.id,
            before={"current_location_id": prior},
            after={"current_location_id": new_location_id},
        )
        db.commit()
        _flash(request, f"Stone {stone.stone_code} relocated.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/stones", status_code=status.HTTP_303_SEE_OTHER
    )
