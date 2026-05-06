"""Unit tests for the role enum + ``require_role`` / ``require_active_user``
dependency factories."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.auth import require_active_user, require_role
from app.models import Role, User, UserStatus


def _user(role: Role | None, status: UserStatus = UserStatus.ACTIVE) -> User:
    return User(
        id=1,
        google_sub="g-1",
        email="u@example.com",
        name="U",
        role=role,
        status=status,
    )


class TestRoleEnum:
    def test_has_four_named_roles(self) -> None:
        assert {r.value for r in Role} == {"admin", "manager", "office", "workshop"}


class TestRequireRole:
    def test_allows_user_with_matching_role(self) -> None:
        dep = require_role(Role.MANAGER)
        result = dep(current_user=_user(Role.MANAGER))
        assert result.role is Role.MANAGER

    def test_admin_passes_any_required_role(self) -> None:
        """Admins implicitly satisfy any role requirement."""
        dep = require_role(Role.WORKSHOP)
        result = dep(current_user=_user(Role.ADMIN))
        assert result.role is Role.ADMIN

    def test_blocks_user_with_wrong_role(self) -> None:
        dep = require_role(Role.MANAGER)
        with pytest.raises(HTTPException) as exc:
            dep(current_user=_user(Role.WORKSHOP))
        assert exc.value.status_code == 403

    def test_blocks_pending_user_even_with_role(self) -> None:
        """Pending users do not get past role checks: no role is effective until active."""
        dep = require_role(Role.MANAGER)
        with pytest.raises(HTTPException) as exc:
            dep(current_user=_user(Role.MANAGER, status=UserStatus.PENDING))
        assert exc.value.status_code == 403

    def test_blocks_user_with_no_role_assigned(self) -> None:
        dep = require_role(Role.WORKSHOP)
        with pytest.raises(HTTPException) as exc:
            dep(current_user=_user(None))
        assert exc.value.status_code == 403

    def test_accepts_multiple_allowed_roles(self) -> None:
        dep = require_role(Role.MANAGER, Role.OFFICE)
        assert dep(current_user=_user(Role.OFFICE)).role is Role.OFFICE
        assert dep(current_user=_user(Role.MANAGER)).role is Role.MANAGER
        with pytest.raises(HTTPException):
            dep(current_user=_user(Role.WORKSHOP))


class TestRequireActiveUser:
    """``require_active_user`` blocks anyone whose status is not ACTIVE.

    A pending user with a valid session cookie can still hit role-agnostic
    endpoints (e.g. ``/auth/me``) without this guard. Disabling someone has
    to take immediate effect regardless of whether their cookie is still
    technically valid.
    """

    def test_passes_active_user(self) -> None:
        u = _user(Role.WORKSHOP, status=UserStatus.ACTIVE)
        assert require_active_user(current_user=u) is u

    def test_blocks_pending_user(self) -> None:
        u = _user(Role.WORKSHOP, status=UserStatus.PENDING)
        with pytest.raises(HTTPException) as exc:
            require_active_user(current_user=u)
        assert exc.value.status_code == 403

    def test_blocks_disabled_user(self) -> None:
        u = _user(Role.MANAGER, status=UserStatus.DISABLED)
        with pytest.raises(HTTPException) as exc:
            require_active_user(current_user=u)
        assert exc.value.status_code == 403

    def test_active_user_with_no_role_passes(self) -> None:
        # A roleless active user shouldn't actually exist (admin-set guard
        # forbids it) but the dependency itself only cares about status.
        u = _user(None, status=UserStatus.ACTIVE)
        assert require_active_user(current_user=u) is u
