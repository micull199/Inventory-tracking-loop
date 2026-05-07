"""Forcing-function tests pinning the README's per-feature workflow sections.

The README has a "Common workflows" section per MISSION §10's DoD #12 wording:
"how to add a supplier and an item, how to do a stock take." Each workflow is
filled in by a small docs slice. These tests guard against accidental
regressions (e.g. a future PR overwriting a filled section with ``_TODO``) and
against drift between the docs and the actual UI (e.g. the supplier route
gets renamed but the README still references the old path).

Per MISSION §8 ("Tests are the verification signal"), these are integration-
flavoured (read on-disk) rather than unit-flavoured (parsed in memory) so the
forcing function fires against what users actually read.
"""

from __future__ import annotations

from pathlib import Path

_README = Path(__file__).resolve().parents[2] / "README.md"


def _section(heading: str) -> str:
    """Return the body between ``### {heading}`` and the next ``### `` or end-of-doc."""
    text = _README.read_text(encoding="utf-8")
    marker = f"### {heading}"
    start = text.find(marker)
    assert start != -1, f"README missing section: {marker!r}"
    body_start = start + len(marker)
    next_h3 = text.find("\n### ", body_start)
    next_h2 = text.find("\n## ", body_start)
    candidates = [c for c in (next_h3, next_h2) if c != -1]
    end = min(candidates) if candidates else len(text)
    return text[body_start:end]


class TestAddingANewSupplierSection:
    """DOC1 — pin the supplier walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Adding a new supplier")
        assert "_TODO_" not in body, "supplier section still has _TODO placeholder"
        assert len(body.strip()) > 200, "supplier section looks unsubstantial"

    def test_section_references_admin_suppliers_route(self) -> None:
        body = _section("Adding a new supplier")
        assert "/admin/suppliers" in body

    def test_section_names_manager_role(self) -> None:
        body = _section("Adding a new supplier")
        assert "Manager" in body

    def test_section_names_required_name_field(self) -> None:
        body = _section("Adding a new supplier")
        assert "Name" in body
        assert "required" in body

    def test_section_explains_archive_posture(self) -> None:
        body = _section("Adding a new supplier")
        assert "Archive" in body
        assert "Unarchive" in body


class TestDefiningACategorySection:
    """DOC2 — pin the taxonomy + field-defs walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Defining a new category and its custom fields")
        assert "_TODO_" not in body, "category section still has _TODO placeholder"
        assert len(body.strip()) > 400, "category section looks unsubstantial"

    def test_section_references_admin_taxonomy_route(self) -> None:
        body = _section("Defining a new category and its custom fields")
        assert "/admin/taxonomy" in body

    def test_section_names_manager_role(self) -> None:
        body = _section("Defining a new category and its custom fields")
        assert "Manager" in body

    def test_section_names_required_name_field(self) -> None:
        body = _section("Defining a new category and its custom fields")
        assert "Name" in body
        assert "required" in body

    def test_section_names_a_field_type(self) -> None:
        body = _section("Defining a new category and its custom fields")
        # Name at least one of the supported FieldType values so a future rename
        # of the type vocabulary forces a docs update.
        assert "select" in body
        assert "multiselect" in body

    def test_section_explains_archive_posture(self) -> None:
        body = _section("Defining a new category and its custom fields")
        assert "Archive" in body
        assert "Unarchive" in body

    def test_section_explains_leaf_node_concept(self) -> None:
        body = _section("Defining a new category and its custom fields")
        # The leaf-node rule is load-bearing for the taxonomy: fields attach to
        # leaves only, and adding a sub-category turns a Category into a non-leaf.
        assert "leaf" in body.lower()
        assert "Sub-category" in body or "sub-category" in body
