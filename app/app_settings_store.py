"""Typed read helpers for the ``app_settings`` key-value store.

Migration 0045 seeds the table with the spec §10.1 thresholds. Callers
use the named-key constants below + the typed getters so we have one
place to add coercion / parsing / default fallback.

Settings are *runtime* tunables — operators edit them via SQL UPDATE
(or a future ``/admin/app-settings`` route). Hot reload is acceptable
because the getters always re-read from the DB on each call; caching
would just complicate the invalidation story for a low-traffic
configuration surface.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting

# Setting keys. Centralised so a typo in a caller surfaces at import
# time rather than as a silent fall-through to the default.
STONES_COST_FLOOR_AUD = "stones.tracking.cost_floor_aud"
STONES_COLOURED_STONE_CT_THRESHOLD = "stones.tracking.coloured_stone_ct_threshold"


def get_setting_decimal(db: Session, key: str, default: Decimal) -> Decimal:
    """Return ``app_settings[key]`` parsed as a ``Decimal``, or ``default``.

    Unparseable values fall back to ``default`` so a corrupted edit
    doesn't bring down the routes that depend on the setting (the
    spec §10.1 thresholds are load-bearing for the create flow). The
    fallback is silent rather than raising — a future settings-admin
    route can validate input at write time.
    """
    row = db.execute(
        select(AppSetting).where(AppSetting.key == key)
    ).scalar_one_or_none()
    if row is None:
        return default
    try:
        return Decimal(row.value)
    except (InvalidOperation, TypeError):
        return default


def stones_cost_floor_aud(db: Session) -> Decimal:
    """Convenience accessor for the AUD cost floor (default $500)."""
    return get_setting_decimal(db, STONES_COST_FLOOR_AUD, Decimal("500"))


def stones_coloured_stone_ct_threshold(db: Session) -> Decimal:
    """Convenience accessor for the coloured-stone ct threshold (default 0.50)."""
    return get_setting_decimal(
        db, STONES_COLOURED_STONE_CT_THRESHOLD, Decimal("0.50")
    )
