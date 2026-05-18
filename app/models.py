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
    """Per-leaf "show this catalog field on items" pick.

    The schema of an item is fixed (every Item has every catalog-backed
    column). This table is a **visibility selector**: it lists which catalog
    keys a given taxonomy node opts into showing on the items form, list,
    and CSV. Picks inherit downward: a key picked on a top-level node is
    visible on every descendant.

    ``key`` must be a value in ``app.field_catalog.CATALOG_BY_KEY`` (enforced
    at write time by the picker route). ``required=True`` forces the field
    to be non-blank when items are created in this category.
    """

    __tablename__ = "taxonomy_field_defs"
    __table_args__ = (
        Index(
            "uq_taxonomy_field_defs_node_key",
            "node_id",
            "key",
            unique=True,
        ),
        Index("ix_taxonomy_field_defs_node_id", "node_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("taxonomy_nodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Catalog key — must match an entry in ``app.field_catalog.FIELD_CATALOG``.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
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
            f"key={self.key!r} required={self.required}>"
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


class StyleFamily(enum.StrEnum):
    """Top-level style classification for a ``Design`` (S3 / ADR-003).

    Drives downstream filtering and design-level reporting. The enum
    deliberately covers ring / band / earring / chain / pendant style
    families in one tuple — the spec keeps the design-IP layer flat so
    a single Emma design can be ``solitaire`` even if it's made as
    multiple physical configurations across categories.
    """

    SOLITAIRE = "solitaire"
    HALO = "halo"
    HIDDEN_HALO = "hidden_halo"
    THREE_STONE = "three_stone"
    TRILOGY = "trilogy"
    CLUSTER = "cluster"
    VINTAGE = "vintage"
    BEZEL = "bezel"
    TENSION = "tension"
    CATHEDRAL = "cathedral"
    PLAIN_BAND = "plain_band"
    ETERNITY = "eternity"
    HALF_ETERNITY = "half_eternity"
    PENDANT = "pendant"
    CHAIN = "chain"
    STUD = "stud"
    DROP = "drop"
    HOOP = "hoop"
    OTHER = "other"


class Design(Base):
    """Design master — separates design IP from production hierarchy.

    See ``docs/adr/003-designs-split-from-taxonomy.md`` for the full
    architectural decision record. In short: today's "Emma" is a depth-1
    ``TaxonomyNode`` doing triple duty as design IP, production grouping
    and SKU prefix; this row is the dedicated home for the design-IP
    concern.

    Designs are **shared across casting locations** — one Emma row
    regardless of whether the rings are spun in Australia or Thailand.
    Location lives at the item level (``Item.location_id``); designs
    are location-agnostic. A deliberately-different TH-only Emma is a
    new design row (e.g. ``Emma-Heavy``), not a Thailand-flavoured
    Emma — the naming forces the divergence to be visible in reporting.

    Soft-deletable; ``design_code`` is unique across active + archived
    rows (mirrors the ``Item.sku`` / ``Stone.stone_code`` convention).
    The ``items.design_id`` FK is **deliberately deferred** — see the
    ADR for the staged rollout. The model is populated and managed
    standalone for now; the items link lands when operator-entered
    design metadata is rich enough to backfill from.
    """

    __tablename__ = "designs"
    __table_args__ = (
        Index("uq_designs_design_code", "design_code", unique=True),
        Index("ix_designs_default_metal_id", "default_metal_id"),
        Index("ix_designs_archived_at", "archived_at"),
        Index("ix_designs_discontinued_date", "discontinued_date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Format ``DSG-NNNN``, system-allocated via ``app.designs.allocate_design_code``.
    design_code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    collection: Mapped[str | None] = mapped_column(String(64), nullable=True)
    style_family: Mapped[StyleFamily | None] = mapped_column(
        SAEnum(
            StyleFamily,
            name="style_family",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    designer: Mapped[str | None] = mapped_column(String(128), nullable=True)
    cad_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # ADR-003 additions: CAD versioning for the AU / TH multi-pull world.
    # ``cad_version`` is operator-set human identifier; ``cad_updated_at`` is
    # the machine-comparable freshness signal that lets the workshops detect
    # stale local pulls without depending on version-string discipline.
    cad_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    cad_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    default_metal_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("metal_master.id", ondelete="RESTRICT"),
        nullable=True,
    )
    intro_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    discontinued_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # Planning standard cost — spec §5 calls this out for future
    # standard-vs-actual variance reporting at the design level.
    # Manager-maintained; nullable so a fresh design can ship with no
    # cost estimate yet.
    standard_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
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
        return (
            f"<Design id={self.id} code={self.design_code!r} "
            f"name={self.name!r} style={self.style_family}>"
        )


class AlloyFamily(enum.StrEnum):
    """Top-level alloy family for a ``Metal`` (S2 of architectural additions).

    Drives downstream display + filtering: gold, platinum, palladium, silver
    behave differently for hallmark rules and spot-price feeds. ``other``
    catches base metals (steel, brass) used in fashion lines.
    """

    GOLD = "gold"
    PLATINUM = "platinum"
    PALLADIUM = "palladium"
    SILVER = "silver"
    OTHER = "other"


class MetalColour(enum.StrEnum):
    """Visible colour of a metal alloy.

    ``two_tone`` is for mixed-metal items where two distinct colours are used
    (e.g. an 18ct WG band with a YG accent). The pair of metals lives on
    ``Item.metal_id`` + ``Item.secondary_metal_id``; this colour is the
    aggregate label.
    """

    YELLOW = "yellow"
    WHITE = "white"
    ROSE = "rose"
    GREEN = "green"
    TWO_TONE = "two_tone"
    PLATINUM = "platinum"
    PALLADIUM = "palladium"
    SILVER = "silver"


class Metal(Base):
    """Administered lookup of metal alloys (S2 of architectural additions spec).

    Required for (a) precious-metal accounting (pure-gram reconciliation
    against the gold pool), (b) costing (gold price moves daily — see
    ``MetalSpotPrice``), (c) customer-facing display, (d) Thailand transfer-
    pricing declarations.

    Soft-deletable; ``metal_code`` is unique across active + archived (same
    archive-doesn't-free-the-code posture as ``Supplier.name``). The
    code-set is operator-extensible (no hardcoded enum) — managers add new
    alloys via the admin route in a future slice. Seed values are populated
    in migration 0030.
    """

    __tablename__ = "metal_master"
    __table_args__ = (
        Index("uq_metal_master_code", "metal_code", unique=True),
        Index("ix_metal_master_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # E.g. ``18KYG``, ``14KWG``, ``PLAT950``, ``PD950``, ``SS``, ``9KRG``.
    metal_code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    alloy_family: Mapped[AlloyFamily] = mapped_column(
        SAEnum(
            AlloyFamily,
            name="alloy_family",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    # 9/14/18/22/24 for gold; null for non-gold.
    karat: Mapped[int | None] = mapped_column(Integer, nullable=True)
    purity_pct: Mapped[Decimal] = mapped_column(Numeric(6, 3), nullable=False)
    colour: Mapped[MetalColour] = mapped_column(
        SAEnum(
            MetalColour,
            name="metal_colour",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    density_g_per_cc: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    hallmark_stamp: Mapped[str | None] = mapped_column(String(16), nullable=True)
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
            f"<Metal id={self.id} code={self.metal_code!r} "
            f"family={self.alloy_family} colour={self.colour}>"
        )


class MetalSpotPrice(Base):
    """Daily spot price per metal (S2 of architectural additions spec).

    Separate table because spot prices change daily and the row count grows
    append-heavy. v1: manual entry by manager via the admin price route
    (a future slice). v2 (post-$200M): pulled from a feed (LBMA, Kitco).

    Unique on ``(metal_id, as_of_date)`` so one price per metal per day —
    duplicate entries are operator error. Currency is implicit AUD for v1;
    a ``currency`` column is reserved for the multi-currency expansion later.
    """

    __tablename__ = "metal_spot_prices"
    __table_args__ = (
        Index(
            "uq_metal_spot_prices_date",
            "metal_id",
            "as_of_date",
            unique=True,
        ),
        Index("ix_metal_spot_prices_metal_id", "metal_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    metal_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("metal_master.id", ondelete="RESTRICT"),
        nullable=False,
    )
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    price_per_gram: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<MetalSpotPrice id={self.id} metal_id={self.metal_id} "
            f"as_of={self.as_of_date} price={self.price_per_gram}>"
        )


# ---------------------------------------------------------------------------
# S4 attribute-group enums (architectural additions spec §4)
#
# Side-table attribute groups attach to ``Item`` via a one-to-one PK FK
# (CASCADE on item delete). Each enum is non-native ``String(N)`` so values
# round-trip in their lowercase wire form across SQLite + Postgres without
# CREATE TYPE migrations.
# ---------------------------------------------------------------------------


class RingSizeStandard(enum.StrEnum):
    US = "us"
    AU_UK = "au_uk"
    EU = "eu"


class BandProfile(enum.StrEnum):
    COURT = "court"
    D_SHAPE = "d_shape"
    FLAT = "flat"
    FLAT_COURT = "flat_court"
    HALFROUND = "halfround"
    KNIFE_EDGE = "knife_edge"
    CATHEDRAL = "cathedral"
    EURO_SHANK = "euro_shank"


class MetalFinish(enum.StrEnum):
    POLISHED = "polished"
    MATTE = "matte"
    BRUSHED = "brushed"
    HAMMERED = "hammered"
    MILGRAIN = "milgrain"
    SANDBLAST = "sandblast"


class ShankStyle(enum.StrEnum):
    SOLID = "solid"
    SPLIT = "split"
    TWISTED = "twisted"
    PAVE_SET = "pave_set"
    PLAIN = "plain"


class SettingStyle(enum.StrEnum):
    SOLITAIRE = "solitaire"
    HALO = "halo"
    HIDDEN_HALO = "hidden_halo"
    THREE_STONE = "three_stone"
    TRILOGY = "trilogy"
    CLUSTER = "cluster"
    VINTAGE = "vintage"
    BEZEL = "bezel"
    TENSION = "tension"


class ProngStyle(enum.StrEnum):
    ROUND = "round"
    CLAW = "claw"
    V_TIP = "v_tip"
    DOUBLE_CLAW = "double_claw"


class GalleryStyle(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    FILIGREE = "filigree"


class BandSetStyle(enum.StrEnum):
    PLAIN = "plain"
    CHANNEL_SET = "channel_set"
    PAVE = "pave"
    ETERNITY = "eternity"
    HALF_ETERNITY = "half_eternity"
    MIXED_METAL = "mixed_metal"


class EarringSold(enum.StrEnum):
    PAIR = "pair"
    SINGLE = "single"


class EarringClosure(enum.StrEnum):
    BUTTERFLY = "butterfly"
    SCREW_BACK = "screw_back"
    LEVER_BACK = "lever_back"
    HOOK = "hook"
    FRENCH_WIRE = "french_wire"
    CLIP = "clip"
    HUGGIE = "huggie"


class EarringStyle(enum.StrEnum):
    STUD = "stud"
    DROP = "drop"
    HOOP = "hoop"
    CHANDELIER = "chandelier"
    HUGGIE = "huggie"
    THREADER = "threader"
    CLIMBER = "climber"


class ChainStyle(enum.StrEnum):
    CABLE = "cable"
    CURB = "curb"
    BOX = "box"
    ROPE = "rope"
    SNAKE = "snake"
    FIGARO = "figaro"
    BELCHER = "belcher"
    WHEAT = "wheat"
    SINGAPORE = "singapore"
    HERRINGBONE = "herringbone"


class ChainClosure(enum.StrEnum):
    LOBSTER = "lobster"
    SPRING_RING = "spring_ring"
    BOX = "box"
    TOGGLE = "toggle"
    S_HOOK = "s_hook"
    BARREL = "barrel"
    MAGNETIC = "magnetic"


class WornAs(enum.StrEnum):
    NECKLACE = "necklace"
    BRACELET = "bracelet"
    ANKLET = "anklet"


class BailType(enum.StrEnum):
    FIXED = "fixed"
    HINGED = "hinged"
    HIDDEN = "hidden"
    ENHANCER = "enhancer"


class EngravingStyle(enum.StrEnum):
    MACHINE = "machine"
    HAND = "hand"
    LASER = "laser"


class StoneType(enum.StrEnum):
    """High-level stone category.

    Drives the per-type validation envelope (e.g. colour grade D-Z is only
    meaningful for diamonds) and reporting. Stored as ``String(16)`` with
    ``values_callable`` so the DB sees the lowercase ``.value``.
    """

    DIAMOND = "diamond"
    LAB_DIAMOND = "lab_diamond"
    SAPPHIRE = "sapphire"
    RUBY = "ruby"
    EMERALD = "emerald"
    MOISSANITE = "moissanite"
    OTHER = "other"


class StoneLab(enum.StrEnum):
    """Grading laboratory that issued a stone's certificate."""

    GIA = "gia"
    IGI = "igi"
    HRD = "hrd"
    GCAL = "gcal"
    OTHER = "other"
    NONE = "none"


class StoneOrigin(enum.StrEnum):
    """How the stone came into being."""

    NATURAL = "natural"
    LAB_GROWN = "lab_grown"
    TREATED_NATURAL = "treated_natural"


class StoneOwnership(enum.StrEnum):
    """Who owns the stone while it lives in inventory.

    ``memo`` and ``consignment`` belong to the supplier until paid for; the
    inventory tracks them on hand but they don't count against owned cost.
    """

    OWNED = "owned"
    MEMO = "memo"
    CONSIGNMENT = "consignment"


class StoneStatus(enum.StrEnum):
    """Lifecycle state of a stone.

    Denormalised from the stone-events ledger — every transition writes a
    matching ``stone_event`` row in the same transaction. The route layer
    (future slice) enforces the legal transitions in the spec.
    """

    AVAILABLE = "available"
    RESERVED = "reserved"
    SET = "set"
    SOLD = "sold"
    RETURNED_TO_SUPPLIER = "returned_to_supplier"
    LOST = "lost"


class TrackingTrigger(enum.StrEnum):
    """Why a ``Stone`` is being tracked as an entity rather than left as melee.

    Spec §10.1 (locked 2026-05-18). The base rule from S1 was simply
    "has a cert → tracked"; this enum surfaces the full rule + the
    manual-override path:

    - ``cert``: the stone has a grading certificate (lab + cert_number).
    - ``coloured_stone_threshold``: a non-diamond stone whose carat
      weight clears the ``stones.tracking.coloured_stone_ct_threshold``
      app-setting (default 0.50 ct).
    - ``cost_threshold``: any stone whose acquisition_cost clears the
      ``stones.tracking.cost_floor_aud`` setting (default $500 AUD).
    - ``manual_override``: none of the auto-triggers fired but the
      operator explicitly wants to track this stone — accompanied by
      ``tracking_override_reason`` (required at the route layer).

    Auto-precedence on create: cert → coloured_stone → cost. Manual
    override only fires when no auto-trigger applies.
    """

    CERT = "cert"
    COLOURED_STONE_THRESHOLD = "coloured_stone_threshold"
    COST_THRESHOLD = "cost_threshold"
    MANUAL_OVERRIDE = "manual_override"


class StonePosition(enum.StrEnum):
    """Where on an item a stone sits.

    Distinguishes a centre stone from accents so an item-detail view can
    render the layout faithfully. ``position_index`` (on ``ItemStone``) is
    used to disambiguate multiple accents in the same position role.
    """

    CENTRE = "centre"
    ACCENT_LEFT = "accent_left"
    ACCENT_RIGHT = "accent_right"
    ACCENT = "accent"
    HALO = "halo"
    GALLERY = "gallery"
    OTHER = "other"


class Unit(Base):
    """Lookup of units of measure (S5 of architectural additions spec).

    Replaces freetext ``Item.unit`` for new entities. The legacy freetext
    column survives during migration per the S5 spec's posture — same as
    ``Item.stone_shape`` (deprecated by ``stone_shape_master`` in S1) and
    ``Item.ring_size`` (deprecated by ``item_ring_attrs.ring_size``). The
    backfill of existing freetext → ``unit_id`` is deferred until
    reporting cleanliness starts to bite.

    Soft-deletable; ``code`` is unique across active + archived rows.
    Same archive-doesn't-free-the-code convention as ``Supplier``,
    ``Location``, ``StoneShape``, ``Metal``, ``MetalMaster``.
    """

    __tablename__ = "unit_master"
    __table_args__ = (
        Index("uq_unit_master_code", "code", unique=True),
        Index("ix_unit_master_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Short wire code: ``ea``, ``g``, ``kg``, ``ct``, ``mm``, ``pair`` …
    code: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
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
        return f"<Unit id={self.id} code={self.code!r} name={self.name!r}>"


class ReasonCode(Base):
    """Movement-type-scoped reason vocabulary (S5 of architectural additions).

    Replaces the freetext ``StockMovement.reason`` for the cases an
    operator picks from a known list. The freetext column survives for
    the long tail (one-off explanations that don't deserve a code).
    Codes are scoped by movement type so ``sale`` and ``po_receipt``
    don't compete for the same pick list.

    ``(movement_type, code)`` is unique across active + archived rows
    so archiving doesn't free a code — matches the rest of the
    codebase's archive convention.
    """

    __tablename__ = "reason_codes"
    __table_args__ = (
        Index(
            "uq_reason_codes_type_code",
            "movement_type",
            "code",
            unique=True,
        ),
        Index("ix_reason_codes_movement_type", "movement_type"),
        Index("ix_reason_codes_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # ``MovementType`` value the code applies to (``in``, ``out``,
    # ``adjustment``, ``transfer``, ``stage_change``). Stored as a
    # plain string so the FK in ``stock_movements.reason_code_id``
    # can be enforced at the DB level without a separate type-
    # consistency trigger.
    movement_type: Mapped[str] = mapped_column(String(16), nullable=False)
    code: Mapped[str] = mapped_column(String(32), nullable=False)
    label: Mapped[str] = mapped_column(String(128), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
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
            f"<ReasonCode id={self.id} type={self.movement_type!r} "
            f"code={self.code!r} label={self.label!r}>"
        )


class AppSetting(Base):
    """Operator-tunable runtime settings.

    Key-value store of strings — callers parse to their expected type
    via the helpers in ``app.app_settings_store``. Migration 0045 seeds
    the two stones-tracking thresholds (spec §10.1); future tuning
    knobs (e.g. metal-spot-stale-after-days) land here without a
    schema change.

    No admin UI yet — operators tune via SQL UPDATE. A follow-up slice
    will add ``/admin/app-settings`` if the surface grows enough to
    warrant one.
    """

    __tablename__ = "app_settings"
    __table_args__ = (Index("uq_app_settings_key", "key", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(String(2000), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
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
        return f"<AppSetting id={self.id} key={self.key!r} value={self.value!r}>"


class SequenceCounter(Base):
    """Generic single-row-per-name counter for non-per-leaf sequence allocators.

    Created in S1 to back the ``STN-NNNNNN`` stone-code allocator. Pre-seeded
    with one row, ``name='stone_code'``. Future slices may add rows for any
    other global counter (e.g. ``design_code`` if S3 lands) — each as its own
    row, no schema change required.

    The single mutation pathway is ``UPDATE ... RETURNING next_value`` (see
    ``app.stones.allocate_stone_code``). SQLite (>= 3.35) and Postgres both
    support this so the read-and-increment happens atomically in one
    round-trip, mirroring ``app.sku.allocate_sequence``.
    """

    __tablename__ = "sequence_counters"
    __table_args__ = (
        Index("uq_sequence_counters_name", "name", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    next_value: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
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
            f"<SequenceCounter id={self.id} name={self.name!r} "
            f"next_value={self.next_value}>"
        )


class StoneShape(Base):
    """Administered lookup of stone shapes (round, oval, cushion, ...).

    Replaces the legacy freetext ``Item.stone_shape`` for new entities
    (``Stone.shape_id``). The freetext column survives on ``Item`` for back
    compat per S1 spec, deprecated but not dropped — eventual removal once
    usage drops to zero.

    Soft-deletable, never hard-deleted. Same archive-doesn't-free-the-name
    convention as ``Supplier`` / ``Location``: the unique constraint on
    ``name`` covers active + archived rows.
    """

    __tablename__ = "stone_shape_master"
    __table_args__ = (
        Index("uq_stone_shape_master_name", "name", unique=True),
        Index("ix_stone_shape_master_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
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
            f"<StoneShape id={self.id} name={self.name!r} "
            f"archived={self.archived_at is not None}>"
        )


class Stone(Base):
    """A tracked stone (anything with a grading certificate or above the melee threshold).

    Tracking rule: if a stone has a diamond/grading report it is a tracked
    entity. Otherwise it is melee, carried on the parent ``Item`` as aggregate
    count + carat weight (``Item.melee_count`` / ``Item.melee_total_ct``).

    ``status``, ``current_item_id`` and ``current_location_id`` are
    denormalised from the latest relevant ``stone_events`` row. The single
    mutation pathway (set / unset / sell / relocate) writes the ledger row
    AND updates the denormalised fields in one transaction — same posture as
    ``items.current_qty`` from ``cost_layers``.

    Soft-deletable; ``stone_code`` is unique across active + archived rows
    (mirrors the ``Item.sku`` convention — re-using a code would silently
    misattribute history). ``cert_number`` is partial-unique scoped by
    ``lab``: a certificate number is unique within an issuing lab.
    """

    __tablename__ = "stones"
    __table_args__ = (
        Index("uq_stones_stone_code", "stone_code", unique=True),
        # Partial unique: a given (lab, cert_number) pair is unique when both
        # are set. Multiple uncertificated stones (lab/cert NULL) coexist.
        Index(
            "uq_stones_cert",
            "lab",
            "cert_number",
            unique=True,
            sqlite_where=text("cert_number IS NOT NULL AND lab IS NOT NULL"),
            postgresql_where=text("cert_number IS NOT NULL AND lab IS NOT NULL"),
        ),
        Index("ix_stones_supplier_id", "supplier_id"),
        Index("ix_stones_current_location_id", "current_location_id"),
        Index("ix_stones_current_item_id", "current_item_id"),
        Index("ix_stones_status", "status"),
        Index("ix_stones_archived_at", "archived_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # Format ``STN-NNNNNN``, system-allocated. Mirrors the SKU allocator.
    stone_code: Mapped[str] = mapped_column(String(32), nullable=False)
    stone_type: Mapped[StoneType] = mapped_column(
        SAEnum(
            StoneType,
            name="stone_type",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    shape_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stone_shape_master.id", ondelete="RESTRICT"),
        nullable=False,
    )
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    depth_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 3), nullable=True)
    carat_weight: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    colour_grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    clarity_grade: Mapped[str | None] = mapped_column(String(8), nullable=True)
    cut_grade: Mapped[str | None] = mapped_column(String(16), nullable=True)
    polish: Mapped[str | None] = mapped_column(String(16), nullable=True)
    symmetry: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fluorescence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    lab: Mapped[StoneLab | None] = mapped_column(
        SAEnum(
            StoneLab,
            name="stone_lab",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    cert_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    cert_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    origin: Mapped[StoneOrigin] = mapped_column(
        SAEnum(
            StoneOrigin,
            name="stone_origin",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=StoneOrigin.NATURAL,
        server_default=StoneOrigin.NATURAL.value,
    )
    treatment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    supplier_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("suppliers.id", ondelete="RESTRICT"),
        nullable=True,
    )
    ownership: Mapped[StoneOwnership] = mapped_column(
        SAEnum(
            StoneOwnership,
            name="stone_ownership",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=StoneOwnership.OWNED,
        server_default=StoneOwnership.OWNED.value,
    )
    # Required when ``ownership = memo`` (enforced in the route layer rather
    # than via a DB-level CHECK to keep the cross-dialect path simple).
    memo_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    acquisition_cost: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    acquisition_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_location_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    status: Mapped[StoneStatus] = mapped_column(
        SAEnum(
            StoneStatus,
            name="stone_status",
            native_enum=False,
            length=24,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=StoneStatus.AVAILABLE,
        server_default=StoneStatus.AVAILABLE.value,
    )
    # Denormalised pointer to the item this stone is currently set into. NULL
    # until status flips to ``set``; cleared on unset. Maintained by the
    # set/unset routes in the same transaction as the ledger event.
    current_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # Spec §10.1: record *why* this stone is being tracked rather than
    # left as melee. Both columns are nullable so legacy stones (rows
    # written before migration 0045) keep working without a backfill;
    # the route layer enforces a non-null trigger on every fresh create.
    tracking_trigger: Mapped[TrackingTrigger | None] = mapped_column(
        SAEnum(
            TrackingTrigger,
            name="tracking_trigger",
            native_enum=False,
            length=32,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    tracking_override_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
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
        return (
            f"<Stone id={self.id} code={self.stone_code!r} "
            f"type={self.stone_type} carat={self.carat_weight} "
            f"status={self.status}>"
        )


class StoneEvent(Base):
    """Append-only ledger of stone lifecycle transitions.

    Stones are entities with their own non-quantity lifecycle, so they need
    their own ledger — extending ``stock_movements`` would conflate FIFO
    qty/value flows with stone state changes. Single write pathway: the
    set/unset/sell/relocate handlers write a ``StoneEvent`` AND update the
    denormalised ``Stone`` fields in one transaction.

    Append-only by mission posture (matches ``StockMovement`` / ``AuditLog``):
    no ``archived_at``, no UPDATE / DELETE handlers. Corrections are made by
    a new event.
    """

    __tablename__ = "stone_events"
    __table_args__ = (
        Index("ix_stone_events_stone_id", "stone_id"),
        Index("ix_stone_events_created_at", "created_at"),
        Index("ix_stone_events_event_type", "event_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    stone_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stones.id", ondelete="RESTRICT"),
        nullable=False,
    )
    # Free-string event type to keep the ledger flexible. Known values:
    # ``created``, ``set``, ``unset``, ``sold``, ``returned``, ``lost``,
    # ``relocated``, ``cert_updated``, ``ownership_changed``.
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    from_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    to_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    from_location_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    to_location_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("locations.id", ondelete="RESTRICT"),
        nullable=True,
    )
    from_status: Mapped[StoneStatus | None] = mapped_column(
        SAEnum(
            StoneStatus,
            name="stone_status",
            native_enum=False,
            length=24,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    to_status: Mapped[StoneStatus | None] = mapped_column(
        SAEnum(
            StoneStatus,
            name="stone_status",
            native_enum=False,
            length=24,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )
    actor_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    note: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<StoneEvent id={self.id} stone_id={self.stone_id} "
            f"type={self.event_type!r} actor={self.actor_id}>"
        )


class ItemStone(Base):
    """Linkage between an ``Item`` and a ``Stone`` with position semantics.

    A ring can hold multiple tracked stones (trilogy = 3, halo with centre + N
    side stones). This is a join table with soft-end semantics: the active set
    has ``unset_at IS NULL``. When a centre stone is replaced, the original
    row's ``unset_at`` is filled in and a new row inserted — historical record
    intact. Same posture as the rest of the codebase's archive convention.

    Two partial unique indexes protect the active set:
    - ``uq_item_stones_active_stone`` — a stone can live in at most one item
      at a time.
    - ``uq_item_stones_position`` — only one stone occupies a given slot
      (``item_id`` + ``position`` + ``position_index``) at a time.
    """

    __tablename__ = "item_stones"
    __table_args__ = (
        Index(
            "uq_item_stones_active_stone",
            "stone_id",
            unique=True,
            sqlite_where=text("unset_at IS NULL"),
            postgresql_where=text("unset_at IS NULL"),
        ),
        Index(
            "uq_item_stones_position",
            "item_id",
            "position",
            "position_index",
            unique=True,
            sqlite_where=text("unset_at IS NULL"),
            postgresql_where=text("unset_at IS NULL"),
        ),
        Index("ix_item_stones_item_id", "item_id"),
        Index("ix_item_stones_stone_id", "stone_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    stone_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("stones.id", ondelete="RESTRICT"),
        nullable=False,
    )
    position: Mapped[StonePosition] = mapped_column(
        SAEnum(
            StonePosition,
            name="stone_position",
            native_enum=False,
            length=16,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    position_index: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    set_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    unset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return (
            f"<ItemStone id={self.id} item_id={self.item_id} "
            f"stone_id={self.stone_id} position={self.position} "
            f"position_index={self.position_index} "
            f"unset={self.unset_at is not None}>"
        )


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
        Index("ix_items_centre_stone_id", "centre_stone_id"),
        Index("ix_items_metal_id", "metal_id"),
        Index("ix_items_secondary_metal_id", "secondary_metal_id"),
        Index("ix_items_unit_id", "unit_id"),
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
    # Standardised fields promoted from the legacy ``item_field_values`` path
    # (migration 0024). Each is nullable; per-category visibility decides
    # which appear on the items form. ``unit_cost`` is intentionally not a
    # column here — FIFO cost layers are the source of truth.
    ring_size: Mapped[str | None] = mapped_column(String(64), nullable=True)
    weight_grams: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    stone_shape: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Stone master integration (S1 of the architectural additions spec).
    # ``centre_stone_id`` is the denormalised pointer to the current centre
    # stone — the ``item_stones`` row with ``position=centre`` and
    # ``unset_at IS NULL``. Allows fast queries without joining; the set/unset
    # handlers maintain it. ``total_carat_weight`` is derived: sum of tracked
    # stones in this item + ``melee_total_ct``; refreshed on set/unset and on
    # melee field changes. The freetext ``stone_shape`` column above is
    # deprecated by ``stone_shape_master`` but kept for back-compat until
    # usage drops to zero.
    centre_stone_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("stones.id", ondelete="RESTRICT"),
        nullable=True,
    )
    total_carat_weight: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    melee_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    melee_total_ct: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("0"), server_default=text("0")
    )
    melee_stone_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Metal master integration (S2 of the architectural additions spec).
    # ``metal_id`` is the primary metal of the item (the alloy the bulk of
    # the piece is cast in). ``secondary_metal_id`` covers two-tone pieces.
    # ``pure_metal_weight_g`` is derived: ``weight_grams * metal.purity_pct``,
    # cached on the row for precious-metal accounting reports without a join.
    metal_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("metal_master.id", ondelete="RESTRICT"),
        nullable=True,
    )
    secondary_metal_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("metal_master.id", ondelete="RESTRICT"),
        nullable=True,
    )
    pure_metal_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    # Unit-master FK (S5 of the architectural additions spec). The legacy
    # freetext ``unit`` column above stays — backfill of existing freetext
    # values to ``unit_id`` is deferred per the spec's "defer until
    # reporting starts to bite" posture.
    unit_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("unit_master.id", ondelete="RESTRICT"),
        nullable=True,
    )
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
        Index("ix_stock_movements_reason_code_id", "reason_code_id"),
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
    # Reason-code FK (S5 of the architectural additions spec). The
    # freetext ``reason`` above stays for the long tail (one-off
    # explanations that don't fit a known code).
    reason_code_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("reason_codes.id", ondelete="RESTRICT"),
        nullable=True,
    )
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


# ---------------------------------------------------------------------------
# S4 attribute-group models (architectural additions spec §4)
#
# Each row is a per-item extension of ``items``. ``item_id`` is both PK and
# the CASCADE FK — deleting an item drops the side row automatically. Every
# attribute column is nullable so a partially-described item is still valid.
# The catalog dispatcher (storage=SIDE_TABLE — landed in a follow-up slice)
# will lazy-create the row when the first non-NULL value is written, and
# delete it when every value goes back to NULL.
# ---------------------------------------------------------------------------


def _saenum(enum_cls: type[enum.StrEnum], *, name: str, length: int = 16) -> SAEnum:
    """One-liner factory for the non-native string enum shape used everywhere.

    All attribute-group enums share the same SQLAlchemy shape: non-native,
    string-backed, value-callable so the wire format is the lowercase
    ``.value`` rather than the Python member name. Centralising the
    boilerplate keeps each ``mapped_column`` short.
    """

    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda cls: [e.value for e in cls],
    )


class ItemRingAttrs(Base):
    """Per-item ring attributes (engagement, wedding, dress rings).

    Side table covering everything that's meaningful for a ring physically:
    size + standard, band geometry, profile, finish, shank style. The
    ``ring_size`` column on ``items`` (freetext String(64)) is **not**
    superseded yet — new ring categories use this numeric column, the legacy
    column stays during migration per the S1 spec's posture on
    ``Item.stone_shape``.
    """

    __tablename__ = "item_ring_attrs"

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ring_size: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    size_standard: Mapped[RingSizeStandard | None] = mapped_column(
        _saenum(RingSizeStandard, name="ring_size_standard"), nullable=True
    )
    resize_tolerance_low: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    resize_tolerance_high: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    band_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    band_depth_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    profile: Mapped[BandProfile | None] = mapped_column(
        _saenum(BandProfile, name="band_profile"), nullable=True
    )
    finish: Mapped[MetalFinish | None] = mapped_column(
        _saenum(MetalFinish, name="metal_finish"), nullable=True
    )
    comfort_fit: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    shank_style: Mapped[ShankStyle | None] = mapped_column(
        _saenum(ShankStyle, name="shank_style"), nullable=True
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
        return f"<ItemRingAttrs item_id={self.item_id} ring_size={self.ring_size}>"


class ItemEngagementAttrs(Base):
    """Per-item engagement-ring attributes (setting, prongs, gallery, mount).

    Only meaningful for engagement-ring items. ``pairs_with_wedding_band_item_id``
    points to a matched wedding band when the ring is sold as part of a set
    (FK RESTRICT — archiving the band shouldn't break the linkage).
    ``mount_price`` is the cost of the mount minus the centre stone — useful
    for quoting different stones against the same mount.
    """

    __tablename__ = "item_engagement_attrs"
    __table_args__ = (
        Index(
            "ix_item_engagement_attrs_pairs_with_wedding_band",
            "pairs_with_wedding_band_item_id",
        ),
    )

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    setting_style: Mapped[SettingStyle | None] = mapped_column(
        _saenum(SettingStyle, name="setting_style"), nullable=True
    )
    setting_variation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    prong_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prong_style: Mapped[ProngStyle | None] = mapped_column(
        _saenum(ProngStyle, name="prong_style"), nullable=True
    )
    gallery_style: Mapped[GalleryStyle | None] = mapped_column(
        _saenum(GalleryStyle, name="gallery_style"), nullable=True
    )
    under_bezel: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    pairs_with_wedding_band_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    mount_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
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
            f"<ItemEngagementAttrs item_id={self.item_id} "
            f"setting={self.setting_style}>"
        )


class ItemBandAttrs(Base):
    """Per-item wedding / dress band attributes.

    ``pairs_with_engagement_item_id`` is the inverse of
    ``ItemEngagementAttrs.pairs_with_wedding_band_item_id`` — the route layer
    will keep both in sync when a his/hers set is registered.
    ``matching_set_id`` is an optional freetext code to group multiple
    bands as a set (his/hers/ours).
    """

    __tablename__ = "item_band_attrs"
    __table_args__ = (
        Index(
            "ix_item_band_attrs_pairs_with_engagement",
            "pairs_with_engagement_item_id",
        ),
    )

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    band_set_style: Mapped[BandSetStyle | None] = mapped_column(
        _saenum(BandSetStyle, name="band_set_style"), nullable=True
    )
    pairs_with_engagement_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
        nullable=True,
    )
    matching_set_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
            f"<ItemBandAttrs item_id={self.item_id} "
            f"style={self.band_set_style}>"
        )


class ItemEarringAttrs(Base):
    """Per-item earring attributes (pair-or-single, closure, style, geometry).

    ``hoop_diameter_mm`` is nullable; populated only for hoop / huggie
    styles. ``drop_length_mm`` is populated for drop / chandelier / threader
    styles. The route layer enforces consistency in a future slice.
    """

    __tablename__ = "item_earring_attrs"

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    sold_as: Mapped[EarringSold | None] = mapped_column(
        _saenum(EarringSold, name="earring_sold"), nullable=True
    )
    closure_type: Mapped[EarringClosure | None] = mapped_column(
        _saenum(EarringClosure, name="earring_closure"), nullable=True
    )
    style: Mapped[EarringStyle | None] = mapped_column(
        _saenum(EarringStyle, name="earring_style"), nullable=True
    )
    drop_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    hoop_diameter_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
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
        return f"<ItemEarringAttrs item_id={self.item_id} style={self.style}>"


class ItemChainAttrs(Base):
    """Per-item chain / necklace / bracelet attributes.

    Linear products that share a common attribute footprint (style, length,
    link width, closure, what they're worn as). ``min_length_mm`` /
    ``max_length_mm`` are populated only when ``adjustable=True``.
    """

    __tablename__ = "item_chain_attrs"

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    chain_style: Mapped[ChainStyle | None] = mapped_column(
        _saenum(ChainStyle, name="chain_style"), nullable=True
    )
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    adjustable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    min_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    max_length_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    link_width_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    closure_type: Mapped[ChainClosure | None] = mapped_column(
        _saenum(ChainClosure, name="chain_closure"), nullable=True
    )
    worn_as: Mapped[WornAs | None] = mapped_column(
        _saenum(WornAs, name="worn_as"), nullable=True
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
            f"<ItemChainAttrs item_id={self.item_id} "
            f"style={self.chain_style}>"
        )


class ItemPendantAttrs(Base):
    """Per-item pendant attributes (geometry, bail, optional default chain).

    ``default_chain_item_id`` is the chain item the pendant typically ships
    with (when ``includes_chain=True`` or as a recommended pairing) — FK
    RESTRICT keeps the link from breaking if the chain is archived.
    """

    __tablename__ = "item_pendant_attrs"
    __table_args__ = (
        Index(
            "ix_item_pendant_attrs_default_chain", "default_chain_item_id"
        ),
    )

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    bail_type: Mapped[BailType | None] = mapped_column(
        _saenum(BailType, name="bail_type"), nullable=True
    )
    bail_opening_mm: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    includes_chain: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    default_chain_item_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="RESTRICT"),
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
        return f"<ItemPendantAttrs item_id={self.item_id} bail={self.bail_type}>"


class ItemEngravingAttrs(Base):
    """Per-item engraving attributes — orthogonal to category.

    Applies wherever engraving is offered: rings, bands, pendants. The
    Boolean ``engraving_available`` is NOT NULL with a default of False so
    the column is always meaningful — explicitly no for items that don't
    offer engraving. Max-chars columns are nullable because they're optional
    even on engraving-available items.
    """

    __tablename__ = "item_engraving_attrs"

    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("items.id", ondelete="CASCADE"),
        primary_key=True,
    )
    engraving_available: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    max_chars_outside: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_chars_inside: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engraving_text: Mapped[str | None] = mapped_column(String(255), nullable=True)
    engraving_font: Mapped[str | None] = mapped_column(String(64), nullable=True)
    engraving_style: Mapped[EngravingStyle | None] = mapped_column(
        _saenum(EngravingStyle, name="engraving_style"), nullable=True
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
            f"<ItemEngravingAttrs item_id={self.item_id} "
            f"available={self.engraving_available}>"
        )
