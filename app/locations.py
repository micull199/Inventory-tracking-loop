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

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.csv_export import csv_branch
from app.csv_import import (
    CsvUploadError,
    RowResult,
    check_required_and_known_headers,
    mark_intra_file_duplicates,
    read_upload,
    row_to_dict,
)
from app.db import get_session
from app.models import Location, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/locations", tags=["locations"])
# See ``app/suppliers.py`` for the rationale: the literal ``/upload`` route
# must be matched ahead of the dynamic ``/{location_id}`` routes.
upload_router = APIRouter(prefix="/admin/locations", tags=["locations"])


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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    return name


def _check_name_unique(db: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(Location.id).where(Location.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Location.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a location with that name already exists",
        )


def _diff(
    location: Location, new: dict[str, str | None]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
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
    return RedirectResponse(url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="location not found")
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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="location not found")

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

    return RedirectResponse(url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER)


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="location not found")

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

    return RedirectResponse(url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{location_id}/unarchive")
def unarchive_location(
    request: Request,
    location_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    location = db.get(Location, location_id)
    if location is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="location not found")

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

    return RedirectResponse(url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# CSV upload
# ---------------------------------------------------------------------------
#
# Same shape as ``suppliers.upload``: bulk-create from a CSV mirroring the
# download. Update-via-CSV is out of v1 scope; a row with a matching ``id``
# is skipped.

_LOCATIONS_UPLOAD_KNOWN: set[str] = {"id", "name", "notes", "archived_at"}
_LOCATIONS_UPLOAD_REQUIRED: set[str] = {"name"}
_LOCATIONS_UPLOAD_COLUMNS = [
    {"name": "id", "required": False, "note": "blank = create; matching id = skip"},
    {"name": "name", "required": True, "note": "unique across active + archived"},
    {"name": "notes", "required": False, "note": "free text, ≤2000 chars"},
    {
        "name": "archived_at",
        "required": False,
        "note": "ignored on create — new rows always land active",
    },
]
_LOCATIONS_NOTES_MAX = 2000


def _build_location_update_result(
    row_number: int,
    raw: dict[str, str],
    *,
    existing: Location,
    other_names_lc: set[str],
) -> RowResult:
    """Diff CSV cells against an existing location row."""
    name = (raw.get("name") or "").strip() or existing.name
    if name.lower() != existing.name.lower() and name.lower() in other_names_lc:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="name",
            error_message="another location already uses that name",
        )
    notes_raw = (raw.get("notes") or "").strip()
    if len(notes_raw) > _LOCATIONS_NOTES_MAX:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="notes",
            error_message=f"notes too long (max {_LOCATIONS_NOTES_MAX} chars)",
        )
    notes = notes_raw or None

    changes: dict[str, Any] = {}
    before: dict[str, Any] = {}
    if name != existing.name:
        changes["name"] = name
        before["name"] = existing.name
    if (notes or None) != (existing.notes or None):
        changes["notes"] = notes
        before["notes"] = existing.notes

    warnings: list[str] = []
    if (raw.get("archived_at") or "").strip():
        warnings.append("archived_at ignored on update — use archive button")

    if not changes:
        return RowResult(
            row_number=row_number, raw=raw, tag="skip",
            error_field="id",
            error_message=f"no changes (id={existing.id})",
            warnings=warnings,
        )
    return RowResult(
        row_number=row_number, raw=raw, tag="update",
        payload={"existing_id": existing.id, "changes": changes, "before": before},
        warnings=warnings,
    )


def _build_location_row_result(
    row_number: int,
    raw: dict[str, str],
    *,
    existing_by_id: dict[int, Location],
    existing_names_lc: set[str],
) -> RowResult:
    raw_id = (raw.get("id") or "").strip()
    if raw_id:
        try:
            id_int = int(raw_id)
        except ValueError:
            return RowResult(
                row_number=row_number,
                raw=raw,
                tag="error",
                error_field="id",
                error_message="id must be a whole number",
            )
        existing = existing_by_id.get(id_int)
        if existing is not None:
            others = existing_names_lc - {existing.name.lower()}
            return _build_location_update_result(
                row_number, raw,
                existing=existing,
                other_names_lc=others,
            )
        return RowResult(
            row_number=row_number,
            raw=raw,
            tag="error",
            error_field="id",
            error_message=(
                f"unknown id {id_int} — don't reuse ids from another database"
            ),
        )

    name = (raw.get("name") or "").strip()
    if not name:
        return RowResult(
            row_number=row_number,
            raw=raw,
            tag="error",
            error_field="name",
            error_message="name is required",
        )
    if name.lower() in existing_names_lc:
        return RowResult(
            row_number=row_number,
            raw=raw,
            tag="error",
            error_field="name",
            error_message="a location with that name already exists",
        )

    notes_raw = (raw.get("notes") or "").strip()
    if len(notes_raw) > _LOCATIONS_NOTES_MAX:
        return RowResult(
            row_number=row_number,
            raw=raw,
            tag="error",
            error_field="notes",
            error_message=f"notes too long (max {_LOCATIONS_NOTES_MAX} chars)",
        )
    notes = notes_raw or None

    warnings: list[str] = []
    if (raw.get("archived_at") or "").strip():
        warnings.append("archived_at ignored on create — new row lands active")

    return RowResult(
        row_number=row_number,
        raw=raw,
        tag="new",
        payload={"name": name, "notes": notes},
        warnings=warnings,
    )


def _validate_location_upload(
    db: Session, headers: list[str], body: list[list[str]]
) -> list[RowResult]:
    check_required_and_known_headers(
        headers,
        known=_LOCATIONS_UPLOAD_KNOWN,
        required=_LOCATIONS_UPLOAD_REQUIRED,
    )
    existing_rows = list(db.execute(select(Location)).scalars().all())
    existing_by_id: dict[int, Location] = {loc.id: loc for loc in existing_rows}
    existing_names_lc = {loc.name.lower() for loc in existing_rows}

    results: list[RowResult] = []
    for offset, row in enumerate(body):
        row_number = offset + 2
        raw = row_to_dict(headers, row)
        results.append(
            _build_location_row_result(
                row_number,
                raw,
                existing_by_id=existing_by_id,
                existing_names_lc=existing_names_lc,
            )
        )
    mark_intra_file_duplicates(results, key="name", case_insensitive=True)
    return results


def _summarise_location_row(r: RowResult) -> str:
    if r.payload is None:
        return ""
    if r.tag == "update":
        return "changed: " + ", ".join(sorted(r.payload.get("changes", {})))
    return f"name={r.payload.get('name', '')}"


@upload_router.get("/upload", response_class=HTMLResponse)
def upload_locations_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "csv_upload_form.html",
        {
            "current_user": _user,
            "title": "Upload locations CSV",
            "subtitle": "Bulk-create locations from a CSV. Update-via-CSV is not supported in v1.",
            "intro_html": "",
            "action": "/admin/locations/upload",
            "cancel_url": "/admin/locations",
            "download_url": "/admin/locations?format=csv&show=active",
            "expected_columns": _LOCATIONS_UPLOAD_COLUMNS,
        },
    )


@upload_router.post("/upload")
async def upload_locations(
    request: Request,
    file: UploadFile = File(...),
    dry_run: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    raw_bytes = await file.read()
    is_dry_run = dry_run == "1"

    preview_ctx: dict[str, Any] = {
        "current_user": user,
        "title": "Locations CSV — preview",
        "subtitle": "",
        "upload_url": "/admin/locations/upload",
        "cancel_url": "/admin/locations",
        "rows": [],
        "headers": [],
        "new_count": 0,
        "update_count": 0,
        "skip_count": 0,
        "error_count": 0,
        "top_level_error": None,
        "committed": False,
    }

    try:
        file_sha256, headers, body = read_upload(raw_bytes, filename=file.filename)
        results = _validate_location_upload(db, headers, body)
    except CsvUploadError as exc:
        preview_ctx["top_level_error"] = str(exc)
        return templates.TemplateResponse(
            request, "csv_upload_preview.html", preview_ctx
        )

    new_count = sum(1 for r in results if r.tag == "new")
    update_count = sum(1 for r in results if r.tag == "update")
    skip_count = sum(1 for r in results if r.tag == "skip")
    error_count = sum(1 for r in results if r.tag == "error")
    preview_ctx.update(
        {
            "headers": headers,
            "rows": [
                {
                    "row_number": r.row_number,
                    "tag": r.tag,
                    "error_field": r.error_field,
                    "error_message": r.error_message,
                    "warnings": r.warnings,
                    "summary": _summarise_location_row(r),
                }
                for r in results
            ],
            "new_count": new_count,
            "update_count": update_count,
            "skip_count": skip_count,
            "error_count": error_count,
        }
    )

    if is_dry_run or error_count > 0 or (new_count == 0 and update_count == 0):
        return templates.TemplateResponse(
            request, "csv_upload_preview.html", preview_ctx
        )

    for r in results:
        if r.payload is None:
            continue
        if r.tag == "new":
            payload = r.payload
            location = Location(name=payload["name"], notes=payload["notes"])
            db.add(location)
            db.flush()
            record_audit(
                db,
                actor=user,
                action="location.created",
                entity_type="location",
                entity_id=location.id,
                before=None,
                after={f: payload[f] for f in _FIELDS},
            )
        elif r.tag == "update":
            payload = r.payload
            existing = db.get(Location, payload["existing_id"])
            if existing is None:  # pragma: no cover — defensive
                continue
            for field, new_val in payload["changes"].items():
                setattr(existing, field, new_val)
            record_audit(
                db,
                actor=user,
                action="location.updated",
                entity_type="location",
                entity_id=existing.id,
                before=payload["before"],
                after=payload["changes"],
            )
    record_audit(
        db,
        actor=user,
        action="location.csv_uploaded",
        entity_type="location",
        entity_id=None,
        before=None,
        after={
            "count": new_count,
            "updated_count": update_count,
            "file_sha256": file_sha256,
        },
    )
    db.commit()
    _flash(
        request,
        f"Imported {new_count} new, updated {update_count} location(s) from CSV.",
    )
    return RedirectResponse(url="/admin/locations", status_code=status.HTTP_303_SEE_OTHER)
