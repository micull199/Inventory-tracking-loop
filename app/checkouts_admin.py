"""Manager / Office cross-item checkout oversight (C4 — DoD #4 + #7).

One read-only route mounted at ``/admin/checkouts``:

- ``GET /admin/checkouts[?show=open|overdue]`` — Manager + Office. Lists every
  currently-open checkout joined to its item + holder + (optional) item_unit,
  ordered with overdue first then by ``checked_out_at`` desc. Workshop is
  excluded — they already see per-item status blocks via C2's `/checkout`
  page; cross-item "who has what" is an oversight tool, not a workshop one.

The module also exposes ``overdue_count(db)`` so ``app/dashboard.py`` can wire
the ``dashboard-overdue-checkouts`` widget without re-implementing the SQL.

Engine isolation: this slice is read-only. No DB writes, no audit, no movement
type, no new tables. The single query joins ``Checkout → Item`` (RESTRICT FK,
always present) and outer-joins ``User`` (SET NULL FK, may be null after a
rare hard-delete) and ``ItemUnit`` (nullable for qty-tracked checkouts).

Overdue is ``returned_at IS NULL AND expected_return < now()``. A checkout
with ``expected_return = NULL`` (no due date) is open-but-never-overdue. The
``days_overdue`` cell is computed in Python from ``now().date() -
expected_return.date()``; only the date part matters for the user-facing
display so a tool that's "due back today" at 23:59 doesn't flip overdue at
midnight UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import Checkout, Item, ItemUnit, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/checkouts", tags=["checkouts-admin"])

_VALID_SHOW = ("open", "overdue")

_CHECKOUTS_ADMIN_CSV_HEADERS: list[str] = [
    "checkout_id",
    "item_id",
    "item_sku",
    "item_name",
    "item_archived",
    "unit_serial",
    "holder_email",
    "checked_out_at",
    "expected_return",
    "is_overdue",
    "days_overdue",
]


def _csv_rows_for_checkouts(rows: list[dict[str, Any]]) -> list[list[Any]]:
    """Map view-shaped checkout rows to CSV cell values.

    The cells mirror the HTML table one-for-one (8 visual columns) plus three
    explicit ID/state columns: ``checkout_id`` + ``item_id`` (HTML carries
    them as ``data-checkout-id`` / ``data-item-id`` attributes — making them
    columns lets a downstream consumer join), and ``is_overdue`` (the HTML
    conflates it with ``days_overdue`` into a single "Status" cell; the CSV
    separates them so a downstream filter can target one or the other).

    ``item_archived`` + ``is_overdue`` render as the literal strings ``"yes"``
    / ``"no"`` (matching R5/R5b's PO list ``supplier_archived`` and items
    list ``requires_checkout`` precedent — spreadsheet receivers find
    yes/no easier to filter on than ``True``/``False``).

    ``unit_serial`` + ``holder_email`` render as empty strings when ``None``
    (matching ``csv_response``'s coercion). The HTML renders ``—`` for these
    but a CSV cell of ``—`` would mis-sort against actual values.

    ``expected_return`` is the full datetime (ISO) when set, empty when
    ``None``. The HTML formats as ``%Y-%m-%d`` but the CSV preserves the
    full datetime so a downstream consumer can sort precisely.

    ``days_overdue`` is the integer when overdue, empty otherwise. Matches
    the ``is_overdue=False → days_overdue is None`` pairing in
    ``_list_open_checkouts``.
    """
    return [
        [
            r["checkout_id"],
            r["item_id"],
            r["item_sku"],
            r["item_name"],
            "yes" if r["item_archived"] else "no",
            r["unit_serial"],
            r["holder_email"],
            r["checked_out_at"],
            r["expected_return"],
            "yes" if r["is_overdue"] else "no",
            r["days_overdue"],
        ]
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_utc_aware(value: datetime | None) -> datetime | None:
    """Coerce a SQLite-read naive datetime into a UTC-aware one.

    SQLAlchemy's ``DateTime(timezone=True)`` round-trips through SQLite as a
    naive ISO string (SQLite has no native TZ type). Postgres preserves the
    offset. Comparing a stored value against ``datetime.now(UTC)`` therefore
    requires a tzinfo-attach step on the SQLite-returned value. This helper
    is a no-op on already-aware datetimes (the Postgres path).
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _coerce_show(raw: str | None) -> str:
    """Default to ``open`` when blank or unrecognised.

    Same posture as the items list's ``?show=active|archived`` and the PO
    list's ``?status_filter=`` — a stale shared link with a typo lands on the
    default rather than 400.
    """
    if raw in _VALID_SHOW:
        return raw
    return "open"


def _open_count(db: Session) -> int:
    """Count of currently-open checkouts (``returned_at IS NULL``)."""
    stmt = select(func.count(Checkout.id)).where(Checkout.returned_at.is_(None))
    return int(db.execute(stmt).scalar_one())


def overdue_count(db: Session, *, now: datetime | None = None) -> int:
    """Count of currently-open checkouts whose due date has passed.

    Exposed so ``app/dashboard.py`` can wire the ``overdue_checkouts`` widget
    without duplicating the predicate.
    """
    moment = now if now is not None else datetime.now(UTC)
    stmt = (
        select(func.count(Checkout.id))
        .where(Checkout.returned_at.is_(None))
        .where(Checkout.expected_return.is_not(None))
        .where(Checkout.expected_return < moment)
    )
    return int(db.execute(stmt).scalar_one())


def _list_open_checkouts(db: Session, *, show: str, now: datetime) -> list[dict[str, Any]]:
    """Return view-shaped rows for the open checkouts table.

    ``show="overdue"`` narrows to rows with a past ``expected_return``;
    ``show="open"`` returns every open row (overdue + not-yet-overdue).
    Ordering: overdue first (so the operator sees the urgent ones at the top
    of the page when they hit the default ``open`` tab), then by
    ``checked_out_at DESC, id DESC`` (newest checkouts first within each
    overdue bucket).
    """
    stmt = (
        select(Checkout, Item, User.email, ItemUnit.serial_or_label)
        .join(Item, Checkout.item_id == Item.id)
        .outerjoin(User, Checkout.user_id == User.id)
        .outerjoin(ItemUnit, Checkout.item_unit_id == ItemUnit.id)
        .where(Checkout.returned_at.is_(None))
    )
    if show == "overdue":
        stmt = stmt.where(Checkout.expected_return.is_not(None)).where(
            Checkout.expected_return < now
        )

    rows: list[dict[str, Any]] = []
    for co, item, holder_email, unit_serial in db.execute(stmt).all():
        expected = _as_utc_aware(co.expected_return)
        checked_out = _as_utc_aware(co.checked_out_at)
        assert checked_out is not None  # NOT NULL on the column
        is_overdue = expected is not None and expected < now
        days_overdue: int | None = None
        if is_overdue and expected is not None:
            delta = now.date() - expected.date()
            days_overdue = delta.days
        rows.append(
            {
                "checkout_id": co.id,
                "item_id": item.id,
                "item_sku": item.sku,
                "item_name": item.name,
                "item_archived": item.archived_at is not None,
                "unit_id": co.item_unit_id,
                "unit_serial": unit_serial,
                "holder_email": holder_email,
                "checked_out_at": checked_out,
                "expected_return": expected,
                "is_overdue": is_overdue,
                "days_overdue": days_overdue,
            }
        )
    # Python-side sort: overdue first, then by checked_out_at desc + id desc.
    rows.sort(
        key=lambda r: (
            0 if r["is_overdue"] else 1,
            -int(r["checked_out_at"].timestamp()),
            -int(r["checkout_id"]),
        )
    )
    return rows


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("")
def list_checkouts(
    request: Request,
    format: str = "",
    user: User = Depends(require_role(Role.MANAGER, Role.OFFICE)),
    db: Session = Depends(get_session),
) -> Response:
    """Render the manager-facing currently-out / overdue list."""
    show = _coerce_show(request.query_params.get("show"))
    now = datetime.now(UTC)
    rows = _list_open_checkouts(db, show=show, now=now)

    if (
        resp := csv_branch(
            format,
            filename=f"checkouts_{show}.csv",
            headers=_CHECKOUTS_ADMIN_CSV_HEADERS,
            rows=_csv_rows_for_checkouts(rows),
        )
    ) is not None:
        return resp

    open_n = _open_count(db)
    overdue_n = overdue_count(db, now=now)

    return templates.TemplateResponse(
        request,
        "checkouts_admin.html",
        {
            "current_user": user,
            "show": show,
            "rows": rows,
            "open_count": open_n,
            "overdue_count": overdue_n,
        },
    )
