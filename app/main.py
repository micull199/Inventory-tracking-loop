"""FastAPI application factory and top-level routes."""

from __future__ import annotations

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import case, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import dashboard as dashboard_module
from app import field_defs as field_defs_module
from app import item_units as item_units_module
from app import items as items_module
from app import locations as locations_module
from app import movements as movements_module
from app import purchase_orders as purchase_orders_module
from app import reorder as reorder_module
from app import suppliers as suppliers_module
from app import taxonomy as taxonomy_module
from app.audit import record_audit
from app.auth import get_current_user, require_role
from app.auth import router as auth_router
from app.config import settings
from app.csrf import CSRFMiddleware
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
app.add_middleware(
    CSRFMiddleware,
    secure=settings.app_env == "prod",
)

app.include_router(auth_router)
app.include_router(suppliers_module.router)
app.include_router(locations_module.router)
app.include_router(taxonomy_module.router)
app.include_router(field_defs_module.router)
app.include_router(items_module.router)
app.include_router(item_units_module.router)
app.include_router(movements_module.router)
app.include_router(reorder_module.router)
app.include_router(purchase_orders_module.draft_router)
app.include_router(purchase_orders_module.list_router)
app.include_router(dashboard_module.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request, current_user: User | None = Depends(get_current_user)
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "index.html", {"current_user": current_user}
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


@app.get("/admin/users", response_class=HTMLResponse)
def admin_list_users(
    request: Request,
    admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_session),
) -> HTMLResponse:
    rows = (
        db.execute(select(User).order_by(_USER_LIST_ORDER, User.created_at.desc()))
        .scalars()
        .all()
    )
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
