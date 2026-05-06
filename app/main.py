"""FastAPI application factory and top-level routes."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import get_current_user, require_role
from app.auth import router as auth_router
from app.config import settings
from app.db import get_session
from app.models import Role, User

app = FastAPI(title="UC Inventory")

app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.app_env == "prod",
)

app.include_router(auth_router)

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


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


@app.get("/admin/users")
def admin_list_users(
    _admin: User = Depends(require_role(Role.ADMIN)),
    db: Session = Depends(get_session),
) -> JSONResponse:
    """List all users. Admin-only — used by the admin user-management UI (next slice)."""
    rows = db.execute(select(User).order_by(User.created_at.desc())).scalars().all()
    return JSONResponse(
        [
            {
                "id": u.id,
                "email": u.email,
                "name": u.name,
                "role": u.role.value if u.role else None,
                "status": u.status.value,
            }
            for u in rows
        ]
    )
