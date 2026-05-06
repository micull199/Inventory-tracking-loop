"""Append-only audit log: writer helper and DB-level immutability.

Every state-changing action in the app must call :func:`record_audit`. The
function flushes a row into ``audit_log`` within the caller's transaction —
the caller is still responsible for committing.

The ``audit_log`` table is also locked at the DB layer:
:func:`apply_immutability_triggers` installs dialect-specific triggers that
reject any UPDATE or DELETE. Application code never mutates audit rows; the
triggers are belt-and-braces so a misbehaving code path (or a future bug) can't
silently rewrite history.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from typing import Any

from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.models import AuditLog, User


def _to_jsonable(value: Any) -> Any:
    """Convert a value to something the JSON column will accept.

    Enums collapse to their ``.value`` (matches how we store them on the
    ``users`` table). Dates and datetimes serialise to ISO-8601 strings.
    Containers are walked recursively. Anything already JSON-safe passes through.
    """
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_to_jsonable(v) for v in value]
    # Last resort: stringify so the row still writes. Anything that lands here
    # is a bug in the caller — log a recognisable shape, don't crash the request.
    return repr(value)


def record_audit(
    db: Session,
    *,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: int | None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> AuditLog:
    """Write a single audit-log row in the caller's transaction.

    ``actor=None`` is reserved for system-issued events (background jobs, the
    bootstrap admin promotion). Every other call must pass the acting user.

    The function flushes the row so its ``id`` is available, but does not
    commit — that's the caller's job. This way an audit row and the change it
    records succeed-or-fail together.
    """
    entry = AuditLog(
        actor_id=actor.id if actor is not None else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        before_json=_to_jsonable(before) if before is not None else None,
        after_json=_to_jsonable(after) if after is not None else None,
    )
    db.add(entry)
    db.flush()
    return entry


# ---------------------------------------------------------------------------
# DB-level immutability
# ---------------------------------------------------------------------------
#
# The triggers below reject UPDATE and DELETE on ``audit_log``. They are the
# enforcement mechanism behind MISSION §9 ("Do not delete the audit log. Do not
# provide a way to edit it."). Single source of truth so the migration and any
# test fixture that needs the same behaviour stay in sync.

_SQLITE_TRIGGERS = (
    """
    CREATE TRIGGER audit_log_block_update
    BEFORE UPDATE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only: UPDATE forbidden');
    END;
    """,
    """
    CREATE TRIGGER audit_log_block_delete
    BEFORE DELETE ON audit_log
    BEGIN
        SELECT RAISE(ABORT, 'audit_log is append-only: DELETE forbidden');
    END;
    """,
)

_POSTGRES_BLOCK_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_log_block_modify() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;
"""

_POSTGRES_TRIGGERS = (
    """
    CREATE TRIGGER audit_log_block_update
    BEFORE UPDATE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_block_modify();
    """,
    """
    CREATE TRIGGER audit_log_block_delete
    BEFORE DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_block_modify();
    """,
)


def apply_immutability_triggers(connection: Connection) -> None:
    """Install the dialect-appropriate UPDATE/DELETE triggers on ``audit_log``."""
    from sqlalchemy import text

    dialect = connection.dialect.name
    if dialect == "sqlite":
        for stmt in _SQLITE_TRIGGERS:
            connection.execute(text(stmt))
    elif dialect == "postgresql":
        connection.execute(text(_POSTGRES_BLOCK_FUNCTION))
        for stmt in _POSTGRES_TRIGGERS:
            connection.execute(text(stmt))
    else:
        raise RuntimeError(
            f"audit_log immutability triggers not implemented for dialect {dialect!r}"
        )
