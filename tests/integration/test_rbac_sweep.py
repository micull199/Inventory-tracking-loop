"""Consolidated RBAC sweep — one test per (route, role) cell.

DoD #9: "Role-based access enforced server-side. A Workshop user hitting a
Manager-only URL gets a 403, verified by tests."

Why a sweep
-----------
Per-route role-enforcement tests already exist (see e.g.
``test_suppliers_routes.py::TestRoleEnforcement``). They cover the role gates
for ~25 of the 90 RBAC-gated routes. This file is the consolidated boundary
test: every live RBAC route x every role lands in the parametrized matrix, so
the suite fails immediately if a new route ships without a gate or with the
wrong gate.

The boundary check at the bottom (`test_rbac_table_covers_all_live_routes`)
enumerates `app.routes` and asserts the table covers every gate-eligible
route. Auth-bootstrap routes (`/auth/google/*`, `/auth/_dev-login`,
`/auth/logout`) are explicitly excluded — they are not RBAC gates.

What is asserted
----------------
- Disallowed roles must get exactly 401 (anon) or 403 (pending or
  insufficient-active-role).
- Allowed roles must get a status code that is NOT 401 and NOT 403. Anything
  else (200, 303, 400, 404, 422) means "the gate let me through" — which is
  what DoD #9 cares about. Happy-path response shapes are pinned by per-route
  test files; they are not this sweep's concern.

Why path-param 99999
--------------------
The role gate fires before the handler runs, so a non-existent path-param ID
is fine: blocked roles 401/403 from the gate; allowed roles get a 404 from
the body's entity lookup. Either outcome verifies the gate behaviour without
needing a fully-seeded entity graph for every cell.
"""

from __future__ import annotations

import re

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import Role, User, UserStatus

# ---------------------------------------------------------------------------
# Gate definitions
# ---------------------------------------------------------------------------

PUBLIC = "public"
ACTIVE = "active"
WORKSHOP = "workshop"
OFFICE = "office"
MANAGER = "manager"
ADMIN = "admin"

ROLE_ANON = "anon"
ROLE_PENDING = "pending"
ROLE_WORKSHOP = "workshop"
ROLE_OFFICE = "office"
ROLE_MANAGER = "manager"
ROLE_ADMIN = "admin"

ALL_ROLES: tuple[str, ...] = (
    ROLE_ANON,
    ROLE_PENDING,
    ROLE_WORKSHOP,
    ROLE_OFFICE,
    ROLE_MANAGER,
    ROLE_ADMIN,
)

ALL_ACTIVE: frozenset[str] = frozenset({ROLE_WORKSHOP, ROLE_OFFICE, ROLE_MANAGER, ROLE_ADMIN})

# Per-gate: which roles get through the gate. Everyone else gets 401 (anon)
# or 403 (any signed-in user without sufficient privilege).
ALLOWED: dict[str, frozenset[str]] = {
    PUBLIC: frozenset(ALL_ROLES),
    ACTIVE: ALL_ACTIVE,
    WORKSHOP: ALL_ACTIVE,
    OFFICE: frozenset({ROLE_OFFICE, ROLE_MANAGER, ROLE_ADMIN}),
    MANAGER: frozenset({ROLE_MANAGER, ROLE_ADMIN}),
    ADMIN: frozenset({ROLE_ADMIN}),
}


# ---------------------------------------------------------------------------
# Route table — every RBAC-gated route in the app
# ---------------------------------------------------------------------------
#
# Entries are (method, path, gate). Path-param placeholders use the literal
# 99999 — see the module docstring for why.

ROUTES: list[tuple[str, str, str]] = [
    # --- Public (no auth) ---
    ("GET", "/", PUBLIC),
    ("GET", "/health", PUBLIC),
    # --- Active-only (any active role) ---
    ("GET", "/auth/me", ACTIVE),
    # --- Admin-only ---
    ("GET", "/admin/users", ADMIN),
    ("POST", "/admin/users/99999/role", ADMIN),
    ("POST", "/admin/users/99999/status", ADMIN),
    # --- Manager-only: suppliers ---
    ("GET", "/admin/suppliers", MANAGER),
    ("GET", "/admin/suppliers/new", MANAGER),
    ("POST", "/admin/suppliers", MANAGER),
    ("GET", "/admin/suppliers/99999/edit", MANAGER),
    ("POST", "/admin/suppliers/99999", MANAGER),
    ("POST", "/admin/suppliers/99999/archive", MANAGER),
    ("POST", "/admin/suppliers/99999/unarchive", MANAGER),
    # --- Manager-only: locations ---
    ("GET", "/admin/locations", MANAGER),
    ("GET", "/admin/locations/new", MANAGER),
    ("POST", "/admin/locations", MANAGER),
    ("GET", "/admin/locations/99999/edit", MANAGER),
    ("POST", "/admin/locations/99999", MANAGER),
    ("POST", "/admin/locations/99999/archive", MANAGER),
    ("POST", "/admin/locations/99999/unarchive", MANAGER),
    # --- Manager-only: taxonomy (top-level + sub + field defs) ---
    ("GET", "/admin/taxonomy", MANAGER),
    ("GET", "/admin/taxonomy/new", MANAGER),
    ("POST", "/admin/taxonomy", MANAGER),
    ("GET", "/admin/taxonomy/99999/edit", MANAGER),
    ("POST", "/admin/taxonomy/99999", MANAGER),
    ("POST", "/admin/taxonomy/99999/archive", MANAGER),
    ("POST", "/admin/taxonomy/99999/unarchive", MANAGER),
    ("GET", "/admin/taxonomy/99999/children", MANAGER),
    ("GET", "/admin/taxonomy/99999/children/new", MANAGER),
    ("POST", "/admin/taxonomy/99999/children", MANAGER),
    ("GET", "/admin/taxonomy/sub/99999/edit", MANAGER),
    ("POST", "/admin/taxonomy/sub/99999", MANAGER),
    ("POST", "/admin/taxonomy/sub/99999/archive", MANAGER),
    ("POST", "/admin/taxonomy/sub/99999/unarchive", MANAGER),
    # Depth-2 grandchildren list + create (taxonomy refinement).
    ("GET", "/admin/taxonomy/99999/sub/99999/grandchildren", MANAGER),
    ("GET", "/admin/taxonomy/99999/sub/99999/grandchildren/new", MANAGER),
    ("POST", "/admin/taxonomy/99999/sub/99999/grandchildren", MANAGER),
    ("GET", "/admin/taxonomy/99999/fields", MANAGER),
    ("GET", "/admin/taxonomy/99999/fields/new", MANAGER),
    ("POST", "/admin/taxonomy/99999/fields", MANAGER),
    ("POST", "/admin/taxonomy/99999/fields/visibility", MANAGER),
    # HTMX fragment: options-textarea visibility per type — Manager-only,
    # same gate as the form that drives it.
    ("GET", "/admin/taxonomy/fields/_options-partial", MANAGER),
    ("GET", "/admin/taxonomy/fields/99999/edit", MANAGER),
    ("POST", "/admin/taxonomy/fields/99999", MANAGER),
    ("POST", "/admin/taxonomy/fields/99999/archive", MANAGER),
    ("POST", "/admin/taxonomy/fields/99999/unarchive", MANAGER),
    # Lifecycle stages CRUD — Manager-only. Stages are owned by a top-level
    # taxonomy node; per-item transitions live under /admin/items further
    # down with the workshop+ surface.
    ("GET", "/admin/taxonomy/99999/stages", MANAGER),
    ("GET", "/admin/taxonomy/99999/stages/new", MANAGER),
    ("POST", "/admin/taxonomy/99999/stages", MANAGER),
    ("GET", "/admin/taxonomy/stages/99999/edit", MANAGER),
    ("POST", "/admin/taxonomy/stages/99999", MANAGER),
    ("POST", "/admin/taxonomy/stages/99999/archive", MANAGER),
    ("POST", "/admin/taxonomy/stages/99999/unarchive", MANAGER),
    # --- Manager-only: audit log read view ---
    ("GET", "/admin/audit", MANAGER),
    # --- Manager-only: items create / archive / unarchive ---
    ("GET", "/admin/items/new", MANAGER),
    # HTMX fragment: custom-fields swap on category change. Same gate as the
    # edit form (Manager + Office + Workshop) — Office/Workshop see the
    # category select disabled in read-only mode and won't fire the swap,
    # but the permissive gate keeps a future widening of the form's
    # writable surface from silently 403'ing on the fragment.
    ("GET", "/admin/items/_custom-fields", WORKSHOP),
    # HTMX fragment: leaf-only category picker search. Same permissive gate
    # as ``_custom-fields`` so the picker renders + searches under all
    # roles; the create POST itself is Manager-only.
    ("GET", "/admin/items/_category-search", WORKSHOP),
    ("POST", "/admin/items", MANAGER),
    ("POST", "/admin/items/99999/archive", MANAGER),
    ("POST", "/admin/items/99999/unarchive", MANAGER),
    # --- Manager-only: item units create / archive / unarchive ---
    ("GET", "/admin/items/99999/units/new", MANAGER),
    ("POST", "/admin/items/99999/units", MANAGER),
    ("POST", "/admin/items/units/99999/archive", MANAGER),
    ("POST", "/admin/items/units/99999/unarchive", MANAGER),
    # --- Manager + Office: items edit POST ---
    ("POST", "/admin/items/99999", OFFICE),
    # --- Manager + Office: item units list / edit ---
    ("GET", "/admin/items/99999/units", OFFICE),
    ("GET", "/admin/items/units/99999/edit", OFFICE),
    ("POST", "/admin/items/units/99999", OFFICE),
    # --- Manager + Office: dashboard / reorder / reports / checkouts admin ---
    ("GET", "/admin/dashboard", OFFICE),
    ("GET", "/admin/reorder", OFFICE),
    ("POST", "/admin/reorder/draft-po", OFFICE),
    ("GET", "/admin/reports/variance-trend", OFFICE),
    ("GET", "/admin/checkouts", OFFICE),
    # --- Manager + Office: purchase orders ---
    ("GET", "/admin/purchase-orders", OFFICE),
    ("GET", "/admin/purchase-orders/99999", OFFICE),
    ("POST", "/admin/purchase-orders/99999", OFFICE),
    ("POST", "/admin/purchase-orders/99999/cancel", OFFICE),
    ("GET", "/admin/purchase-orders/99999/pdf", OFFICE),
    ("POST", "/admin/purchase-orders/99999/send", OFFICE),
    ("GET", "/admin/purchase-orders/99999/receive", OFFICE),
    ("POST", "/admin/purchase-orders/99999/receive", OFFICE),
    # --- Manager + Office: stock takes ---
    ("GET", "/admin/stock-takes", OFFICE),
    ("GET", "/admin/stock-takes/new", OFFICE),
    ("POST", "/admin/stock-takes", OFFICE),
    ("GET", "/admin/stock-takes/99999", OFFICE),
    ("POST", "/admin/stock-takes/99999/start", OFFICE),
    ("POST", "/admin/stock-takes/99999/counts", OFFICE),
    ("POST", "/admin/stock-takes/99999/commit", OFFICE),
    # --- Workshop+: items list / read ---
    ("GET", "/admin/items", WORKSHOP),
    ("GET", "/admin/items/99999/edit", WORKSHOP),
    # --- Workshop+: movements ---
    ("GET", "/admin/items/99999/in", WORKSHOP),
    ("POST", "/admin/items/99999/in", WORKSHOP),
    ("GET", "/admin/items/99999/out", WORKSHOP),
    ("POST", "/admin/items/99999/out", WORKSHOP),
    ("GET", "/admin/items/99999/adjust", WORKSHOP),
    ("POST", "/admin/items/99999/adjust", WORKSHOP),
    ("GET", "/admin/items/99999/transfer", WORKSHOP),
    ("POST", "/admin/items/99999/transfer", WORKSHOP),
    ("GET", "/admin/items/99999/detail", WORKSHOP),
    # Lifecycle stage transition on an item — Workshop+ (same writer surface
    # as the stock movements above). The owning stages are configured by
    # Manager on the taxonomy admin block above.
    ("GET", "/admin/items/99999/stage", WORKSHOP),
    ("POST", "/admin/items/99999/stage", WORKSHOP),
    # Internal transfer orders (Slice 2 of the in-transit scope addition).
    # Workshop reads list + detail; Office + Manager create / ship / receive /
    # cancel. Cost engine is never invoked on these movements.
    ("GET", "/admin/transfers", WORKSHOP),
    ("GET", "/admin/transfers/99999", WORKSHOP),
    ("GET", "/admin/transfers/new", OFFICE),
    ("POST", "/admin/transfers", OFFICE),
    ("POST", "/admin/transfers/99999/ship", OFFICE),
    ("POST", "/admin/transfers/99999/receive", OFFICE),
    ("POST", "/admin/transfers/99999/cancel", OFFICE),
    # --- Workshop+: checkouts (item-level) ---
    ("GET", "/admin/items/99999/checkout", WORKSHOP),
    ("POST", "/admin/items/99999/checkout", WORKSHOP),
    ("POST", "/admin/items/checkouts/99999/return", WORKSHOP),
    # --- Workshop+: scan ---
    ("GET", "/scan", WORKSHOP),
    ("POST", "/scan/resolve", WORKSHOP),
    ("GET", "/scan/item/99999", WORKSHOP),
]


# Routes that exist in the app but are *not* RBAC-gated. The boundary check
# subtracts these from the live-route count when comparing to ROUTES.
NON_RBAC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # Google OAuth flow — no role gate (auth bootstrap).
        ("GET", "/auth/google/login"),
        ("GET", "/auth/google/callback"),
        # Dev-only sign-in backdoor — bypasses CSRF + has no role check.
        ("POST", "/auth/_dev-login"),
        # Logout — no role gate; just clears the session.
        ("POST", "/auth/logout"),
    }
)


# ---------------------------------------------------------------------------
# Fixture: seed one user per role
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_users(db_session: Session) -> dict[str, User]:
    """Create one user per role (pending + four active) for the sweep.

    All emails are deterministic so the dev-login flow can find the same user
    across the two-step CSRF-bootstrap-then-login sequence.
    """
    users = {
        ROLE_PENDING: User(
            google_sub="rbac-pending",
            email="rbac-pending@x.test",
            name="Pending",
            role=None,
            status=UserStatus.PENDING,
        ),
        ROLE_WORKSHOP: User(
            google_sub="rbac-workshop",
            email="rbac-workshop@x.test",
            name="Workshop",
            role=Role.WORKSHOP,
            status=UserStatus.ACTIVE,
        ),
        ROLE_OFFICE: User(
            google_sub="rbac-office",
            email="rbac-office@x.test",
            name="Office",
            role=Role.OFFICE,
            status=UserStatus.ACTIVE,
        ),
        ROLE_MANAGER: User(
            google_sub="rbac-manager",
            email="rbac-manager@x.test",
            name="Manager",
            role=Role.MANAGER,
            status=UserStatus.ACTIVE,
        ),
        ROLE_ADMIN: User(
            google_sub="rbac-admin",
            email="rbac-admin@x.test",
            name="Admin",
            role=Role.ADMIN,
            status=UserStatus.ACTIVE,
        ),
    }
    for u in users.values():
        db_session.add(u)
    db_session.commit()
    for u in users.values():
        db_session.refresh(u)
    return users


def _bootstrap_csrf(client: TestClient) -> str:
    """GET / so the CSRF middleware sets the cookie, then return the token."""
    if "csrftoken" not in client.cookies:
        client.get("/")
    return client.cookies["csrftoken"]


def _login_as(client: TestClient, user: User) -> None:
    """Sign in as ``user`` via the dev-login backdoor."""
    resp = client.post(
        "/auth/_dev-login",
        data={"email": user.email, "sub": user.google_sub, "name": user.name},
        follow_redirects=False,
    )
    assert resp.status_code == 303, f"_dev-login for {user.email} returned {resp.status_code}"


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "gate"), ROUTES)
@pytest.mark.parametrize("role", ALL_ROLES)
def test_rbac_gate(
    method: str,
    path: str,
    gate: str,
    role: str,
    client: TestClient,
    seeded_users: dict[str, User],
) -> None:
    """Assert the role gate fires correctly for one (route, role) cell.

    Blocked roles must get exactly 401 (anon) or 403 (any signed-in user
    without sufficient privilege). Allowed roles must get any status code
    that is NOT 401 and NOT 403 — what we are pinning is "the gate let me
    through", not the happy path.
    """
    csrf = _bootstrap_csrf(client)

    if role != ROLE_ANON:
        _login_as(client, seeded_users[role])

    if method == "GET":
        response = client.get(path, follow_redirects=False)
    elif method == "POST":
        response = client.post(
            path,
            data={"csrf_token": csrf},
            follow_redirects=False,
        )
    else:
        raise NotImplementedError(f"unexpected method {method!r}")

    allowed = role in ALLOWED[gate]
    if allowed:
        assert response.status_code not in (401, 403), (
            f"{method} {path} as {role} should be allowed by gate={gate} "
            f"but got {response.status_code}: {response.text[:200]!r}"
        )
    elif role == ROLE_ANON:
        assert response.status_code == 401, (
            f"{method} {path} as anon should be 401 (not signed in) "
            f"but got {response.status_code}: {response.text[:200]!r}"
        )
    else:
        assert response.status_code == 403, (
            f"{method} {path} as {role} should be 403 (insufficient privilege) "
            f"but got {response.status_code}: {response.text[:200]!r}"
        )


# ---------------------------------------------------------------------------
# Boundary check
# ---------------------------------------------------------------------------


# Normalises both `{item_id}` (live-route shape) and `99999` (table shape) to a
# single `*` marker so the boundary check can compare like with like.
_PATH_PARAM_RE = re.compile(r"\{[^}]+\}|99999")


def _normalise_path(path: str) -> str:
    return _PATH_PARAM_RE.sub("*", path)


def _live_rbac_routes() -> set[tuple[str, str]]:
    """Every (method, normalised-path) in the live app, minus the auth-bootstrap set."""
    seen: set[tuple[str, str]] = set()
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        for method in r.methods:
            if method == "HEAD":
                continue
            seen.add((method, r.path))
    rbac_only = seen - NON_RBAC_ROUTES
    return {(m, _normalise_path(p)) for m, p in rbac_only}


def test_rbac_table_covers_all_live_routes() -> None:
    """The route table must cover every gate-eligible route in the live app.

    Forces every new route to declare a role gate (and add an entry here):
    if a future PR ships a new route without updating this table, the sweep
    fails with a clear message naming the missed (method, path) pair.
    """
    live = _live_rbac_routes()
    table = {(method, _normalise_path(path)) for method, path, _ in ROUTES}

    missing = live - table
    extra = table - live

    assert not missing, (
        f"ROUTES table is missing {len(missing)} live RBAC route(s); add an "
        f"entry per cell with the correct gate level. Missing: {sorted(missing)}"
    )
    assert not extra, (
        f"ROUTES table has {len(extra)} entries that don't match a live route; "
        f"check for a typo or a removed endpoint. Extra: {sorted(extra)}"
    )


def test_rbac_table_has_no_duplicate_entries() -> None:
    """Every (method, path) appears at most once in ROUTES."""
    seen: set[tuple[str, str]] = set()
    duplicates: list[tuple[str, str]] = []
    for method, path, _ in ROUTES:
        key = (method, path)
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    assert not duplicates, f"duplicate ROUTES entries: {duplicates}"


def test_rbac_table_uses_known_gates() -> None:
    """Every gate value in ROUTES is one we know how to check."""
    known = set(ALLOWED.keys())
    unknown = {gate for _, _, gate in ROUTES if gate not in known}
    assert not unknown, f"unknown gate values in ROUTES: {unknown}"
