"""Manager-owned supplier CRUD routes.

Suppliers are the simplest settings entity: name + a few contact fields, no
schema versioning, no children. They are required for purchase orders (PO1+),
so this slice lands them first.

Access: ``Manager`` and ``Admin`` (admins always pass ``require_role``).
Workshop and Office both 403 — Office is a sibling role, not a subset, per
MISSION §3 ("Office: items, movements, POs ... cannot manage the taxonomy").

All mutations are POST + 303 + audit-logged. Validation failures raise
``HTTPException(400, ...)``; a future iteration may upgrade to in-form error
rendering for a better UX (queued in self-critique).
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
from app.models import Role, Supplier, User
from app.template_env import templates

router = APIRouter(prefix="/admin/suppliers", tags=["suppliers"])
# Separate router for the bulk-upload routes. Included *before* ``router``
# in ``app/main.py`` so the literal ``/admin/suppliers/upload`` path is
# matched ahead of the dynamic ``/{supplier_id}`` routes. Without this, a
# POST to ``/admin/suppliers/upload`` resolves to ``update_supplier`` with
# ``supplier_id="upload"`` and 422s on int coercion.
upload_router = APIRouter(prefix="/admin/suppliers", tags=["suppliers"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FIELDS: tuple[str, ...] = ("name", "email", "phone", "notes")


def _normalise(form: dict[str, str]) -> dict[str, str | None]:
    """Strip whitespace; treat empty string as ``None`` for optional fields."""
    name = (form.get("name") or "").strip()
    out: dict[str, str | None] = {"name": name}
    for f in ("email", "phone", "notes"):
        v = (form.get(f) or "").strip()
        out[f] = v or None
    return out


def _validate_name(name: str | None) -> str:
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="name is required")
    return name


def _check_name_unique(db: Session, name: str, *, exclude_id: int | None = None) -> None:
    stmt = select(Supplier.id).where(Supplier.name == name)
    if exclude_id is not None:
        stmt = stmt.where(Supplier.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a supplier with that name already exists",
        )


def _diff(
    supplier: Supplier, new: dict[str, str | None]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(before, after)`` of *changed* fields only, or None if no-op."""
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _FIELDS:
        old = getattr(supplier, f)
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
#
# Active first, then archived. Within each bucket, alphabetical so the page is
# stable across loads (no ORDER BY on `created_at` would tilt by recency).

_LIST_ORDER = case((Supplier.archived_at.is_(None), 0), else_=1)


_SUPPLIERS_CSV_HEADERS: list[str] = [
    "id",
    "name",
    "email",
    "phone",
    "notes",
]


def _csv_rows_for_suppliers(rows: list[Supplier]) -> list[list[Any]]:
    """Map ``Supplier`` rows to CSV cell values.

    The cells mirror the HTML table one-for-one. ``id`` is added at the front
    so a downstream consumer can join (the HTML carries it as
    ``data-supplier-id`` rather than a cell). Optional fields render as empty
    cells (``None`` → ``""`` via ``csv_response``'s coercion), matching the
    HTML's ``s.email or ""`` rendering.
    """
    return [[s.id, s.name, s.email, s.phone, s.notes] for s in rows]


@router.get("")
def list_suppliers(
    request: Request,
    show: str = "active",
    format: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"

    stmt = select(Supplier)
    if show == "active":
        stmt = stmt.where(Supplier.archived_at.is_(None))
    else:
        stmt = stmt.where(Supplier.archived_at.is_not(None))
    stmt = stmt.order_by(_LIST_ORDER, Supplier.name)

    rows = list(db.execute(stmt).scalars().all())

    if (
        resp := csv_branch(
            format,
            filename=f"suppliers_{show}.csv",
            headers=_SUPPLIERS_CSV_HEADERS,
            rows=_csv_rows_for_suppliers(rows),
        )
    ) is not None:
        return resp

    return templates.TemplateResponse(
        request,
        "suppliers_list.html",
        {
            "current_user": _user,
            "suppliers": rows,
            "show": show,
        },
    )


# ---------------------------------------------------------------------------
# New / create
# ---------------------------------------------------------------------------


@router.get("/new", response_class=HTMLResponse)
def new_supplier_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "suppliers_form.html",
        {
            "current_user": _user,
            "supplier": None,
            "form": {"name": "", "email": "", "phone": "", "notes": ""},
            "title": "New supplier",
            "action": "/admin/suppliers",
        },
    )


@router.post("")
def create_supplier(
    request: Request,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise({"name": name, "email": email, "phone": phone, "notes": notes})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"])  # type: ignore[arg-type]

    supplier = Supplier(
        name=fields["name"],
        email=fields["email"],
        phone=fields["phone"],
        notes=fields["notes"],
    )
    db.add(supplier)
    db.flush()

    record_audit(
        db,
        actor=user,
        action="supplier.created",
        entity_type="supplier",
        entity_id=supplier.id,
        before=None,
        after={f: fields[f] for f in _FIELDS},
    )
    db.commit()
    _flash(request, f"Supplier “{supplier.name}” created.")
    return RedirectResponse(url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Edit / update
# ---------------------------------------------------------------------------


@router.get("/{supplier_id}/edit", response_class=HTMLResponse)
def edit_supplier_form(
    request: Request,
    supplier_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found")
    return templates.TemplateResponse(
        request,
        "suppliers_form.html",
        {
            "current_user": _user,
            "supplier": supplier,
            "form": {
                "name": supplier.name,
                "email": supplier.email or "",
                "phone": supplier.phone or "",
                "notes": supplier.notes or "",
            },
            "title": f"Edit {supplier.name}",
            "action": f"/admin/suppliers/{supplier.id}",
        },
    )


@router.post("/{supplier_id}")
def update_supplier(
    request: Request,
    supplier_id: int,
    name: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found")

    fields = _normalise({"name": name, "email": email, "phone": phone, "notes": notes})
    _validate_name(fields["name"])
    _check_name_unique(db, fields["name"], exclude_id=supplier.id)  # type: ignore[arg-type]

    diff = _diff(supplier, fields)
    if diff is not None:
        before, after = diff
        for f in _FIELDS:
            setattr(supplier, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="supplier.updated",
            entity_type="supplier",
            entity_id=supplier.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Supplier “{supplier.name}” updated.")
    else:
        # No-op: don't write an audit row, but still 303 to the list so the
        # browser's POST-redirect-GET cycle completes cleanly.
        db.rollback()

    return RedirectResponse(url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Archive / unarchive (soft delete)
# ---------------------------------------------------------------------------


@router.post("/{supplier_id}/archive")
def archive_supplier(
    request: Request,
    supplier_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found")

    if supplier.archived_at is None:
        supplier.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="supplier.archived",
            entity_type="supplier",
            entity_id=supplier.id,
            before={"archived_at": None},
            after={"archived_at": supplier.archived_at},
        )
        db.commit()
        _flash(request, f"Supplier “{supplier.name}” archived.")
    else:
        db.rollback()

    return RedirectResponse(url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/{supplier_id}/unarchive")
def unarchive_supplier(
    request: Request,
    supplier_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    supplier = db.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="supplier not found")

    if supplier.archived_at is not None:
        previous = supplier.archived_at
        supplier.archived_at = None
        record_audit(
            db,
            actor=user,
            action="supplier.unarchived",
            entity_type="supplier",
            entity_id=supplier.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Supplier “{supplier.name}” restored.")
    else:
        db.rollback()

    return RedirectResponse(url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# CSV upload
# ---------------------------------------------------------------------------
#
# Bulk-create suppliers from a CSV file whose columns mirror the download.
# Update-via-CSV is intentionally out of scope (see CSV uploads spec.md):
# a row whose ``id`` matches an existing supplier is *skipped*, not patched.

_SUPPLIERS_UPLOAD_KNOWN: set[str] = {"id", "name", "email", "phone", "notes", "archived_at"}
_SUPPLIERS_UPLOAD_REQUIRED: set[str] = {"name"}
_SUPPLIERS_UPLOAD_COLUMNS = [
    {"name": "id", "required": False, "note": "blank = create; matching id = skip"},
    {"name": "name", "required": True, "note": "unique across active + archived"},
    {"name": "email", "required": False, "note": "RFC-shape validation only"},
    {"name": "phone", "required": False, "note": ""},
    {"name": "notes", "required": False, "note": ""},
    {
        "name": "archived_at",
        "required": False,
        "note": "ignored on create — new rows always land active",
    },
]


def _validate_email_shape(value: str) -> bool:
    """Loose RFC-shape email check: one ``@``, a dot in the domain portion.

    Not RFC-822 compliant (no library wants to maintain that). Catches the
    obvious typos (``alice``, ``a@b``, ``a@b.``); accepts the things humans
    actually type. Same posture as most web forms.
    """
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    if not local or not domain:
        return False
    return not ("." not in domain or domain.startswith(".") or domain.endswith("."))


def _parse_supplier_payload(
    raw: dict[str, str],
) -> tuple[dict[str, Any], str | None, str | None]:
    """Parse the editable cells of a supplier CSV row.

    Returns ``(payload, error_field, error_message)``. ``payload`` only
    contains keys whose source cell was non-blank for those that map to
    ``None`` (email/phone/notes). ``name`` is always present (validated
    non-empty separately by the caller — except on update where a blank
    cell means "no change").
    """
    payload: dict[str, Any] = {}
    payload["name"] = (raw.get("name") or "").strip()
    email = (raw.get("email") or "").strip()
    if email and not _validate_email_shape(email):
        return payload, "email", "email is not a valid address"
    payload["email"] = email or None
    payload["phone"] = (raw.get("phone") or "").strip() or None
    payload["notes"] = (raw.get("notes") or "").strip() or None
    return payload, None, None


def _build_supplier_update_result(
    row_number: int,
    raw: dict[str, str],
    *,
    existing: Supplier,
    other_names_lc: set[str],
) -> RowResult:
    """Compute a diff between the CSV cells and an existing supplier row.

    Returns a ``RowResult`` tagged ``update`` (with changes payload),
    ``skip`` (no changes), or ``error``. Empty cells are interpreted as
    "no change" except for ``name`` which is required to stay non-blank.
    """
    payload, err_field, err_msg = _parse_supplier_payload(raw)
    if err_field is not None:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field=err_field, error_message=err_msg or "",
        )
    name = payload["name"]
    if not name:
        # blank name cell on update = leave alone.
        name = existing.name
    if name.lower() != existing.name.lower() and name.lower() in other_names_lc:
        return RowResult(
            row_number=row_number, raw=raw, tag="error",
            error_field="name",
            error_message="another supplier already uses that name",
        )

    changes: dict[str, Any] = {}
    before: dict[str, Any] = {}
    for field in _FIELDS:
        new_val = payload.get(field)
        if field == "name":
            # Use the resolved name (which may be the existing value when
            # the cell was blank). The other fields are taken verbatim.
            new_val = name
        # Compare normalised values.
        old_val = getattr(existing, field)
        if (old_val or None) != (new_val or None):
            changes[field] = new_val
            before[field] = old_val

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


def _build_supplier_row_result(
    row_number: int,
    raw: dict[str, str],
    *,
    existing_by_id: dict[int, Supplier],
    existing_names_lc: set[str],
) -> RowResult:
    """Validate one CSV row; return a tagged ``RowResult``."""
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
            # Build name-uniqueness scope excluding self.
            others = existing_names_lc - {existing.name.lower()}
            return _build_supplier_update_result(
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
            error_message="a supplier with that name already exists",
        )

    email = (raw.get("email") or "").strip() or None
    if email is not None and not _validate_email_shape(email):
        return RowResult(
            row_number=row_number,
            raw=raw,
            tag="error",
            error_field="email",
            error_message="email is not a valid address",
        )
    phone = (raw.get("phone") or "").strip() or None
    notes = (raw.get("notes") or "").strip() or None

    warnings: list[str] = []
    if (raw.get("archived_at") or "").strip():
        warnings.append("archived_at ignored on create — new row lands active")

    return RowResult(
        row_number=row_number,
        raw=raw,
        tag="new",
        payload={"name": name, "email": email, "phone": phone, "notes": notes},
        warnings=warnings,
    )


def _validate_supplier_upload(
    db: Session, headers: list[str], body: list[list[str]]
) -> list[RowResult]:
    check_required_and_known_headers(
        headers,
        known=_SUPPLIERS_UPLOAD_KNOWN,
        required=_SUPPLIERS_UPLOAD_REQUIRED,
    )
    existing_rows = list(db.execute(select(Supplier)).scalars().all())
    existing_by_id: dict[int, Supplier] = {s.id: s for s in existing_rows}
    existing_names_lc = {s.name.lower() for s in existing_rows}

    results: list[RowResult] = []
    for offset, row in enumerate(body):
        # Row 1 is the header; the first data row is row 2 in a spreadsheet.
        row_number = offset + 2
        raw = row_to_dict(headers, row)
        results.append(
            _build_supplier_row_result(
                row_number,
                raw,
                existing_by_id=existing_by_id,
                existing_names_lc=existing_names_lc,
            )
        )
    mark_intra_file_duplicates(results, key="name", case_insensitive=True)
    return results


def _summarise_supplier_row(r: RowResult) -> str:
    if r.payload is None:
        return ""
    if r.tag == "update":
        return "changed: " + ", ".join(sorted(r.payload.get("changes", {})))
    return f"name={r.payload.get('name', '')}"


@upload_router.get("/upload", response_class=HTMLResponse)
def upload_suppliers_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "csv_upload_form.html",
        {
            "current_user": _user,
            "title": "Upload suppliers CSV",
            "subtitle": "Bulk-create suppliers from a CSV. Update-via-CSV is not supported in v1.",
            "intro_html": "",
            "action": "/admin/suppliers/upload",
            "cancel_url": "/admin/suppliers",
            "download_url": "/admin/suppliers?format=csv&show=active",
            "expected_columns": _SUPPLIERS_UPLOAD_COLUMNS,
        },
    )


@upload_router.post("/upload")
async def upload_suppliers(
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
        "title": "Suppliers CSV — preview",
        "subtitle": "",
        "upload_url": "/admin/suppliers/upload",
        "cancel_url": "/admin/suppliers",
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
        results = _validate_supplier_upload(db, headers, body)
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
                    "summary": _summarise_supplier_row(r),
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

    # Commit pass: insert every ``new`` row + apply ``update`` rows in a
    # single transaction, plus a summary audit row carrying the file's
    # sha256.
    for r in results:
        if r.payload is None:
            continue
        if r.tag == "new":
            payload = r.payload
            supplier = Supplier(
                name=payload["name"],
                email=payload["email"],
                phone=payload["phone"],
                notes=payload["notes"],
            )
            db.add(supplier)
            db.flush()
            record_audit(
                db,
                actor=user,
                action="supplier.created",
                entity_type="supplier",
                entity_id=supplier.id,
                before=None,
                after={f: payload[f] for f in _FIELDS},
            )
        elif r.tag == "update":
            payload = r.payload
            existing_id = payload["existing_id"]
            changes = payload["changes"]
            before = payload["before"]
            existing = db.get(Supplier, existing_id)
            if existing is None:  # pragma: no cover — defensive
                continue
            for field, new_val in changes.items():
                setattr(existing, field, new_val)
            record_audit(
                db,
                actor=user,
                action="supplier.updated",
                entity_type="supplier",
                entity_id=existing.id,
                before=before,
                after=changes,
            )
    record_audit(
        db,
        actor=user,
        action="supplier.csv_uploaded",
        entity_type="supplier",
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
        f"Imported {new_count} new, updated {update_count} supplier(s) from CSV.",
    )
    return RedirectResponse(url="/admin/suppliers", status_code=status.HTTP_303_SEE_OTHER)
