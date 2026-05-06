"""ORM models. Importing this module registers all tables on ``Base.metadata``.

Add new models here as they're introduced; ``migrations/env.py`` imports the
package so Alembic autogenerate can see every table.
"""

from __future__ import annotations

import enum
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
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


class Supplier(Base):
    """A vendor UC buys stock from. Soft-deletable; never hard-deleted.

    The unique constraint on ``name`` covers archived rows too — archiving does
    not free the name, by design. To re-use a supplier name the operator must
    either rename the existing row or unarchive it. This keeps PO grouping
    unambiguous when a name is repeated across history.
    """

    __tablename__ = "suppliers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        return f"<Supplier id={self.id} name={self.name!r} archived={self.archived_at is not None}>"


class Location(Base):
    """A physical place stock can live (workshop bench, store room, safe…).

    Soft-deletable; never hard-deleted. The unique constraint on ``name``
    covers archived rows too — same reasoning as ``Supplier``: archiving must
    not free the name, because items reference a location by id and humans
    reference by name. Allowing two "Workshop Bench" rows (one archived, one
    active) would silently let stock be assigned to the wrong one.
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        return f"<Location id={self.id} name={self.name!r} archived={self.archived_at is not None}>"


class TaxonomyNode(Base):
    """A node in the (at most two-level) item taxonomy.

    Top-level rows have ``parent_id`` null and represent categories. Sub-categories
    (S4) point to a parent via ``parent_id``; the depth limit is enforced in the
    application layer rather than the DB. Soft-deletable; never hard-deleted.

    Uniqueness: per MISSION §3, a manager renames or archives a node; archiving
    must not free the name. Two partial unique indexes (added in migration 0005)
    cover both shapes:

    - ``uq_taxonomy_top_name`` — unique on ``(name)`` where ``parent_id IS NULL``
      (top-level siblings, S3).
    - ``uq_taxonomy_child_name`` — unique on ``(parent_id, name)`` where
      ``parent_id IS NOT NULL`` (children of the same parent, S4-ready).

    Both indexes scope across active *and* archived rows, matching the Supplier
    and Location convention.
    """

    __tablename__ = "taxonomy_nodes"
    __table_args__ = (
        Index(
            "uq_taxonomy_top_name",
            "name",
            unique=True,
            sqlite_where=text("parent_id IS NULL"),
            postgresql_where=text("parent_id IS NULL"),
        ),
        Index(
            "uq_taxonomy_child_name",
            "parent_id",
            "name",
            unique=True,
            sqlite_where=text("parent_id IS NOT NULL"),
            postgresql_where=text("parent_id IS NOT NULL"),
        ),
        Index("ix_taxonomy_nodes_parent_id", "parent_id"),
        Index("ix_taxonomy_nodes_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    parent_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        return (
            f"<TaxonomyNode id={self.id} name={self.name!r} "
            f"parent_id={self.parent_id} archived={self.archived_at is not None}>"
        )


class FieldType(enum.StrEnum):
    """Type of a custom field def attached to a taxonomy leaf node.

    Mirrors MISSION §3 / §6: ``select`` and ``multiselect`` carry a list of
    options in ``options_json``; the other types do not. Stored as a string
    column with ``values_callable`` so the DB sees the lowercase ``.value``
    rather than the Python member name.
    """

    TEXT = "text"
    NUMBER = "number"
    DECIMAL = "decimal"
    DATE = "date"
    BOOLEAN = "boolean"
    SELECT = "select"
    MULTISELECT = "multiselect"


class TaxonomyFieldDef(Base):
    """A custom field on a taxonomy *leaf* node.

    Items in this leaf inherit the field; required fields must be filled to
    save. Edits to the schema are non-retroactive (existing items keep their
    stored values; that's enforced when items land in I1+, not here).
    Soft-deletable; never hard-deleted. Archiving hides the field from new
    entry but keeps the row so historical item values remain meaningful.

    Uniqueness:
    - ``(node_id, name)`` and ``(node_id, key)`` are both unique across
      *active and archived* rows. Archiving must not free either, because
      ``item_field_values`` will reference the def by id, and likely by key for
      cross-version stability — re-using a name on a new def under the same
      node would silently overload the audit history.

    The "leaf" invariant is enforced in the application layer (the field-def
    routes), not in this model — a row whose ``node_id`` points at a top-level
    node *with active children* is still schema-valid in isolation.
    """

    __tablename__ = "taxonomy_field_defs"
    __table_args__ = (
        Index(
            "uq_taxonomy_field_defs_node_name",
            "node_id",
            "name",
            unique=True,
        ),
        Index(
            "uq_taxonomy_field_defs_node_key",
            "node_id",
            "key",
            unique=True,
        ),
        Index("ix_taxonomy_field_defs_node_id", "node_id"),
        Index("ix_taxonomy_field_defs_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[FieldType] = mapped_column(
        SAEnum(
            FieldType,
            name="taxonomy_field_type",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    options_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        return (
            f"<TaxonomyFieldDef id={self.id} node_id={self.node_id} "
            f"name={self.name!r} key={self.key!r} type={self.type} "
            f"required={self.required} archived={self.archived_at is not None}>"
        )


class TrackingMode(enum.StrEnum):
    """How an item's stock is counted.

    ``qty`` items are counted in bulk (e.g. a box of polishing compound, grams
    of silver wire). ``unique`` items are tracked one record per physical unit
    (e.g. a specific tool, a specific mould) — the per-unit rows live in the
    ``item_units`` table introduced in I3. For I1a there is no behavioural
    difference between the two modes; the column exists so the future ``qty``
    vs. ``unique`` split has a stable handle.
    """

    QTY = "qty"
    UNIQUE = "unique"


class Item(Base):
    """A thing UC tracks: a stockable material, consumable, tool, or mould.

    Each item belongs to exactly one *leaf* taxonomy node (top-level with no
    active children, or any sub-category). The leaf invariant is enforced in
    the application layer (see ``app/items.py``); the FK column allows any
    taxonomy node so the schema doesn't need to know about the leaf rule.

    ``current_qty`` is updated only by stock-movement processing (M1+). For
    I1a it is read-only on the form, defaulting to 0; the column is here so
    M1's first ``in`` movement has somewhere to land without a follow-up
    migration.

    Soft-deletable; never hard-deleted. ``sku`` is unique across active *and*
    archived rows by design — same reasoning as Supplier/Location names: SKUs
    appear on PO history and audit rows, and re-using one would silently let a
    user point at the wrong row. ``qr_code`` is partial-unique (only when set)
    so multiple items can share NULL while every printed label is one-to-one.
    """

    __tablename__ = "items"
    __table_args__ = (
        Index("uq_items_sku", "sku", unique=True),
        Index(
            "uq_items_qr_code",
            "qr_code",
            unique=True,
            sqlite_where=text("qr_code IS NOT NULL"),
            postgresql_where=text("qr_code IS NOT NULL"),
        ),
        Index("ix_items_taxonomy_node_id", "taxonomy_node_id"),
        Index("ix_items_supplier_id", "supplier_id"),
        Index("ix_items_location_id", "location_id"),
        Index("ix_items_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    taxonomy_node_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    unit: Mapped[str] = mapped_column(String(32), nullable=False)
    tracking_mode: Mapped[TrackingMode] = mapped_column(
        SAEnum(
            TrackingMode,
            name="item_tracking_mode",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=TrackingMode.QTY,
    )
    requires_checkout: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    current_qty: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    reorder_threshold: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    reorder_qty: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    supplier_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=True,
    )
    location_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    qr_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
        return (
            f"<Item id={self.id} sku={self.sku!r} name={self.name!r} "
            f"node_id={self.taxonomy_node_id} archived={self.archived_at is not None}>"
        )


class ItemFieldValue(Base):
    """Custom-field value bound to one (item, field def) pair (I2).

    Sparse storage: exactly one of the ``value_*`` columns is populated for any
    given row, chosen by the def's ``type``:

    - ``text``        → ``value_text``
    - ``number``      → ``value_number`` (integer)
    - ``decimal``     → ``value_decimal``
    - ``date``        → ``value_date``
    - ``boolean``     → ``value_bool``
    - ``select``      → ``value_text`` (the chosen option, as-is)
    - ``multiselect`` → ``value_json`` (list of chosen options)

    A row exists only when the item has a non-null/non-empty value for that
    field; clearing a value deletes the row. The ``(item_id, field_def_id)``
    unique index prevents the route layer from accidentally double-writing.

    The field def is referenced by id, not key. The S5 self-critique flagged
    that field renames re-derive the slug — a key change is recorded in the
    audit row but doesn't affect existing values stored here, because the link
    is by id. ``field_def.archived_at`` is intentionally not enforced at this
    layer: items keep their stored values when a def is archived (per MISSION
    §3 "Deleting a field hides it from new entry but preserves the value").
    """

    __tablename__ = "item_field_values"
    __table_args__ = (
        Index(
            "uq_item_field_values_item_field_def",
            "item_id",
            "field_def_id",
            unique=True,
        ),
        Index("ix_item_field_values_item_id", "item_id"),
        Index("ix_item_field_values_field_def_id", "field_def_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    field_def_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("taxonomy_field_defs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    value_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    value_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    value_decimal: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 4), nullable=True
    )
    value_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    value_bool: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    value_json: Mapped[Any | None] = mapped_column(JSON, nullable=True)
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
        return (
            f"<ItemFieldValue id={self.id} item_id={self.item_id} "
            f"field_def_id={self.field_def_id}>"
        )


class AuditLog(Base):
    """Append-only record of every state-changing action.

    Writes go through ``app.audit.record_audit``. Reads are unrestricted; the
    DB-level immutability triggers (see ``apply_immutability_triggers``) reject
    any UPDATE/DELETE so a corrupted application code path cannot rewrite history.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    after_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<AuditLog id={self.id} action={self.action!r} "
            f"entity={self.entity_type}:{self.entity_id} actor={self.actor_id}>"
        )
