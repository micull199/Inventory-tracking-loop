"""Audit-coverage sweep — one test per state-changing route.

DoD #8: "Every state change in audit log with actor + timestamp. Audit log
not editable."

Why a sweep
-----------
Per-route audit-row tests already exist (see e.g.
``test_suppliers_routes.py``'s create/edit/archive shape assertions). They
cover the audit shapes for each domain. This file is the consolidated
boundary test: every live mutation route in ``app.routes`` lands in the
parametrized sweep, so the suite fails immediately if a new POST/PUT/PATCH/
DELETE route ships without a ``record_audit`` call.

The boundary check at the bottom (``test_audit_exempt_set_contains_only_live_routes``)
asserts ``_EXEMPT_FROM_AUDIT_WRITE`` doesn't drift out of sync with the live
app — if a route is removed, its stale exempt entry surfaces here.

Why source-text inspection rather than runtime sweep
----------------------------------------------------
A runtime sweep would need a per-route ``(setup_func, payload)`` map to fire
each route with a happy-path body and watch ``audit_log.count(*)`` increment.
That is ~150-300 LoC of fixture wiring on top of the per-route happy-path
tests already in the suite. Source-text inspection
(``"record_audit(" in inspect.getsource(endpoint)``) gives the same
forcing-function guarantee for ~5% of the LoC: every future mutation route
that forgets to call ``record_audit`` fails the sweep on first PR. The
runtime evidence already exists in per-domain test files; the sweep is the
cross-cutting boundary, not a runtime re-proof.

The 3 exempt routes
-------------------
- ``POST /auth/logout`` — pops the session cookie; no DB state change.
- ``POST /auth/_dev-login`` — dev-only sign-in backdoor; delegates the audit
  write to ``upsert_user_from_userinfo`` (which writes ``user.created`` +
  optional ``user.bootstrap_admin_granted``). The route's source doesn't
  directly contain ``record_audit(``; the helper does.
- ``POST /scan/resolve`` — read-only QR/SKU lookup that 303-redirects to
  ``/scan/item/{id}``; no DB state change.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute

from app.main import app

# ---------------------------------------------------------------------------
# Exempt set — mutation routes that intentionally don't directly call
# ``record_audit`` in their own source body. Each entry has a one-line reason
# above it. Add to this set only with a justification the next reviewer can
# audit at a glance.
# ---------------------------------------------------------------------------

_EXEMPT_FROM_AUDIT_WRITE: frozenset[tuple[str, str]] = frozenset(
    {
        # Pops the session cookie; no DB state change.
        ("POST", "/auth/logout"),
        # Dev-only sign-in backdoor; audit write delegated to
        # ``upsert_user_from_userinfo``.
        ("POST", "/auth/_dev-login"),
        # Read-only QR/SKU lookup → 303 redirect; no DB state change.
        ("POST", "/scan/resolve"),
        # Post-0024: field-def unarchive is a deprecated no-op that 400s
        # unconditionally. Kept for URL stability; no state change to audit.
        ("POST", "/admin/taxonomy/fields/{field_id}/unarchive"),
    }
)


# ---------------------------------------------------------------------------
# Route enumeration
# ---------------------------------------------------------------------------

_MUTATION_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _enumerate_mutation_routes() -> list[tuple[str, str, object]]:
    """Walk ``app.routes`` and return every (method, path, endpoint) triple
    where method is a state-changing HTTP verb.

    A route registered under multiple methods (none in v1, but defensively)
    yields one entry per mutation method. Non-APIRoute entries (e.g. mounts)
    are skipped — they don't have a callable ``endpoint``.
    """
    triples: list[tuple[str, str, object]] = []
    for r in app.routes:
        if not isinstance(r, APIRoute):
            continue
        for method in r.methods:
            if method in _MUTATION_METHODS:
                triples.append((method, r.path, r.endpoint))
    return triples


_MUTATION_ROUTES: list[tuple[str, str, object]] = _enumerate_mutation_routes()


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "endpoint"),
    _MUTATION_ROUTES,
    ids=[f"{m} {p}" for m, p, _ in _MUTATION_ROUTES],
)
def test_mutation_route_writes_audit_directly(method: str, path: str, endpoint: object) -> None:
    """Every state-changing route must call ``record_audit`` in its body
    (or be in the exempt set with a documented reason).
    """
    if (method, path) in _EXEMPT_FROM_AUDIT_WRITE:
        pytest.skip(f"{method} {path} is in _EXEMPT_FROM_AUDIT_WRITE")

    src = inspect.getsource(endpoint)  # type: ignore[arg-type]
    assert "record_audit(" in src, (
        f"{method} {path} body has no record_audit() call. "
        f"Either add the call so the state change appears in the audit log, "
        f"or add the route to _EXEMPT_FROM_AUDIT_WRITE with a documented "
        f"reason (DoD #8 — every state change in audit log)."
    )


# ---------------------------------------------------------------------------
# Boundary checks
# ---------------------------------------------------------------------------


def test_audit_exempt_set_contains_only_live_routes() -> None:
    """The exempt set must not contain stale entries.

    If a future PR removes one of the exempt routes (e.g. retires
    ``/scan/resolve``), this check fails until the stale entry is removed
    from ``_EXEMPT_FROM_AUDIT_WRITE``.
    """
    live_pairs = {(m, p) for m, p, _ in _MUTATION_ROUTES}
    stale = _EXEMPT_FROM_AUDIT_WRITE - live_pairs
    assert not stale, (
        f"_EXEMPT_FROM_AUDIT_WRITE contains entries that no longer exist "
        f"as live mutation routes: {sorted(stale)}. Remove them."
    )


def test_audit_sweep_covers_at_least_45_routes() -> None:
    """Lower-bound sanity check: as of A2, the live app has 48 mutation
    routes (45 with direct ``record_audit`` + 3 exempt). A regression that
    drops the count below 45 (e.g. a router failing to register) would
    surface here as well as in per-route tests.
    """
    assert len(_MUTATION_ROUTES) >= 45, (
        f"expected >= 45 mutation routes; found {len(_MUTATION_ROUTES)}. "
        f"A router probably failed to register."
    )
