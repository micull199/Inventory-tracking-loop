"""ORM models. Importing this module registers all tables on ``Base.metadata``.

Add new models here as they're introduced; ``migrations/env.py`` imports the
package so Alembic autogenerate can see every table.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import DateTime, String, func
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Role(enum.StrEnum):
    """Role assigned by an admin once a user is approved.

    A pending user has ``user.role is None`` until an admin assigns one.
    """

    ADMIN = "admin"
    MANAGER = "manager"
    OFFICE = "office"
    WORKSHOP = "workshop"


class UserStatus(enum.StrEnum):
    """Lifecycle of a user account.

    ``pending``  → created on first Google sign-in, awaiting admin approval.
    ``active``   → approved and able to use the app at their assigned role.
    ``disabled`` → revoked; cannot sign in or perform actions.
    """

    PENDING = "pending"
    ACTIVE = "active"
    DISABLED = "disabled"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    google_sub: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role | None] = mapped_column(
        SAEnum(
            Role,
            name="user_role",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(
            UserStatus,
            name="user_status",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=UserStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<User id={self.id} email={self.email!r} role={self.role} status={self.status}>"
