"""FastAPI application factory and top-level routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import case, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import audit_routes as audit_routes_module
from app import checkouts as checkouts_module
from app import checkouts_admin as checkouts_admin_module
from app import dashboard as dashboard_module
from app import field_defs as field_defs_module
from app import item_units as item_units_module
from app import items as items_module
from app import locations as locations_module
from app import movements as movements_module
from app import purchase_orders as purchase_orders_module
from app import reorder as reorder_module
from app import reports as reports_module
from app import scan as scan_module
from app import stock_takes as stock_takes_module
from app import suppliers as suppliers_module
from app import taxonomy as taxonomy_module
from app import transfers as transfers_module
from app.audit import record_audit
from app.auth import get_current_user, require_role
from app.auth import router as auth_router
from app.config import settings
from app.csrf import DEFAULT_EXEMPT_PATHS, CSRFMiddleware
from app.csv_export import csv_branch
from app.db import get_session
from app.models import Role, User, UserStatus
from app.template_env import templates

app = FastAPI(title="UC Inventory")

# Middleware order matters: the *last* added is the outermost. We want CSRF to
# run first (outermost) so it can short-circuit forged requests before the
# session cookie is even decoded. SessionMiddleware is added first, then CSRF.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.app_env == "prod",
)
_csrf_exempt = DEFAULT_EXEMPT_PATHS
if settings.oauth_stub_mode:
    # The stub token endpoint receives a server-side POST from Authlib's httpx
    # client — it never carries a CSRF cookie, so exempt it when the stub is
    # active.  This path only exists (and is only reachable) in test mode.
    _csrf_exempt = _csrf_exempt | frozenset({"/auth/_stub/token"})
app.add_middleware(
    CSRFMiddleware,
    secure=settings.app_env == "prod",
    exempt_paths=_csrf_exempt,
)

# Vendored static assets (htmx, etc.). Mounted at /static so templates can
# reference them without a CDN dependency — production deploys behind a
# strict CSP or egress filter would otherwise fail to load HTMX.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

app.include_router(auth_router)
app.include_router(suppliers_module.router)
app.include_router(locations_module.router)
app.include_router(taxonomy_module.router)
app.include_router(field_defs_module.router)
app.include_router(items_module.router)
app.include_router(item_units_module.router)
app.include_router(movements_module.router)
app.include_router(checkouts_module.router)
app.include_router(checkouts_admin_module.router)
app.include_router(reorder_module.router)
app.include_router(purchase_orders_module.draft_router)
app.include_router(purchase_orders_module.list_router)
app.include_router(dashboard_module.router)
app.include_router(stock_takes_module.router)
app.include_router(reports_module.router)
app.include_router(scan_module.router)
app.include_router(audit_routes_module.router)
app.include_router(transfers_module.router)

if settings.oauth_stub_mode:
    from app import oauth_test_stub as oauth_test_stub_module

    app.include_router(oauth_test_stub_module.stub_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, current_user: User | None = Depends(get_current_user)) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "current_user": current_user,
            "dev_login_enabled": settings.app_env in {"dev", "test"},
            "dev_login_email": "dev@dev.com",
        },
    )


# ---------------------------------------------------------------------------
# Admin user management
# ---------------------------------------------------------------------------
#
# Role/status mutations are POST-only so that SameSite=Lax + the eventual CSRF
# token (queued for F4) can protect them. GETs never mutate state.

# Order: pending users first (admin needs to act on these), then active, then
# disabled — within each bucket, newest first.
_USER_LIST_ORDER = case(
    (User.status == UserStatus.PENDING, 0),
    (User.status == UserStatus.ACTIVE, 1),
    else_=2,
)


_ADMIN_USERS_CSV_HEADERS: list[str] = [
    "id",
    "email",
    "name",
    "role",
    "status",
    "created_at",
]


def _csv_rows_for_admin_users(rows: list[User]) -> list[list[object]]:
    """Map ``User`` rows to CSV cell values.

    Mirrors the HTML table cells one-for-one. ``id`` is added at the front so
    a downstream consumer can join (the HTML carries it as ``data-user-id``
    rather than a cell). Enums are pre-coerced to their ``.value`` strings so
    the cell carries ``"manager"`` rather than Python's ``<Role.MANAGER: 'manager'>``
    repr; ``role`` renders empty when ``None`` (pending users with no role).
    ``created_at`` is preserved as a full ISO datetime via ``csv_response``'s
    coercion — the HTML displays only the date, but a downstream consumer
    benefits from the full precision.
    """
    return [
        [
            u.id,
            u.email,
            u.name,
            u.role.value if u.role is not None else None,
            u.status.value,
            u.created_at,
        ]
        for u in rows
    ]


@app.get("/admin/users")
def admin_list_users(
    request: Request,
    format: str = "",
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_session),
) -> Response:
    rows = list(
        db.execute(select(User).order_by(_USER_LIST_ORDER, User.created_at.desc())).scalars().all()
    )

    if (
        resp := csv_branch(
            format,
            filename="users.csv",
            headers=_ADMIN_USERS_CSV_HEADERS,
            rows=_csv_rows_for_admin_users(rows),
        )
    ) is not None:
        return resp

    return templates.TemplateResponse(
        request,
        "admin_users.html",
        {
            "current_user": admin,
            "users": rows,
            "roles": list(Role),
            "statuses": list(UserStatus),
        },
    )


@app.post("/admin/users/{user_id}/role")
def admin_set_user_role(
    user_id: int,
    role: str = Form(""),
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_session),
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    new_role: Role | None
    if role == "":
        new_role = None
    else:
        try:
            new_role = Role(role)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="invalid role"
            ) from exc

    # Self-demotion guard: an admin cannot remove their own admin role. Without
    # this, the only admin could lock everyone out by accident.
    if user.id == admin.id and new_role is not Role.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot change your own role",
        )

    previous_role = user.role
    user.role = new_role
    if previous_role != new_role:
        record_audit(
            db,
            actor=admin,
            action="user.role_assigned",
            entity_type="user",
            entity_id=user.id,
            before={"role": previous_role},
            after={"role": new_role},
        )
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/admin/users/{user_id}/status")
def admin_set_user_status(
    user_id: int,
    status_value: str = Form(..., alias="status"),
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_session),
) -> Response:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")

    try:
        new_status = UserStatus(status_value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status"
        ) from exc

    # Self-status guard: same reasoning as the role guard.
    if user.id == admin.id and new_status is not UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot change your own status",
        )

    # Activating a user with no role would land them on the welcome page with no
    # permissions — a UX trap. Force the admin to assign a role first.
    if new_status is UserStatus.ACTIVE and user.role is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="cannot activate a user with no role assigned",
        )

    previous_status = user.status
    user.status = new_status
    if previous_status != new_status:
        record_audit(
            db,
            actor=admin,
            action="user.status_changed",
            entity_type="user",
            entity_id=user.id,
            before={"status": previous_status},
            after={"status": new_status},
        )
    db.commit()
    return RedirectResponse(url="/admin/users", status_code=status.HTTP_303_SEE_OTHER)
