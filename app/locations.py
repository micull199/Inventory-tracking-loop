"""Manager-owned location CRUD routes.

Locations are physical places stock can live: workshop bench, store room, safe,
etc. They are required for items (I1) and transfers (M5), so this slice lands
them now alongside suppliers.

Shape mirrors ``app/suppliers.py`` deliberately — name + notes + archived_at,
soft-deletable, name unique across active *and* archived rows. The two routers
sharing this shape is the data point we need to decide whether a shared
"settings CRUD" helper is worth extracting before S3 (taxonomy) lands.

Access: ``Manager`` and ``Admin``. Workshop and Office both 403 — Office is a
sibling role, not a subset, per MISSION §3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.csv_export import csv_branch
from app.db import get_session
from app.models import Location, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/locations", tags=["locations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELDS: tuple[str, ...] = ("name", "notes")


def _normalise(form: dict[str, str]) -> dict[str, str | None]:
    """Strip whitespace; treat empty string as ``None`` for optional fields."""
    name = (form.get("name") or "").strip()
    notes = (form.get("notes") or "").strip()
    return {"name": name, "notes": notes or None}


def _validate_name(name: str | None) -> str:
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    return name


def _check_name_unique(
    db: Session, name: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Location.id).where(Location.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Location.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a location with that name already exists",
        )


def _diff(location: Location, new: dict[str, str | None]) -> tuple[
    dict[str, Any], dict[str, Any]
] | None:
    """Return ``(before, after)`` of *changed* fields only, or None if no-op."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(location, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    if not before:
        return None
    return before, after


def _flash(request: Request, message: str) -> None:
    """Stash a one-shot message in the session; rendered + cleared by base.html."""
    request.session["flash"] = message


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

_LIST_ORDER = case((Location.archived_at.is_(None), 0), else_=1)


_LOCATIONS_CSV_HEADERS: list[str] = [
    "id",
    "name",
    "notes",
]


def _csv_rows_for_locations(rows: list[Location]) -> list[list[Any]]:
    """Map ``Location`` rows to CSV cell values.

    The cells mirror the HTML table one-for-one. ``id`` is added at the front
    so a downstream consumer can join (the HTML carries it as
    ``data-location-id`` rather than a cell). Blank notes render as empty
    cells (``None`` → ``""`` via ``csv_response``'s coercion), matching the
    HTML's ``l.notes or ""`` rendering. The ``archived_at`` column is not
    exposed — the active partition is encoded in the filename.
    """
    return [[loc.id, loc.name, loc.notes] for loc in rows]


@router.get("")
def list_locations(
    request: Request,
    show: str = "active",
    format: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(Location)
    if show == "active":
        stmt = stmt.where(Location.archived_at.is_(None))
    else:
        stmt = stmt.where(Location.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, Location.name)

    rows = list(db.execute(stmt).scalars().all())

    if (
        resp := csv_branch(
            format,
            filename=f"locations_{show}.csv",
            headers=_LOCATIONS_CSV_HEADERS,
            rows=_csv_rows_for_locations(rows),
        )
    ) is not None:
        return resp

    return templates.TemplateResponse(
        request,
        "locations_list.html",
        {
            "current_user": _user,
            "locations": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_location_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "locations_form.html",
        {
            "current_user": _user,
            "location": None,
            "form": {"name": "", "notes": ""},
            "title": "New location",
            "action": "/admin/locations",
        },
    )


@router.post("")
def create_location(
    request: Request,
    name: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise({"name": name, "notes": notes})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"])  # type: ignore[arg-type]

    location = Location(name=fields["name"], notes=fields["notes"])
    db.add(location)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="location.created",
        entity_type="location",
        entity_id=location.id,
        before=None,
        after={f: fields[f] for f in _FIELDS},
    )
    db.commit()
    _flash(request, f"Location “{location.name}” created.")
    return RedirectResponse(
        url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{location_id}/edit", response_class=HTMLResponse)
def edit_location_form(
    request: Request,
    location_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="location not found"
        )
    return templates.TemplateResponse(
        request,
        "locations_form.html",
        {
            "current_user": _user,
            "location": location,
            "form": {
                "name": location.name,
                "notes": location.notes or "",
            },
            "title": f"Edit {location.name}",
            "action": f"/admin/locations/{location.id}",
        },
    )


@router.post("/{location_id}")
def update_location(
    request: Request,
    location_id: int,
    name: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="location not found"
        )

    fields = _normalise({"name": name, "notes": notes})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"], exclude_id=location.id)  # type: ignore[arg-type]

    diff = _diff(location, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(location, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="location.updated",
            entity_type="location",
            entity_id=location.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Location “{location.name}” updated.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Archive / unarchive (soft delete)
# ---------------------------------------------------------------------------


@router.post("/{location_id}/archive")
def archive_location(
    request: Request,
    location_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="location not found"
        )

    if location.archived_at is None:
        location.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="location.archived",
            entity_type="location",
            entity_id=location.id,
            before={"archived_at": None},
            after={"archived_at": location.archived_at},
        )
        db.commit()
        _flash(request, f"Location “{location.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{location_id}/unarchive")
def unarchive_location(
    request: Request,
    location_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="location not found"
        )

    if location.archived_at is not None:
        previous = location.archived_at
        location.archived_at = None
        record_audit(
            db,
            actor=user,
            action="location.unarchived",
            entity_type="location",
            entity_id=location.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Location “{location.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(
        url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER
    )
