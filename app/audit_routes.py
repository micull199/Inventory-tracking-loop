"""Manager+Admin audit log read view.

The ``audit_log`` table is populated by ``app.audit.record_audit`` and locked at
the DB layer by the immutability triggers. This module provides the *read* leg
— a paginated newest-first list view so a Manager or Admin can inspect what's
happened in the system.

Access: ``Manager`` and ``Admin`` (admins always pass ``require_role``).
Workshop and Office both 403 — Office is a sibling role per MISSION §3 and the
audit log includes role/cost data Office is not gated to see in aggregate.

This is a read-only surface. The HTML branch paginates (50 rows per page);
the CSV branch (``?format=csv``) ignores pagination and exports every row —
a snapshot artefact for the receiver. Per MISSION §9 the audit log can never
be edited, so neither branch ever writes; both are pinned by tests.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import AuditLog, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/audit", tags=["audit"])

PAGE_SIZE = 50


_AUDIT_CSV_HEADERS: list[str] = [
    "id",
    "created_at",
    "actor_email",
    "action",
    "entity_type",
    "entity_id",
    "before_json",
    "after_json",
]


def _coerce_json_cell(value: dict[str, Any] | None) -> str | None:
    """Render a stored JSON dict as compact JSON for a CSV cell.

    Returns ``None`` for ``None`` so the ``csv_response`` coercion writes an
    empty cell (matches the HTML's em-dash semantics, but a CSV cell of ``"—"``
    would mis-sort against real values). ``default=str`` is a safety net for
    any value that escaped ``_to_jsonable`` at write time.
    """
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), default=str)


def _csv_rows_for_audit(rows: list[tuple[AuditLog, User | None]]) -> list[list[Any]]:
    """Map ``(AuditLog, User | None)`` rows to CSV cell values.

    Mirrors the HTML table cells (Time / Actor / Action / Entity / Before /
    After) plus two explicit ID columns at the front: ``id`` (the HTML carries
    it as ``data-audit-id``, not a cell) and a separated ``actor_email`` (the
    HTML conflates ``actor.email`` and the ``(system)`` placeholder; the CSV
    keeps them distinct so a spreadsheet receiver can filter cleanly — system
    actions render with an empty ``actor_email`` cell).
    """
    return [
        [
            entry.id,
            entry.created_at,
            actor.email if actor is not None else None,
            entry.action,
            entry.entity_type,
            entry.entity_id,
            _coerce_json_cell(entry.before_json),
            _coerce_json_cell(entry.after_json),
        ]
        for entry, actor in rows
    ]


@router.get("")
def list_audit(
    request: Request,
    page: int = 1,
    format: str = "",
    current_user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    """Newest-first paginated list of audit-log entries.

    A too-high ``page`` does not 404 — it renders an empty table. The user can
    page back. A too-low ``page`` (zero or negative) is clamped to 1.

    ``?format=csv`` exports every row (ignores pagination); anything else
    renders HTML.
    """
    if format == "csv":
        # CSV branch ignores pagination — the receiver wants the whole snapshot.
        all_rows = db.execute(
            select(AuditLog, User)
            .outerjoin(User, AuditLog.actor_id == User.id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        ).all()
        # `.all()` returns Row objects; convert to plain tuples for the helper.
        rows_for_csv: list[tuple[AuditLog, User | None]] = [
            (entry, actor) for entry, actor in all_rows
        ]
        resp = csv_branch(
            format,
            filename="audit_log.csv",
            headers=_AUDIT_CSV_HEADERS,
            rows=_csv_rows_for_audit(rows_for_csv),
        )
        # `csv_branch` returns ``None`` for non-csv format; we just checked
        # ``format == "csv"``, so this is always a Response.
        assert resp is not None
        return resp

    if page < 1:
        page = 1
    offset = (page - 1) * PAGE_SIZE

    total = db.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    # LEFT JOIN the actor so we can render the email; system actions
    # (actor_id IS NULL) still appear with "(system)" in the actor column.
    rows = db.execute(
        select(AuditLog, User)
        .outerjoin(User, AuditLog.actor_id == User.id)
        .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        .limit(PAGE_SIZE)
        .offset(offset)
    ).all()

    showing_from = offset + 1 if rows else 0
    showing_to = offset + len(rows)
    has_prev = page > 1
    has_next = (offset + len(rows)) < total

    return templates.TemplateResponse(
        request,
        "admin_audit.html",
        {
            "current_user": current_user,
            "rows": rows,
            "page": page,
            "page_size": PAGE_SIZE,
            "total": total,
            "showing_from": showing_from,
            "showing_to": showing_to,
            "has_prev": has_prev,
            "has_next": has_next,
        },
    )
