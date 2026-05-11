"""Scan-mode landing page and action picker (SC1a + SC1b).

The smallest end-to-end SC1 sub-slice: a single focused-input page that
USB-emulating barcode/QR scanners can drive directly. The scanner sends
keystrokes followed by Enter; the form auto-submits to ``/scan/resolve``
which looks up the scanned value as either a ``qr_code`` or ``sku``.

After SC1b, a successful resolve 303-redirects to ``/scan/item/{id}``
which renders an action picker (Stock in / Stock out / Adjust, plus
Check out for flagged items) inline with the scan input. The user's
flow is then: scan → click action → submit form. SC1c will fold qty
and unit-cost entry inline so the entire flow collapses to two
interactions. Camera-based scanning lands in SC2.

All routes here are read-only — none writes an audit row. Resolution
and presentation are navigation, not state changes. The audit log
only records mutations.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
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
    by_qr = db.execute(select(Item).where(Item.qr_code == code)).scalar_one_or_none()
    if by_qr is not None:
        return by_qr
    return db.execute(select(Item).where(Item.sku == code)).scalar_one_or_none()


@router.get("", response_class=HTMLResponse)
def scan_page(
    request: Request,
    user: User = Depends(require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)),
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
    _user: User = Depends(require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    trimmed = code.strip()
    if not trimmed:
        _flash(request, "Please scan or type a code.")
        return RedirectResponse(url="/scan", status_code=status.HTTP_303_SEE_OTHER)

    item = _resolve_code(db, trimmed)
    if item is None:
        _flash(request, f"No item found for code: {trimmed}.")
        return RedirectResponse(url="/scan", status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse(
        url=f"/scan/item/{item.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/item/{item_id}", response_class=HTMLResponse)
def scan_item_page(
    request: Request,
    item_id: int,
    user: User = Depends(require_role(Role.WORKSHOP, Role.OFFICE, Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    """Action picker for a resolved item.

    Reached via the 303 redirect from ``POST /scan/resolve`` on a successful
    code resolution. Renders ``scan.html`` with ``resolved_item`` set so the
    template surfaces the item identity + Stock-in / Stock-out / Adjust
    action links (and Check-out when ``requires_checkout`` is set). The
    scan input is still rendered + autofocused on this page so a USB
    scanner on the next item drives a fresh resolve without manual nav.
    """
    item = db.get(Item, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    return templates.TemplateResponse(
        request,
        "scan.html",
        {"current_user": user, "resolved_item": item},
    )
