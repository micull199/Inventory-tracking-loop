"""Scan-mode landing page (SC1a).

The smallest end-to-end SC1 sub-slice: a single focused-input page that
USB-emulating barcode/QR scanners can drive directly. The scanner sends
keystrokes followed by Enter; the form auto-submits to ``/scan/resolve``
which looks up the scanned value as either a ``qr_code`` or ``sku`` and
303-redirects to the matching item's existing edit page (which already
exposes the in/out/adjust action links to Workshop per I1c).

This delivers a complete *three-interaction* end-to-end path (scan → click
action → submit form). DoD #3 calls out "two interactions"; subsequent
slices (SC1b/c) collapse the action-click into the scan-landing surface.
Camera-based scanning lands in SC2.

Both routes are read-only — neither writes an audit row. Resolution is a
navigation, not a state change. The audit log only records mutations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_role
from app.db import get_session
from app.models import Item, Role, User
from app.template_env import templates

router = APIRouter(prefix="/scan", tags=["scan"])


def _flash(request: Request, message: str) -> None:
    """Stash a one-shot message in the session; rendered + cleared by base.html."""
    request.session["flash"] = message


def _resolve_code(db: Session, code: str) -> Item | None:
    """Look up an item by ``qr_code`` first, then ``sku``.

    Precedence rationale: physical labels carry a QR code; the scanner reads
    that. SKUs are typed identifiers (e.g. ``ALLOY-925-2MM``) used as a
    fallback when no QR code is present. If by accident two distinct items
    share the same string across columns (e.g. item A has ``sku="X"`` and
    item B has ``qr_code="X"``), the QR-coded item wins — that's what the
    scanner physically points at.
    """
    by_qr = db.execute(
        select(Item).where(Item.qr_code == code)
    ).scalar_one_or_none()
    if by_qr is not None:
        return by_qr
    return db.execute(
        select(Item).where(Item.sku == code)
    ).scalar_one_or_none()


@router.get("", response_class=HTMLResponse)
def scan_page(
    request: Request,
    user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "scan.html",
        {"current_user": user},
    )


@router.post("/resolve")
def resolve_scan(
    request: Request,
    code: str = Form(""),
    _user: User = Depends(
        require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)
    ),
    db: Session = Depends(get_session),
) -> Response:
    trimmed = code.strip()
    if not trimmed:
        _flash(request, "Please scan or type a code.")
        return RedirectResponse(
            url="/scan", status_code=status.HTTP_303_SEE_OTHER
        )

    item = _resolve_code(db, trimmed)
    if item is None:
        _flash(request, f"No item found for code: {trimmed}.")
        return RedirectResponse(
            url="/scan", status_code=status.HTTP_303_SEE_OTHER
        )

    return RedirectResponse(
        url=f"/admin/items/{item.id}/edit",
        status_code=status.HTTP_303_SEE_OTHER,
    )
