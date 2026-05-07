"""Authentication: Google OAuth, session helpers, and role-gating dependencies.

The OAuth flow lives behind Authlib. The pure user-upsert logic lives in
``upsert_user_from_userinfo`` so it can be unit-tested without the HTTP
surface area.
"""

from __future__ import annotations

from typing import Any, cast

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from app.audit import record_audit
from app.config import settings
from app.db import get_session
from app.models import Role, User, UserStatus

# ---------------------------------------------------------------------------
# Authlib OAuth client
# ---------------------------------------------------------------------------

oauth = OAuth()
if settings.google_client_id and settings.google_client_secret:
    if settings.oauth_stub_mode:
        # Test-only: register Google against the local stub endpoints so no
        # external network call is needed and no JWT verification fires.
        _stub_base = f"{settings.app_base_url.rstrip('/')}/auth/_stub"
        oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            authorize_url=f"{_stub_base}/authorize",
            access_token_url=f"{_stub_base}/token",
            userinfo_endpoint=f"{_stub_base}/userinfo",
            client_kwargs={"scope": "email profile"},
        )
    else:
        oauth.register(
            name="google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


# ---------------------------------------------------------------------------
# Pure user-upsert logic (tested directly)
# ---------------------------------------------------------------------------


def upsert_user_from_userinfo(
    db: Session,
    userinfo: dict[str, Any],
    *,
    bootstrap_admin_email: str | None,
) -> User:
    """Create-or-update a User from a verified Google ``userinfo`` payload.

    On first sign-in the user is ``pending`` with ``role=None``. If the email
    matches ``bootstrap_admin_email`` AND no admin yet exists, the user is
    promoted to ``admin`` + ``active`` to seed the very first admin account.

    On subsequent sign-ins, name/email are refreshed but role/status are
    preserved (an admin may have already promoted them).
    """
    sub = str(userinfo["sub"])
    email = str(userinfo["email"])
    name = str(userinfo.get("name") or email)

    existing = db.execute(select(User).where(User.google_sub == sub)).scalar_one_or_none()
    if existing is not None:
        existing.email = email
        existing.name = name
        return existing

    role: Role | None = None
    status_: UserStatus = UserStatus.PENDING
    bootstrap_promoted = False
    if bootstrap_admin_email and email.lower() == bootstrap_admin_email.lower():
        admin_exists = db.execute(
            select(func.count()).select_from(User).where(User.role == Role.ADMIN)
        ).scalar_one()
        if not admin_exists:
            role = Role.ADMIN
            status_ = UserStatus.ACTIVE
            bootstrap_promoted = True

    user = User(google_sub=sub, email=email, name=name, role=role, status=status_)
    db.add(user)
    db.flush()

    # Audit: the user is the actor of their own creation. The first sign-in is
    # the only user-creation path, and we want a row in the log even though
    # nobody else triggered it.
    record_audit(
        db,
        actor=user,
        action="user.created",
        entity_type="user",
        entity_id=user.id,
        before=None,
        after={"email": user.email, "role": user.role, "status": user.status},
    )
    if bootstrap_promoted:
        # Separate row so the *why* of the elevated initial role is auditable
        # even though it lands in the same DB transaction as user.created.
        record_audit(
            db,
            actor=None,  # system event: no admin existed yet to grant this
            action="user.bootstrap_admin_granted",
            entity_type="user",
            entity_id=user.id,
            before={"role": None, "status": UserStatus.PENDING},
            after={"role": Role.ADMIN, "status": UserStatus.ACTIVE},
        )
    return user


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_current_user(
    request: Request, db: Session = Depends(get_session)
) -> User | None:
    """Resolve the logged-in user from the session cookie, or ``None``."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def require_user(current_user: User | None = Depends(get_current_user)) -> User:
    """Reject unauthenticated requests with a 401."""
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    return current_user


def require_active_user(current_user: User = Depends(require_user)) -> User:
    """Reject signed-in but non-active users (pending, disabled) with a 403.

    Routes that should reflect *current* account state — including any future
    user-data endpoint — should depend on this rather than ``require_user``.
    Without it, a user disabled mid-session continues to read their own
    profile via a still-valid cookie until they sign out.
    """
    if current_user.status is not UserStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="account not active",
        )
    return current_user


def require_role(*allowed: Role):  # type: ignore[no-untyped-def]
    """Build a FastAPI dependency that requires the user to hold one of ``allowed``.

    Admins always pass. Pending or disabled users are blocked even if their
    stored role would otherwise match — only active users get role checks.
    """
    allowed_set = set(allowed)

    def _dep(current_user: User = Depends(require_user)) -> User:
        if current_user.status is not UserStatus.ACTIVE:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="account not active",
            )
        if current_user.role is Role.ADMIN:
            return current_user
        if current_user.role in allowed_set:
            return current_user
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="insufficient role",
        )

    return _dep


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/google/login")
async def google_login(request: Request) -> Response:
    google = oauth.create_client("google")
    if google is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google SSO is not configured",
        )
    redirect_uri = f"{settings.app_base_url.rstrip('/')}/auth/google/callback"
    return cast(
        Response,
        await google.authorize_redirect(request, redirect_uri),
    )


@router.get("/google/callback")
async def google_callback(
    request: Request, db: Session = Depends(get_session)
) -> Response:
    google = oauth.create_client("google")
    if google is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google SSO is not configured",
        )
    try:
        token = await google.authorize_access_token(request)
    except OAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth error: {exc.error}",
        ) from exc

    userinfo = token.get("userinfo")
    if userinfo is None:
        # Some providers return userinfo separately; fetch it explicitly.
        userinfo = await google.userinfo(token=token)
    if not userinfo or "sub" not in userinfo or "email" not in userinfo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OAuth response missing user identity",
        )

    user = upsert_user_from_userinfo(
        db,
        dict(userinfo),
        bootstrap_admin_email=settings.bootstrap_admin_email or None,
    )
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/logout")
async def logout(request: Request) -> Response:
    request.session.pop("user_id", None)
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/me")
async def me(current_user: User = Depends(require_active_user)) -> JSONResponse:
    return JSONResponse(
        {
            "id": current_user.id,
            "email": current_user.email,
            "name": current_user.name,
            "role": current_user.role.value if current_user.role else None,
            "status": current_user.status.value,
        }
    )


# ---------------------------------------------------------------------------
# Dev-only login backdoor
# ---------------------------------------------------------------------------
#
# Mounted only in dev/test. Lets Playwright (and local dev) sign in without
# touching real Google. Hard-gated by ``settings.app_env`` and refuses to do
# anything in prod.

if settings.app_env in {"dev", "test"}:

    @router.post("/_dev-login")
    async def dev_login(
        request: Request,
        email: str = Form(...),
        name: str = Form("Dev User"),
        sub: str | None = Form(None),
        db: Session = Depends(get_session),
    ) -> Response:
        if settings.app_env not in {"dev", "test"}:  # belt and braces
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        userinfo = {"sub": sub or f"dev-{email}", "email": email, "name": name}
        user = upsert_user_from_userinfo(
            db, userinfo, bootstrap_admin_email=settings.bootstrap_admin_email or None
        )
        db.commit()
        request.session["user_id"] = user.id
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
