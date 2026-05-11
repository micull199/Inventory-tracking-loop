"""Unit tests for the ``Archetype`` enum.

The enum drives the taxonomy refinement (see
``docs/taxonomy-refinement-plan.md``). These tests pin the wire-level values
because the column is stored as a non-native ``String(16)`` (see
``app/models.py::TaxonomyNode.archetype``), so the ``.value`` strings are
load-bearing for both migration backfills and any downstream comparisons in
templates / routes.
"""

from __future__ import annotations

import enum

from app.models import Archetype


class TestArchetypeEnum:
    def test_has_exactly_three_members(self) -> None:
        # Pinning the count makes it impossible to silently add a fourth
        # archetype without updating every consumer.
        assert len(list(Archetype)) == 3

    def test_member_values(self) -> None:
        assert Archetype.UNIQUE.value == "unique"
        assert Archetype.BULK.value == "bulk"
        assert Archetype.UNIQUE_VARIANT.value == "unique_variant"

    def test_is_str_enum(self) -> None:
        # ``StrEnum`` semantics matter — the migration backfills with
        # ``"bulk"`` and the column comparison ``archetype == "bulk"`` must
        # hold without an explicit ``.value`` lookup.
        assert issubclass(Archetype, enum.StrEnum)
        assert Archetype.BULK == "bulk"
        assert Archetype.UNIQUE == "unique"
        assert Archetype.UNIQUE_VARIANT == "unique_variant"

    def test_lookup_by_value(self) -> None:
        # Round-trip the wire value through the enum constructor — this is
        # how the route layer will rehydrate a form field.
        assert Archetype("unique") is Archetype.UNIQUE
        assert Archetype("bulk") is Archetype.BULK
        assert Archetype("unique_variant") is Archetype.UNIQUE_VARIANT
