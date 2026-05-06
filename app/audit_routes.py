"""Manager+Admin audit log read view.

The ``audit_log`` table is populated by ``app.audit.record_audit`` and locked at
the DB layer by the immutability triggers. This module provides the *read* leg
— a paginated newest-first list view so a Manager or Admin can inspect what's
happened in the system.

Access: ``Manager`` and ``Admin`` (admins always pass ``require_role``).
Workshop and Office both 403 — Office is a sibling role per MISSION §3 and the
audit log includes role/cost data Office is not gated to see in aggregate.

This is a read-only surface. There is no edit, delete, or export route in
this slice — those are queued (CSV export is a future polish slice; per
MISSION §9 the audit log can never be edited).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_session
from app.models import AuditLog, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/audit", tags=["audit"])

PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
def list_audit(
    request: Request,
    page: int = 1,
    current_user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Newest-first paginated list of audit-log entries.

    A too-high ``page`` does not 404 — it renders an empty table. The user can
    page back. A too-low ``page`` (zero or negative) is clamped to 1.
    """
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
