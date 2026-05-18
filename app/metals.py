"""Manager-owned metals admin: ``/admin/metals`` + ``/admin/metal-prices``.

Two related route surfaces in one module — they share the metal lookup
and the operator workflow:

- ``/admin/metals`` (CRUD) — soft-deletable lookup mirroring the
  stone-shapes / suppliers / locations pattern. Manager-administered.
  Seeded with 14 canonical alloys in migration 0030; new entries are
  rare (custom alloys, region-specific stamps).
- ``/admin/metal-prices`` (daily entries) — spec §2.2 v1 path:
  "manual entry by manager". One row per metal per day enforced by
  the ``uq_metal_spot_prices_date`` unique index.

Access: ``Manager`` + ``Admin`` only. Workshop and Office both 403,
per MISSION §3.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.audit import record_audit
from app.auth import require_role
from app.db import get_session
from app.models import AlloyFamily, Metal, MetalColour, MetalSpotPrice, Role, User
from app.template_env import templates

router = APIRouter(prefix="/admin/metals", tags=["metals"])
prices_router = APIRouter(prefix="/admin/metal-prices", tags=["metal-prices"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _flash(request: Request, message: str) -> None:
    request.session["flash"] = message


def _parse_required_decimal(raw: str, field_name: str) -> Decimal:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a number",
        ) from exc


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


def _parse_optional_int(raw: str, field_name: str) -> int | None:
    text = (raw or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be a whole number",
        ) from exc


def _parse_required_date(raw: str, field_name: str) -> date:
    text = (raw or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} is required",
        )
    try:
        return datetime.fromisoformat(text).date()
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field_name} must be an ISO date (YYYY-MM-DD)",
        ) from exc


def _parse_enum(enum_cls: type, raw: str, *, field_name: str) -> Any:
    text = (raw or "").strip()
    if not text:
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


# ---------------------------------------------------------------------------
# Metals CRUD
# ---------------------------------------------------------------------------

_METAL_FIELDS: tuple[str, ...] = (
    "metal_code",
    "name",
    "alloy_family",
    "karat",
    "purity_pct",
    "colour",
    "density_g_per_cc",
    "hallmark_stamp",
)


def _normalise_metal(form: dict[str, str]) -> dict[str, Any]:
    metal_code = (form.get("metal_code") or "").strip().upper()
    if not metal_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="metal_code is required",
        )
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="name is required"
        )
    alloy_family = _parse_enum(
        AlloyFamily, form.get("alloy_family") or "", field_name="alloy_family"
    )
    karat = _parse_optional_int(form.get("karat") or "", "karat")
    # Karat is meaningful only for gold; reject mismatches so the data
    # stays consistent. Spec §2.1: "9/14/18/22/24 for gold; null for
    # non-gold".
    if alloy_family is not AlloyFamily.GOLD and karat is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="karat applies only to gold alloys",
        )
    purity_pct = _parse_required_decimal(form.get("purity_pct") or "", "purity_pct")
    if purity_pct <= 0 or purity_pct > 100:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="purity_pct must be between 0 and 100",
        )
    colour = _parse_enum(MetalColour, form.get("colour") or "", field_name="colour")
    return {
        "metal_code": metal_code,
        "name": name,
        "alloy_family": alloy_family,
        "karat": karat,
        "purity_pct": purity_pct,
        "colour": colour,
        "density_g_per_cc": _parse_optional_decimal(
            form.get("density_g_per_cc") or "", "density_g_per_cc"
        ),
        "hallmark_stamp": (form.get("hallmark_stamp") or "").strip() or None,
    }


def _check_metal_code_unique(
    db: Session, metal_code: str, *, exclude_id: int | None = None
) -> None:
    stmt = select(Metal.id).where(Metal.metal_code == metal_code)
    if exclude_id is not None:
        stmt = stmt.where(Metal.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a metal with that code already exists",
        )


def _diff_metal(
    metal: Metal, new: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _METAL_FIELDS:
        old = getattr(metal, f)
        new_v = new[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v
    return (before, after) if before else None


def _empty_metal_form() -> dict[str, str]:
    return {
        "metal_code": "",
        "name": "",
        "alloy_family": "gold",
        "karat": "",
        "purity_pct": "",
        "colour": "yellow",
        "density_g_per_cc": "",
        "hallmark_stamp": "",
    }


def _metal_form_view(metal: Metal) -> dict[str, str]:
    def _s(v: Any) -> str:
        return str(v) if v is not None else ""

    return {
        "metal_code": metal.metal_code,
        "name": metal.name,
        "alloy_family": metal.alloy_family.value,
        "karat": _s(metal.karat),
        "purity_pct": _s(metal.purity_pct),
        "colour": metal.colour.value,
        "density_g_per_cc": _s(metal.density_g_per_cc),
        "hallmark_stamp": metal.hallmark_stamp or "",
    }


_METAL_LIST_ORDER = case((Metal.archived_at.is_(None), 0), else_=1)


@router.get("")
def list_metals(
    request: Request,
    show: str = "active",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    if show not in {"active", "archived"}:
        show = "active"
    stmt = select(Metal)
    if show == "active":
        stmt = stmt.where(Metal.archived_at.is_(None))
    else:
        stmt = stmt.where(Metal.archived_at.is_not(None))
    stmt = stmt.order_by(_METAL_LIST_ORDER, Metal.alloy_family, Metal.metal_code)
    rows = list(db.execute(stmt).scalars().all())
    return templates.TemplateResponse(
        request,
        "metals_list.html",
        {
            "current_user": _user,
            "metals": rows,
            "show": show,
        },
    )


@router.get("/new", response_class=HTMLResponse)
def new_metal_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "metals_form.html",
        {
            "current_user": _user,
            "metal": None,
            "form": _empty_metal_form(),
            "title": "New metal",
            "action": "/admin/metals",
            "alloy_families": [f.value for f in AlloyFamily],
            "colours": [c.value for c in MetalColour],
        },
    )


@router.post("")
def create_metal(
    request: Request,
    metal_code: str = Form(""),
    name: str = Form(""),
    alloy_family: str = Form("gold"),
    karat: str = Form(""),
    purity_pct: str = Form(""),
    colour: str = Form("yellow"),
    density_g_per_cc: str = Form(""),
    hallmark_stamp: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise_metal(
        {
            "metal_code": metal_code,
            "name": name,
            "alloy_family": alloy_family,
            "karat": karat,
            "purity_pct": purity_pct,
            "colour": colour,
            "density_g_per_cc": density_g_per_cc,
            "hallmark_stamp": hallmark_stamp,
        }
    )
    _check_metal_code_unique(db, fields["metal_code"])
    metal = Metal(**fields)
    db.add(metal)
    db.flush()
    record_audit(
        db,
        actor=user,
        action="metal.created",
        entity_type="metal",
        entity_id=metal.id,
        before=None,
        after={f: fields[f] for f in _METAL_FIELDS},
    )
    db.commit()
    _flash(request, f"Metal “{metal.metal_code}” created.")
    return RedirectResponse(
        url="/admin/metals", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/{metal_id}/edit", response_class=HTMLResponse)
def edit_metal_form(
    request: Request,
    metal_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    metal = db.get(Metal, metal_id)
    if metal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metal not found"
        )
    return templates.TemplateResponse(
        request,
        "metals_form.html",
        {
            "current_user": _user,
            "metal": metal,
            "form": _metal_form_view(metal),
            "title": f"Edit {metal.metal_code}",
            "action": f"/admin/metals/{metal.id}",
            "alloy_families": [f.value for f in AlloyFamily],
            "colours": [c.value for c in MetalColour],
        },
    )


@router.post("/{metal_id}")
def update_metal(
    request: Request,
    metal_id: int,
    metal_code: str = Form(""),
    name: str = Form(""),
    alloy_family: str = Form("gold"),
    karat: str = Form(""),
    purity_pct: str = Form(""),
    colour: str = Form("yellow"),
    density_g_per_cc: str = Form(""),
    hallmark_stamp: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    metal = db.get(Metal, metal_id)
    if metal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metal not found"
        )
    fields = _normalise_metal(
        {
            "metal_code": metal_code,
            "name": name,
            "alloy_family": alloy_family,
            "karat": karat,
            "purity_pct": purity_pct,
            "colour": colour,
            "density_g_per_cc": density_g_per_cc,
            "hallmark_stamp": hallmark_stamp,
        }
    )
    _check_metal_code_unique(db, fields["metal_code"], exclude_id=metal.id)

    diff = _diff_metal(metal, fields)
    if diff is not None:
        before, after = diff
        for f in _METAL_FIELDS:
            setattr(metal, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="metal.updated",
            entity_type="metal",
            entity_id=metal.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, f"Metal “{metal.metal_code}” updated.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/metals", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{metal_id}/archive")
def archive_metal(
    request: Request,
    metal_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    metal = db.get(Metal, metal_id)
    if metal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metal not found"
        )
    if metal.archived_at is None:
        metal.archived_at = datetime.now(UTC)
        record_audit(
            db,
            actor=user,
            action="metal.archived",
            entity_type="metal",
            entity_id=metal.id,
            before={"archived_at": None},
            after={"archived_at": metal.archived_at},
        )
        db.commit()
        _flash(request, f"Metal “{metal.metal_code}” archived.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/metals", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/{metal_id}/unarchive")
def unarchive_metal(
    request: Request,
    metal_id: int,
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    metal = db.get(Metal, metal_id)
    if metal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metal not found"
        )
    if metal.archived_at is not None:
        previous = metal.archived_at
        metal.archived_at = None
        record_audit(
            db,
            actor=user,
            action="metal.unarchived",
            entity_type="metal",
            entity_id=metal.id,
            before={"archived_at": previous},
            after={"archived_at": None},
        )
        db.commit()
        _flash(request, f"Metal “{metal.metal_code}” restored.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/metals", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Metal spot prices (spec §2.2 v1 path)
# ---------------------------------------------------------------------------

_PRICE_FIELDS: tuple[str, ...] = (
    "metal_id",
    "as_of_date",
    "price_per_gram",
    "source",
    "notes",
)


def _normalise_price(db: Session, form: dict[str, str]) -> dict[str, Any]:
    metal_id_raw = (form.get("metal_id") or "").strip()
    try:
        metal_id = int(metal_id_raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="metal_id is required",
        ) from exc
    metal = db.get(Metal, metal_id)
    if metal is None or metal.archived_at is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="metal not found or archived",
        )
    as_of_date = _parse_required_date(form.get("as_of_date") or "", "as_of_date")
    price_per_gram = _parse_required_decimal(
        form.get("price_per_gram") or "", "price_per_gram"
    )
    if price_per_gram <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="price_per_gram must be greater than zero",
        )
    source = (form.get("source") or "").strip()
    if not source:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source is required (e.g. 'manual', 'lbma_pm_fix', 'kitco')",
        )
    return {
        "metal_id": metal_id,
        "as_of_date": as_of_date,
        "price_per_gram": price_per_gram,
        "source": source,
        "notes": (form.get("notes") or "").strip() or None,
    }


def _check_price_unique_per_day(
    db: Session, metal_id: int, as_of_date: date, *, exclude_id: int | None = None
) -> None:
    """Enforce one price per metal per day (matches the partial-unique index).

    The DB constraint would catch this too, but surfacing the clash in
    the route layer gives a friendlier error than a deferred IntegrityError.
    """
    stmt = (
        select(MetalSpotPrice.id)
        .where(MetalSpotPrice.metal_id == metal_id)
        .where(MetalSpotPrice.as_of_date == as_of_date)
    )
    if exclude_id is not None:
        stmt = stmt.where(MetalSpotPrice.id != exclude_id)
    if db.execute(stmt).first() is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a price for that metal on that date already exists; edit it instead",
        )


def _active_metal_options(db: Session) -> list[dict[str, Any]]:
    rows: list[Metal] = list(
        db.execute(
            select(Metal)
            .where(Metal.archived_at.is_(None))
            .order_by(Metal.alloy_family, Metal.metal_code)
        ).scalars().all()
    )
    return [
        {"id": m.id, "label": f"{m.metal_code} — {m.name}"}
        for m in rows
    ]


def _empty_price_form() -> dict[str, str]:
    return {
        "metal_id": "",
        "as_of_date": datetime.now(UTC).date().isoformat(),
        "price_per_gram": "",
        "source": "manual",
        "notes": "",
    }


def _price_form_view(price: MetalSpotPrice) -> dict[str, str]:
    return {
        "metal_id": str(price.metal_id),
        "as_of_date": price.as_of_date.isoformat(),
        "price_per_gram": str(price.price_per_gram),
        "source": price.source,
        "notes": price.notes or "",
    }


@prices_router.get("")
def list_metal_prices(
    request: Request,
    metal_id: str = "",
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    # Optional filter: ?metal_id=<int> narrows to one metal's history.
    # Invalid / blank id falls through to "all metals".
    parsed_metal_id: int | None = None
    if metal_id.strip():
        try:
            parsed_metal_id = int(metal_id)
        except ValueError:
            parsed_metal_id = None

    stmt = select(MetalSpotPrice).order_by(
        MetalSpotPrice.as_of_date.desc(), MetalSpotPrice.metal_id
    )
    if parsed_metal_id is not None:
        stmt = stmt.where(MetalSpotPrice.metal_id == parsed_metal_id)
    rows = list(db.execute(stmt).scalars().all())
    # Resolve metal labels up-front so the template doesn't have to join.
    metal_by_id: dict[int, Metal] = {
        m.id: m
        for m in db.execute(select(Metal)).scalars().all()
    }
    view_rows = [
        {
            "price": p,
            "metal_label": (
                f"{metal_by_id[p.metal_id].metal_code} — {metal_by_id[p.metal_id].name}"
                if p.metal_id in metal_by_id
                else "(unknown)"
            ),
        }
        for p in rows
    ]
    return templates.TemplateResponse(
        request,
        "metal_prices_list.html",
        {
            "current_user": _user,
            "rows": view_rows,
            "metal_options": _active_metal_options(db),
            "selected_metal_id": parsed_metal_id,
        },
    )


@prices_router.get("/new", response_class=HTMLResponse)
def new_metal_price_form(
    request: Request,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "metal_prices_form.html",
        {
            "current_user": _user,
            "price": None,
            "form": _empty_price_form(),
            "title": "New metal price",
            "action": "/admin/metal-prices",
            "metal_options": _active_metal_options(db),
        },
    )


@prices_router.post("")
def create_metal_price(
    request: Request,
    metal_id: str = Form(""),
    as_of_date: str = Form(""),
    price_per_gram: str = Form(""),
    source: str = Form("manual"),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    fields = _normalise_price(
        db,
        {
            "metal_id": metal_id,
            "as_of_date": as_of_date,
            "price_per_gram": price_per_gram,
            "source": source,
            "notes": notes,
        },
    )
    _check_price_unique_per_day(db, fields["metal_id"], fields["as_of_date"])

    price = MetalSpotPrice(**fields)
    db.add(price)
    db.flush()
    record_audit(
        db,
        actor=user,
        action="metal_price.created",
        entity_type="metal_price",
        entity_id=price.id,
        before=None,
        after={f: fields[f] for f in _PRICE_FIELDS},
    )
    db.commit()
    _flash(request, f"Price recorded for {fields['as_of_date']}.")
    return RedirectResponse(
        url="/admin/metal-prices", status_code=status.HTTP_303_SEE_OTHER
    )


@prices_router.get("/{price_id}/edit", response_class=HTMLResponse)
def edit_metal_price_form(
    request: Request,
    price_id: int,
    _user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    price = db.get(MetalSpotPrice, price_id)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metal price not found"
        )
    return templates.TemplateResponse(
        request,
        "metal_prices_form.html",
        {
            "current_user": _user,
            "price": price,
            "form": _price_form_view(price),
            "title": f"Edit price for {price.as_of_date}",
            "action": f"/admin/metal-prices/{price.id}",
            "metal_options": _active_metal_options(db),
        },
    )


@prices_router.post("/{price_id}")
def update_metal_price(
    request: Request,
    price_id: int,
    metal_id: str = Form(""),
    as_of_date: str = Form(""),
    price_per_gram: str = Form(""),
    source: str = Form("manual"),
    notes: str = Form(""),
    user: User = Depends(require_role(Role.MANAGER)),
    db: Session = Depends(get_session),
) -> Response:
    price = db.get(MetalSpotPrice, price_id)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="metal price not found"
        )
    fields = _normalise_price(
        db,
        {
            "metal_id": metal_id,
            "as_of_date": as_of_date,
            "price_per_gram": price_per_gram,
            "source": source,
            "notes": notes,
        },
    )
    _check_price_unique_per_day(
        db, fields["metal_id"], fields["as_of_date"], exclude_id=price.id
    )

    # Diff and apply. Edits are typo corrections — same shape as the
    # other admin update routes.
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for f in _PRICE_FIELDS:
        old = getattr(price, f)
        new_v = fields[f]
        if old != new_v:
            before[f] = old
            after[f] = new_v

    if before:
        for f in _PRICE_FIELDS:
            setattr(price, f, fields[f])
        record_audit(
            db,
            actor=user,
            action="metal_price.updated",
            entity_type="metal_price",
            entity_id=price.id,
            before=before,
            after=after,
        )
        db.commit()
        _flash(request, "Metal price updated.")
    else:
        db.rollback()
    return RedirectResponse(
        url="/admin/metal-prices", status_code=status.HTTP_303_SEE_OTHER
    )
