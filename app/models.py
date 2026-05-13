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
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db import Base


def _derive_sku_prefix(name: str | None) -> str:
    """Derive a default ``sku_prefix`` from ``name`` for a single row.

    Mirrors the migration 0016 backfill rule for the candidate prefix step
    (without sibling disambiguation — that happens in the migration or at
    write time in the application layer):

    1. Try the first three alphabetic chars of ``name``, uppercased.
    2. If empty, fall back to the first three alphanumeric chars uppercased.
    3. If still empty (or ``name`` is ``None``/empty), use ``"CAT"``.
    4. Truncate to 8 chars.

    The result is always 1-8 uppercase alphanumeric chars, the same shape
    enforced by the ``sku_prefix`` validator.
    """

    raw = name or ""
    alpha = "".join(ch for ch in raw if ch.isalpha())[:3]
    if alpha:
        candidate = alpha.upper()
    else:
        alnum = "".join(ch for ch in raw if ch.isalnum())[:3]
        candidate = alnum.upper() if alnum else "CAT"
    return candidate[:8]


class Role(enum.StrEnum):
    """Role assigned by an admin once a user is approved.

    A pending user has ``user.role is None`` until an admin assigns one.
    """

    ADMIN = "admin"
    MANAGER = "manager"
    OFFICE = "office"
    WORKSHOP = "workshop"


class Archetype(enum.StrEnum):
    """Per-top-level category behaviour flag (taxonomy refinement).

    Stored only on depth-0 ``TaxonomyNode`` rows; depth-1 + depth-2 rows
    leave ``archetype IS NULL`` and inherit the value from their root by
    walking the ``parent_id`` chain at read time.

    - ``unique`` — one-of-a-kind items; tracking_mode forced to ``unique``.
    - ``bulk``   — quantity-tracked items; tracking_mode forced to ``qty``.
    - ``unique_variant`` — design family with auto-numbered pieces; each
      item lives on its own auto-created depth-2 leaf below a user-picked
      depth-1 sub-category. See ``docs/taxonomy-refinement-plan.md``.
    """

    UNIQUE = "unique"
    BULK = "bulk"
    UNIQUE_VARIANT = "unique_variant"


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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        # Sibling-scoped uniqueness on sku_prefix. Same shape as the
        # name-uniqueness pair: one partial index for the top-level
        # (parent_id IS NULL), one for the children (parent_id IS NOT NULL).
        # Scoped across active *and* archived rows to keep SKUs stable across
        # the archive lifecycle.
        Index(
            "uq_taxonomy_sku_prefix_top",
            "sku_prefix",
            unique=True,
            sqlite_where=text("parent_id IS NULL"),
            postgresql_where=text("parent_id IS NULL"),
        ),
        Index(
            "uq_taxonomy_sku_prefix_child",
            "parent_id",
            "sku_prefix",
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
    # Per-top-level archetype flag (see ``Archetype``). NULL at depth 1+2;
    # the application code resolves ``effective_archetype`` by walking up
    # to depth 0 at read time. SQL CHECK to enforce "NULL ↔ not root" is
    # deliberately left to the route layer for cross-dialect simplicity.
    archetype: Mapped[Archetype | None] = mapped_column(
        SAEnum(
            Archetype,
            name="taxonomy_archetype",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    # SKU prefix segment for this node. Composed with ancestor prefixes to
    # produce an item's SKU (e.g. ``RAW-SIL-925-0008``). Required after the
    # 0016 backfill; uppercased + 1-8 alphanumeric chars (validator below).
    # The Python-side ``default`` derives a sensible value from ``name`` so
    # legacy call sites and demo fixtures that pass ``TaxonomyNode(name=...)``
    # without ``sku_prefix`` keep working; explicit callers can override.
    sku_prefix: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=lambda context: _derive_sku_prefix(
            context.get_current_parameters().get("name") if context is not None else None
        ),
    )
    # Per-leaf SKU sequence allocator (used by the SKU helper to mint the
    # next ``<PREFIX>-<NNNN>`` suffix). Always defaults to 1 on insert;
    # incremented atomically by the allocator at item-create time.
    next_sequence: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Per-category defaults for the items create form. Validated dict (or
    # None). Keys mirror the items form's field names: ``unit``,
    # ``tracking_mode``, ``requires_checkout``, ``reorder_threshold``,
    # ``reorder_qty``, ``supplier_id``, ``location_id``. Absent / null keys
    # mean "no default" — the form input renders blank. See
    # ``app.taxonomy._coerce_defaults`` for the write-time validator.
    defaults_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    @validates("sku_prefix")
    def _validate_sku_prefix(self, _key: str, value: str | None) -> str:
        """Normalise + validate ``sku_prefix`` on assignment.

        Enforces: 1-8 alphanumeric chars, stored uppercased. Raises
        ``ValueError`` on an empty / non-alnum / too-long input so the route
        layer surfaces a clear 400 instead of a deferred constraint error.
        """

        if value is None:
            raise ValueError("sku_prefix is required")
        stripped = value.strip()
        if not stripped:
            raise ValueError("sku_prefix must not be blank")
        if not stripped.isalnum():
            raise ValueError("sku_prefix must contain only alphanumeric characters")
        if len(stripped) > 8:
            raise ValueError("sku_prefix must be 8 characters or fewer")
        return stripped.upper()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<TaxonomyNode id={self.id} name={self.name!r} "
            f"parent_id={self.parent_id} archetype={self.archetype} "
            f"sku_prefix={self.sku_prefix!r} "
            f"archived={self.archived_at is not None}>"
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
        Index("ix_taxonomy_field_defs_catalog_key", "catalog_key"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    # References ``app.field_catalog.FIELD_CATALOG[*].key``. Nullable for the
    # backfill window introduced in migration 0021; tightened to NOT NULL in
    # 0023 once 0022 has matched every row.
    catalog_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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


class TaxonomyStage(Base):
    """An ordered lifecycle stage owned by a top-level taxonomy node.

    Each top-level category defines its own stage list (e.g. RINGS:
    ``raw → polishing → QC → ready-to-ship → shipped``; RAW MATERIALS:
    ``on-hand → issued``). Items in that category carry a
    ``current_stage_id`` and transitions are recorded as ``STAGE_CHANGE``
    movements on ``stock_movements``.

    ``top_level_node_id`` must reference a row whose ``parent_id IS NULL``.
    The constraint is enforced in the route layer rather than the DB so the
    error surface stays unified with the rest of the taxonomy validation.

    Uniqueness: ``(top_level_node_id, name)`` is unique across active *and*
    archived rows (matches the ``TaxonomyNode`` convention — archiving must
    not free the name). At most one row per top-level node may have
    ``is_initial = TRUE`` while not archived (partial unique index).
    """

    __tablename__ = "taxonomy_stages"
    __table_args__ = (
        Index(
            "uq_taxonomy_stage_name",
            "top_level_node_id",
            "name",
            unique=True,
        ),
        Index(
            "uq_taxonomy_stage_initial_active",
            "top_level_node_id",
            unique=True,
            sqlite_where=text("is_initial = 1 AND archived_at IS NULL"),
            postgresql_where=text("is_initial AND archived_at IS NULL"),
        ),
        Index("ix_taxonomy_stages_top_level_node_id", "top_level_node_id"),
        Index("ix_taxonomy_stages_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    top_level_node_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    is_initial: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
            f"<TaxonomyStage id={self.id} top_level_node_id={self.top_level_node_id} "
            f"name={self.name!r} sort_order={self.sort_order} "
            f"is_initial={self.is_initial} archived={self.archived_at is not None}>"
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
        Index("ix_items_current_stage_id", "current_stage_id"),
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
    # Lifecycle stage owned by the item's top-level taxonomy category. NULL
    # is legitimate for items whose category has no stages defined. Stage
    # transitions are recorded as ``STAGE_CHANGE`` movements; the column
    # itself reflects only the *current* stage.
    current_stage_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("taxonomy_stages.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Integer rendered as the final segment of this item's SKU at create
    # time (e.g. SKU ``RAW-SIL-0008`` stores ``assigned_sequence=8``). Set
    # on every item create going forward; NULL on rows that pre-date the
    # 0016 migration. Round-trips the relationship to the leaf's
    # ``next_sequence`` without re-parsing the SKU string.
    assigned_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    value_decimal: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
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
            f"<ItemFieldValue id={self.id} item_id={self.item_id} field_def_id={self.field_def_id}>"
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
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
    ``stage_change`` — lifecycle-stage transition for the item (e.g. ring goes
                     from ``raw`` to ``ready-to-ship``). ``qty`` is written as
                     ``0`` because no quantity moves; the row exists to keep
                     the audit-visible history aligned with every other stock
                     event. Cost engine is never invoked.
    """

    IN = "in"
    OUT = "out"
    ADJUSTMENT = "adjustment"
    TRANSFER = "transfer"
    STAGE_CHANGE = "stage_change"


class StockMovement(Base):
    """A single mutation of an item's stock (M1+).

    Append-only by mission (§3 "Cost layer history is part of the audit trail
    and cannot be edited. Corrections are made via new movements"). The route
    layer creates a row for every recorded action; the cost engine
    (``app/cost_engine.py``) reads the row's id when stitching consumptions
    onto an out / negative-adjustment movement, and writes ``total_cost`` once
    the engine finishes.

    ``po_id`` carries the FK to ``purchase_orders.id`` (added in migration
    0012 by PO5 — the receive path is what activates the link).
    ``stock_take_id`` carries the FK to ``stock_takes.id`` (added in migration
    0014 by ST1 — the receive path will activate the link in ST3 when stock
    takes start writing adjustment movements).
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
    po_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("purchase_orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    stock_take_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("stock_takes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Set on the two TRANSFER movements (ship + receive) emitted by a
    # Transfer Order document under ``/admin/transfers/...``. Distinguishes
    # them from instant-flip transfers written by the legacy
    # ``/admin/items/{id}/transfer`` route, which leave this NULL.
    transfer_order_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("transfer_orders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # ``STAGE_CHANGE`` movements populate both stage FKs (``from_stage_id``
    # may be NULL on the very first transition for an item with no prior
    # stage). Every other movement type leaves both NULL.
    from_stage_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("taxonomy_stages.id", ondelete="RESTRICT"),
        nullable=True,
    )
    to_stage_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("taxonomy_stages.id", ondelete="RESTRICT"),
        nullable=True,
    )
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
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
    unit_cost_at_consumption: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<CostLayerConsumption id={self.id} layer_id={self.layer_id} "
            f"movement_id={self.movement_id} qty_consumed={self.qty_consumed}>"
        )


class Checkout(Base):
    """A single tool / mould checkout record (C-series).

    A row is open when ``returned_at IS NULL`` and closed (returned) once it
    has a non-null ``returned_at``. There is no status enum: the open/returned
    state is derived from a single nullable column. Overdue is
    ``returned_at IS NULL AND expected_return < now()``.

    ``user_id`` is the assignee — the workshop user who has the item. It is
    FK SET NULL so a user soft-delete (UserStatus.DISABLED is the v1 path;
    hard delete is rare) doesn't cascade through historical checkouts.
    ``item_id`` is RESTRICT — an item with checkout history cannot be
    hard-deleted (soft-archive is the v1 path anyway). ``item_unit_id`` is
    nullable: qty-tracked items check out the item-as-a-whole, while
    unique-tracked items (I3) point at a specific physical unit.

    No ``archived_at``: a checkout row is part of the audit trail. Corrections
    are made by adding a new return record with a condition note explaining
    the correction.

    The route layer (C2 onward) enforces:
    - Only items with ``requires_checkout=True`` may have checkout rows.
    - At most one open checkout per item / item_unit at a time.
    - For unique-tracked items, ``item_unit_id`` is required; for qty-tracked
      items, it must be NULL.
    """

    __tablename__ = "checkouts"
    __table_args__ = (
        Index("ix_checkouts_item_id", "item_id"),
        Index("ix_checkouts_item_unit_id", "item_unit_id"),
        Index("ix_checkouts_user_id", "user_id"),
        Index("ix_checkouts_returned_at", "returned_at"),
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
    user_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    checked_out_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expected_return: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    returned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    condition_note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
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
            f"<Checkout id={self.id} item_id={self.item_id} "
            f"user_id={self.user_id} returned={self.returned_at is not None}>"
        )


class POStatus(enum.StrEnum):
    """Lifecycle of a purchase order (PO2+).

    ``draft``              — created from the reorder dashboard; editable.
    ``sent``               — emailed to the supplier (PO4); locked from edits.
    ``in_transit``         — supplier confirmed dispatch (Slice 3 of the
                             in-transit scope addition). Manager marks this
                             after the supplier confirms shipment. The receive
                             route accepts both ``sent`` and ``in_transit`` so
                             marking-as-shipped is optional.
    ``partially_received`` — at least one line received but more is expected.
    ``received``           — every line fully received.
    ``cancelled``          — abandoned without receiving.

    PO2 only writes ``draft``. The other values exist on the enum so PO3 (PDF),
    PO4 (send), and PO5 (receive) don't need to alter the column.
    """

    DRAFT = "draft"
    SENT = "sent"
    IN_TRANSIT = "in_transit"
    PARTIALLY_RECEIVED = "partially_received"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class PurchaseOrder(Base):
    """A purchase order grouped by supplier (PO2+).

    Created in ``draft`` from the reorder dashboard. The supplier is FK-required
    (every PO is *to* one supplier). ``created_by`` is FK SET NULL: a user
    deletion (rare; the system uses soft-deletes) shouldn't cascade through PO
    history. ``expected_date`` and ``notes`` are user-editable in PO2b's edit
    form; in PO2 they're stored but not yet writable from the UI (so they're
    NULL on every PO created in this slice).

    Status transitions in v1 (out of PO2 scope but documented for context):
    - draft → sent (PO4 send-by-email path)
    - sent → partially_received | received (PO5 receive path)
    - partially_received → received
    - draft | sent → cancelled
    """

    __tablename__ = "purchase_orders"
    __table_args__ = (
        Index("ix_purchase_orders_supplier_id", "supplier_id"),
        Index("ix_purchase_orders_status", "status"),
        Index("ix_purchase_orders_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    supplier_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[POStatus] = mapped_column(
        SAEnum(
            POStatus,
            name="purchase_order_status",
            native_enum=False,
            length=20,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=POStatus.DRAFT,
        server_default=POStatus.DRAFT.value,
    )
    expected_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when the supplier confirms dispatch (Slice 3 of in-transit scope
    # addition). Optional — receive can fire from ``sent`` or ``in_transit``.
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
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
        return f"<PurchaseOrder id={self.id} supplier_id={self.supplier_id} status={self.status}>"


class PurchaseOrderLine(Base):
    """A single line item on a purchase order (PO2+).

    ``qty_ordered`` is the user's intent at draft time (or auto-derived from
    the item's ``reorder_qty`` / deficit when the PO is drafted from the
    reorder dashboard). ``qty_received`` is incremented on PO5 receive (full
    or partial); ``expected_unit_cost`` is the planning estimate, NULL when
    no prior cost layer was available to copy from. The **actual** unit cost
    of received stock is recorded on the cost layer at receipt (MISSION §3),
    not on this row — keeping expected vs. actual cleanly split.

    No ``archived_at`` — lines are part of the PO and don't soft-delete
    independently. Cancelling a PO sets its status to ``cancelled``; the lines
    stay attached.
    """

    __tablename__ = "purchase_order_lines"
    __table_args__ = (
        Index("ix_purchase_order_lines_po_id", "po_id"),
        Index("ix_purchase_order_lines_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("purchase_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty_ordered: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    qty_received: Mapped[Decimal] = mapped_column(
        Numeric(14, 4),
        nullable=False,
        default=Decimal("0"),
        server_default=text("0"),
    )
    expected_unit_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<PurchaseOrderLine id={self.id} po_id={self.po_id} "
            f"item_id={self.item_id} qty_ordered={self.qty_ordered}>"
        )


class StockTake(Base):
    """A scheduled stock-take session (ST1+).

    Lifecycle is **derived from timestamps** (no status enum, matching MISSION
    §6 which lists only the timestamps):

    - ``scheduled``    — both ``started_at`` and ``completed_at`` are NULL.
    - ``in_progress``  — ``started_at`` is set, ``completed_at`` is NULL.
    - ``completed``    — ``completed_at`` is set.

    Scope is mutually exclusive across ``scope_node_id`` and
    ``scope_location_id`` — at most one may be set; both null = "all items".
    The XOR invariant is enforced in the route layer (same posture as taxonomy
    leaf invariants and item-units qty-vs-unique rule); a DB-level CHECK
    constraint is a future tightening pass.

    ``created_by`` is FK SET NULL (a user soft-delete shouldn't cascade through
    history). ``scope_node_id`` and ``scope_location_id`` are FK RESTRICT —
    once a stock take has been scheduled against a category / location, that
    parent cannot be hard-deleted (soft-archive is the v1 path). No
    ``archived_at``: a stock take row is part of the audit trail; corrections
    are made via new movements (ST3+).

    ST1 only writes the ``scheduled`` state. ST2 will add the start /
    in-progress / counting flow; ST3 will add the commit-variances-as-
    adjustments flow that flips the row to ``completed``.
    """

    __tablename__ = "stock_takes"
    __table_args__ = (
        Index("ix_stock_takes_scope_node_id", "scope_node_id"),
        Index("ix_stock_takes_scope_location_id", "scope_location_id"),
        Index("ix_stock_takes_scheduled_for", "scheduled_for"),
        Index("ix_stock_takes_completed_at", "completed_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    scope_node_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scope_location_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    scheduled_for: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
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
            f"<StockTake id={self.id} scheduled_for={self.scheduled_for} "
            f"node={self.scope_node_id} location={self.scope_location_id} "
            f"started={self.started_at is not None} "
            f"completed={self.completed_at is not None}>"
        )


class StockTakeLine(Base):
    """One item-line on a stock-take session (ST2+).

    Created when the stock-take starts (ST2 captures the current
    ``item.current_qty`` into ``system_qty`` so a later edit to current_qty
    doesn't shift the variance). ``counted_qty`` is populated as the operator
    works through the count; ``variance = counted_qty - system_qty`` is
    populated on commit. ``committed`` flips True once ST3 has written the
    paired adjustment movement.

    ST1 doesn't write any rows here; the table exists so ST2 can land
    without a follow-up migration.
    """

    __tablename__ = "stock_take_lines"
    __table_args__ = (
        Index("ix_stock_take_lines_stock_take_id", "stock_take_id"),
        Index("ix_stock_take_lines_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stock_take_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stock_takes.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    system_qty: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    counted_qty: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    variance: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    committed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<StockTakeLine id={self.id} stock_take_id={self.stock_take_id} "
            f"item_id={self.item_id} system_qty={self.system_qty} "
            f"counted_qty={self.counted_qty} committed={self.committed}>"
        )


class TransferOrderStatus(enum.StrEnum):
    """Lifecycle of an internal Transfer Order (Slice 2 of the scope addition).

    ``draft``    — created with source/destination + lines; editable.
    ``shipped``  — items have left source; each line's item has
                   ``location_id = NULL`` (in transit); not yet received.
    ``received`` — items arrived; each line's item has
                   ``location_id = destination_location_id``.
    ``cancelled`` — abandoned while still ``draft`` (no movements ever written).

    Full-receipt-only model in v1: shipped goods are received in full when the
    courier delivers. Discrepancies after the fact go through the existing
    adjustment movement path.
    """

    DRAFT = "draft"
    SHIPPED = "shipped"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class TransferOrder(Base):
    """A document describing stock moving between two UC locations.

    Two-event flow: ship at source decrements visibility (``item.location_id``
    becomes NULL while in transit), receive at destination sets
    ``item.location_id = destination_location_id``. Cost engine is not invoked
    — transfers don't change ownership or cost basis, only physical location.

    The existing instant-flip ``TRANSFER`` movement under ``/admin/items/{id}
    /transfer`` is preserved as a "quick relocate" path. Use the TO flow when
    in-transit visibility matters (e.g. courier shipments between sites).
    """

    __tablename__ = "transfer_orders"
    __table_args__ = (
        Index("ix_transfer_orders_source_location_id", "source_location_id"),
        Index("ix_transfer_orders_destination_location_id", "destination_location_id"),
        Index("ix_transfer_orders_status", "status"),
        Index("ix_transfer_orders_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_location_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    destination_location_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[TransferOrderStatus] = mapped_column(
        SAEnum(
            TransferOrderStatus,
            name="transfer_order_status",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=TransferOrderStatus.DRAFT,
    )
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expected_arrival: Mapped[date | None] = mapped_column(Date, nullable=True)
    carrier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tracking_number: Mapped[str | None] = mapped_column(String(128), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    shipped_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    received_by: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
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
            f"<TransferOrder id={self.id} status={self.status} "
            f"src={self.source_location_id} dst={self.destination_location_id}>"
        )


class TransferOrderLine(Base):
    """One item being transferred under a Transfer Order.

    ``qty`` is informational (transfers are whole-item location flips in v1;
    a future per-location-qty refactor would make this load-bearing).
    ``ship_movement_id`` and ``receive_movement_id`` are populated when the
    parent transitions to ``shipped`` and ``received`` respectively.
    """

    __tablename__ = "transfer_order_lines"
    __table_args__ = (
        Index(
            "uq_transfer_order_line_item",
            "transfer_order_id",
            "item_id",
            unique=True,
        ),
        Index("ix_transfer_order_lines_item_id", "item_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    transfer_order_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("transfer_orders.id", ondelete="CASCADE"),
        nullable=False,
    )
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    ship_movement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("stock_movements.id", ondelete="RESTRICT"),
        nullable=True,
    )
    receive_movement_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("stock_movements.id", ondelete="RESTRICT"),
        nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<TransferOrderLine id={self.id} to_id={self.transfer_order_id} "
            f"item_id={self.item_id} qty={self.qty}>"
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
