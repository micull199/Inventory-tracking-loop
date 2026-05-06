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


class ItemUnitStatus(enum.StrEnum):
    """Lifecycle state of one physical unit of a unique-tracked item (I3).

    ``available`` is the normal in-the-workshop state. ``lost`` flags a unit
    that can't be found / is presumed missing but hasn't been formally retired
    via archive — useful for inventory audits where the manager wants to keep
    the unit visible-but-flagged. The "checked out" state is *not* stored on
    the unit; it's derived from there being an open ``checkouts`` row pointing
    at the unit (C-series), so this enum stays small and orthogonal.
    """

    AVAILABLE = "available"
    LOST = "lost"


class ItemUnit(Base):
    """One physical unit of a unique-tracked item (I3).

    Only items with ``tracking_mode == UNIQUE`` may have unit rows; the route
    layer enforces that invariant. Each unit has its own serial/label, status,
    and (optionally) its own location — a single item can have units spread
    across multiple workshop locations.

    ``serial_or_label`` is unique *within an item* across active and archived
    rows (same archive-doesn't-free-the-name reasoning as Supplier names). Two
    different items can legitimately share a serial — labels are item-scoped.

    Soft-deletable; never hard-deleted. Per MISSION §6 the column ``status``
    lives on this table, not derived from ``checkouts``: that table doesn't
    exist yet (C-series), and a no-checkout-yet "lost" state is still useful.
    """

    __tablename__ = "item_units"
    __table_args__ = (
        Index(
            "uq_item_units_item_serial",
            "item_id",
            "serial_or_label",
            unique=True,
        ),
        Index("ix_item_units_item_id", "item_id"),
        Index("ix_item_units_location_id", "location_id"),
        Index("ix_item_units_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    serial_or_label: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[ItemUnitStatus] = mapped_column(
        SAEnum(
            ItemUnitStatus,
            name="item_unit_status",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=ItemUnitStatus.AVAILABLE,
    )
    location_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )
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
            f"<ItemUnit id={self.id} item_id={self.item_id} "
            f"serial_or_label={self.serial_or_label!r} status={self.status}>"
        )


class MovementType(enum.StrEnum):
    """Kind of stock movement (M1+).

    ``in``         — receipt of new stock (manual or via PO). Creates a cost
                     layer; bumps ``item.current_qty`` upward.
    ``out``        — consumed in production / scrapped / lost. Consumes cost
                     layers FIFO; reduces ``item.current_qty``.
    ``adjustment`` — stock-take correction. Positive adjustments behave like
                     ``in`` (create a layer); negative adjustments behave like
                     ``out`` (consume FIFO). The ``qty`` column is signed —
                     positive for "found extra", negative for "missing" — and
                     the engine routes to the receipt or consume path based on
                     sign.
    ``transfer``   — between locations. No cost change in v1; the route layer
                     records two movements (or a single movement plus a
                     location flip) without touching cost layers.
    """

    IN = "in"
    OUT = "out"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"


class StockMovement(Base):
    """A single mutation of an item's stock (M1+).

    Append-only by mission (§3 "Cost layer history is part of the audit trail
    and cannot be edited. Corrections are made via new movements"). The route
    layer creates a row for every recorded action; the cost engine
    (``app/cost_engine.py``) reads the row's id when stitching consumptions
    onto an out / negative-adjustment movement, and writes ``total_cost`` once
    the engine finishes.

    ``po_id`` and ``stock_take_id`` are plain integer columns (no FK
    constraint) in M1 because the ``purchase_orders`` and ``stock_takes``
    tables don't exist yet. The FK constraint is added in PO2 / ST1's
    migrations.
    """

    __tablename__ = "stock_movements"
    __table_args__ = (
        Index("ix_stock_movements_item_id", "item_id"),
        Index("ix_stock_movements_item_unit_id", "item_unit_id"),
        Index("ix_stock_movements_user_id", "user_id"),
        Index("ix_stock_movements_type", "type"),
        Index("ix_stock_movements_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    item_unit_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("item_units.id", ondelete="RESTRICT"),
        nullable=True,
    )
    type: Mapped[MovementType] = mapped_column(
        SAEnum(
            MovementType,
            name="stock_movement_type",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    po_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stock_take_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<StockMovement id={self.id} item_id={self.item_id} type={self.type} "
            f"qty={self.qty} total_cost={self.total_cost}>"
        )


class CostLayerSource(enum.StrEnum):
    """Where a cost layer came from (audit aid; FIFO doesn't branch on it).

    ``po_receipt``         — created by receiving against a purchase order.
    ``manual_in``          — manual stock-in (receipt outside a PO).
    ``positive_adjustment``— a stock-take found extra units; the operator
                             entered a unit cost.
    """

    PO_RECEIPT = "po_receipt"
    MANUAL_IN = "manual_in"
    POSITIVE_ADJUSTMENT = "positive_adjustment"


class CostLayer(Base):
    """A FIFO cost layer for an item (M1+).

    Created on every ``in`` / positive-adjustment movement at the unit cost
    entered at the moment of receipt. ``qty_remaining`` is decremented by
    consumptions (out / negative-adjustment movements); ``qty_received``,
    ``unit_cost``, and ``received_at`` are immutable once written. Once
    ``qty_remaining`` hits zero the layer stays in the table — it's part of
    the audit trail. Corrections are made via new movements.

    FIFO ordering is ``(received_at ASC, id ASC)``: a backdated receipt can
    land before existing layers, and ties are broken deterministically by id.
    The composite index ``ix_cost_layers_item_received`` covers that ORDER BY.
    """

    __tablename__ = "cost_layers"
    __table_args__ = (
        Index("ix_cost_layers_item_id", "item_id"),
        Index("ix_cost_layers_source_movement_id", "source_movement_id"),
        Index(
            "ix_cost_layers_item_received",
            "item_id",
            "received_at",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty_received: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    qty_remaining: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[CostLayerSource] = mapped_column(
        SAEnum(
            CostLayerSource,
            name="cost_layer_source",
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    source_movement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stock_movements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<CostLayer id={self.id} item_id={self.item_id} "
            f"qty_received={self.qty_received} qty_remaining={self.qty_remaining} "
            f"unit_cost={self.unit_cost} source={self.source}>"
        )


class CostLayerConsumption(Base):
    """One layer-tap by a single out / negative-adjustment movement.

    A movement that crosses N layers produces N consumption rows. Each row
    records the qty drawn from that layer and the unit cost at the moment of
    consumption (snapshot — the layer's ``unit_cost`` is itself immutable, so
    the snapshot will always equal it, but storing it explicitly makes the
    consumption row self-contained for reporting).
    """

    __tablename__ = "cost_layer_consumptions"
    __table_args__ = (
        Index("ix_cost_layer_consumptions_layer_id", "layer_id"),
        Index("ix_cost_layer_consumptions_movement_id", "movement_id"),
        Index(
            "ix_cost_layer_consumptions_movement_layer",
            "movement_id",
            "layer_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    layer_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("cost_layers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    movement_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stock_movements.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty_consumed: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    unit_cost_at_consumption: Mapped[Decimal] = mapped_column(
        Numeric(14, 4), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<CostLayerConsumption id={self.id} layer_id={self.layer_id} "
            f"movement_id={self.movement_id} qty_consumed={self.qty_consumed}>"
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
