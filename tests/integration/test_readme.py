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


class TestCreatingAnItemSection:
    """DOC3 — pin the item-creation walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Creating an item")
        assert "_TODO_" not in body, "item section still has _TODO placeholder"
        assert len(body.strip()) > 400, "item section looks unsubstantial"

    def test_section_references_admin_items_route(self) -> None:
        body = _section("Creating an item")
        assert "/admin/items" in body

    def test_section_names_manager_role(self) -> None:
        body = _section("Creating an item")
        # Item creation is Manager-owned (Admin always passes); the section
        # must name the role so a future Office reader knows why the form
        # is hidden from them.
        assert "Manager" in body

    def test_section_names_required_core_fields(self) -> None:
        body = _section("Creating an item")
        # The four required core fields enforced by ``app/items.py``'s
        # ``_normalise`` helper. A future PR that drops one fails this test.
        assert "SKU" in body
        assert "Name" in body
        assert "Category" in body
        assert "Unit" in body
        assert "required" in body

    def test_section_names_tracking_modes(self) -> None:
        body = _section("Creating an item")
        # The two ``TrackingMode`` enum values. If a future rename of the
        # vocabulary lands, this test fails and forces the docs to update.
        assert "qty" in body
        assert "unique" in body

    def test_section_names_requires_checkout_concept(self) -> None:
        body = _section("Creating an item")
        # The ``requires_checkout`` flag is the primary differentiator for
        # tool/mould workflow visibility (DoD #4). The section must name it
        # so Manager readers know how to enable check-out for those items.
        assert "check-out" in body.lower() or "check out" in body.lower()

    def test_section_explains_archive_posture(self) -> None:
        body = _section("Creating an item")
        assert "Archive" in body
        assert "Unarchive" in body


class TestPrintingQrAndScanningSection:
    """DOC4 — pin the scan-mode walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Printing a QR label and scanning it")
        assert "_TODO_" not in body, "scan section still has _TODO placeholder"
        assert len(body.strip()) > 300, "scan section looks unsubstantial"

    def test_section_references_scan_route(self) -> None:
        body = _section("Printing a QR label and scanning it")
        # The /scan landing page is the user-visible entry point. A future
        # rename of the prefix (currently fixed by ``router = APIRouter(
        # prefix="/scan", ...)`` in ``app/scan.py``) fails this test and
        # forces a docs update on the same PR.
        assert "/scan" in body

    def test_section_references_scan_item_action_picker_route(self) -> None:
        body = _section("Printing a QR label and scanning it")
        # ``/scan/item/{id}`` is the action picker reached via the 303
        # redirect from ``POST /scan/resolve``. Documenting it explicitly
        # gives readers a URL to bookmark when debugging.
        assert "/scan/item/{id}" in body

    def test_section_names_workshop_role(self) -> None:
        body = _section("Printing a QR label and scanning it")
        # Scanning is a Workshop-primary surface (Manager + Office + Admin
        # also pass via ``require_role``). The section must name Workshop
        # so a future Workshop reader knows the page is for them.
        assert "Workshop" in body

    def test_section_names_qr_vs_sku_resolution_precedence(self) -> None:
        body = _section("Printing a QR label and scanning it")
        # The ``_resolve_code`` helper in ``app/scan.py`` looks up
        # ``qr_code`` first, then ``sku``. The README must explain this
        # so users diagnosing a wrong-item resolution understand the
        # order. ``QR`` and ``SKU`` (case-sensitive) cover both halves.
        assert "QR" in body
        assert "SKU" in body
        assert "qr_code" in body or "QR code" in body

    def test_section_names_camera_and_usb_scanner_postures(self) -> None:
        body = _section("Printing a QR label and scanning it")
        # MISSION §3 calls out both modalities: "Works on a desktop with
        # a USB scanner and on a phone/tablet camera." A future PR that
        # drops one of them from the docs fails this test.
        assert "USB" in body
        assert "camera" in body.lower()

    def test_section_explains_archived_item_resolves(self) -> None:
        body = _section("Printing a QR label and scanning it")
        # SC1a's archive-still-resolves posture is observable in
        # ``scan.html``: an archived item's scan page renders the badge
        # + a note directing the user to the items list. The README must
        # name the behaviour so a workshop user scanning an old physical
        # label isn't surprised when the action forms are missing.
        assert "archived" in body.lower()
