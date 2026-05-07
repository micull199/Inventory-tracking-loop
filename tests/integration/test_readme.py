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


class TestRunningAStockTakeSection:
    """DOC5 — pin the stock-take walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Running a stock take")
        assert "_TODO_" not in body, "stock-take section still has _TODO placeholder"
        assert len(body.strip()) > 400, "stock-take section looks unsubstantial"

    def test_section_references_admin_stock_takes_route(self) -> None:
        body = _section("Running a stock take")
        # ``/admin/stock-takes`` is the list route mounted by
        # ``app/stock_takes.py`` (router prefix). A future rename of
        # the prefix fails this test and forces a docs update on the
        # same PR.
        assert "/admin/stock-takes" in body

    def test_section_names_office_role(self) -> None:
        body = _section("Running a stock take")
        # MISSION §3.103 assigns stock takes to Office. DoD #5 names
        # Office explicitly. The section must name the role so a
        # future Office reader knows the page is for them.
        assert "Office" in body

    def test_section_names_scope_options(self) -> None:
        body = _section("Running a stock take")
        # ``_VALID_SCOPE_TYPES`` in ``app/stock_takes.py`` lists three
        # scope types: ``all`` / ``node`` / ``location``. The section
        # must surface all three so a user reading the docs can decide
        # which scope to schedule. Use the user-facing labels rather
        # than the wire values.
        assert "Category" in body
        assert "Location" in body
        assert "All items" in body

    def test_section_names_variance_and_commit_chain(self) -> None:
        body = _section("Running a stock take")
        # The variance → commit chain is the load-bearing semantic of
        # ST3. A future PR that drops "variance" or "commit" from the
        # docs would silently confuse readers about how a count
        # actually changes inventory.
        assert "variance" in body.lower()
        assert "commit" in body.lower()

    def test_section_names_fifo_engine_wiring(self) -> None:
        body = _section("Running a stock take")
        # MISSION §3 (Stock takes) calls out: "Positive adjustments
        # require a unit cost (defaults to the most recent layer's
        # cost) and create a new cost layer. Negative adjustments
        # consume layers FIFO." The section must name the FIFO posture
        # so a user understands why positive + negative variances are
        # priced differently.
        assert "FIFO" in body

    def test_section_names_audit_trail_link(self) -> None:
        body = _section("Running a stock take")
        # A1's audit view at ``/admin/audit`` is the cross-cutting
        # surface where stock-take adjustments become traceable. The
        # section must name the audit trail so a Manager investigating
        # a movement knows how to find the parent stock take.
        assert "audit" in body.lower()


class TestGeneratingPurchaseOrderSection:
    """DOC6 — pin the PO send walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Generating and sending a purchase order")
        assert "_TODO_" not in body, "PO section still has _TODO placeholder"
        assert len(body.strip()) > 400, "PO section looks unsubstantial"

    def test_section_references_admin_purchase_orders_route(self) -> None:
        body = _section("Generating and sending a purchase order")
        # ``/admin/purchase-orders`` is the list-and-detail prefix
        # mounted by ``app/purchase_orders.py``'s ``list_router``.
        # A future rename of the prefix fails this test and forces
        # a docs update on the same PR.
        assert "/admin/purchase-orders" in body

    def test_section_references_reorder_dashboard_route(self) -> None:
        body = _section("Generating and sending a purchase order")
        # ``/admin/reorder`` is the entry point for drafting POs
        # from low-stock items. A future rename of the prefix
        # (currently mounted by ``app/purchase_orders.py``'s
        # ``draft_router``) fails this test.
        assert "/admin/reorder" in body

    def test_section_names_office_role(self) -> None:
        body = _section("Generating and sending a purchase order")
        # MISSION §3.103 assigns POs to Office. DoD #6 names Office
        # explicitly. The section must name the role so a future
        # Office reader knows the page is for them.
        assert "Office" in body

    def test_section_names_po_statuses(self) -> None:
        body = _section("Generating and sending a purchase order")
        # ``app/models.py::POStatus`` enumerates: draft, sent,
        # partially_received, received, cancelled. The section must
        # name all five so a future PR that renames a status
        # (e.g. ``partially_received`` → ``partial``) fails the
        # suite and forces a docs update on the same PR.
        assert "draft" in body.lower()
        assert "sent" in body.lower()
        assert "partially_received" in body
        assert "received" in body.lower()
        assert "cancelled" in body.lower()

    def test_section_names_expected_vs_actual_cost(self) -> None:
        body = _section("Generating and sending a purchase order")
        # MISSION §3 (Reorder and POs / Cost tracking) calls out the
        # expected-vs-actual unit-cost split: expected is what gets
        # emailed; actual is recorded at receipt time and is what
        # creates the FIFO cost layer. The section must name both
        # halves so a user understands why the PO line cost isn't
        # authoritative for stock valuation.
        lower = body.lower()
        assert "expected unit cost" in lower
        assert "actual unit cost" in lower

    def test_section_names_pdf_and_email(self) -> None:
        body = _section("Generating and sending a purchase order")
        # PO3 (PDF) + PO4 (email) are the two send-side artefacts.
        # The section must name both so a future PR that drops
        # either modality from the docs fails the suite.
        assert "PDF" in body
        assert "email" in body.lower()


class TestReceivingStockAgainstPOSection:
    """DOC7 — pin the PO receive walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Receiving stock against a PO")
        assert "_TODO_" not in body, (
            "PO receive section still has _TODO placeholder"
        )
        assert len(body.strip()) > 400, (
            "PO receive section looks unsubstantial"
        )

    def test_section_references_admin_purchase_orders_route(self) -> None:
        body = _section("Receiving stock against a PO")
        # ``/admin/purchase-orders`` is the prefix mounted by
        # ``app/purchase_orders.py``'s ``list_router``. The receive
        # form lives at ``/admin/purchase-orders/{id}/receive``.
        # A future rename of the prefix fails this test and forces
        # a docs update on the same PR.
        assert "/admin/purchase-orders" in body

    def test_section_references_receive_path(self) -> None:
        body = _section("Receiving stock against a PO")
        # The /receive sub-path is the form route mounted at
        # ``app/purchase_orders.py:1110`` (GET) + ``:1137`` (POST).
        # Pinned independently of the prefix so a future rename of
        # ``/receive`` to ``/record-receipt`` fires a granular failure.
        assert "/receive" in body

    def test_section_names_office_role(self) -> None:
        body = _section("Receiving stock against a PO")
        # MISSION §3.103 + DoD #6 assigns POs (including receive)
        # to Office. The role gate at ``app/purchase_orders.py:1114``
        # is ``require_role(Role.MANAGER, Role.OFFICE)``; Admin
        # passes via ``app/auth.py``'s blanket admin override.
        # The section must name Office so a future Office reader
        # knows the page is for them.
        assert "Office" in body

    def test_section_names_receivable_statuses(self) -> None:
        body = _section("Receiving stock against a PO")
        # ``_RECEIVABLE_STATUSES`` at ``app/purchase_orders.py:1059``
        # is exactly ``(SENT, PARTIALLY_RECEIVED)``. The section must
        # name both so a future PR that drops one (or expands the
        # gate to include DRAFT) fails the suite.
        assert "sent" in body.lower()
        assert "partially_received" in body

    def test_section_names_fifo_cost_layer_creation(self) -> None:
        body = _section("Receiving stock against a PO")
        # MISSION §3 (Cost tracking): "Receiving creates a new FIFO
        # cost layer at that actual unit cost." The section must
        # name FIFO + cost layer so a future reader understands why
        # the actual unit cost is load-bearing for stock valuation.
        # ``FIFO`` is the canonical acronym across MISSION + cost-
        # engine source; case-sensitive pin.
        assert "FIFO" in body
        assert "cost layer" in body.lower()

    def test_section_names_actual_vs_expected_cost(self) -> None:
        body = _section("Receiving stock against a PO")
        # MISSION §3 (Reorder and POs / Cost tracking): expected
        # unit cost on the PO line is what gets emailed; actual
        # unit cost is entered at receipt time and is what creates
        # the FIFO cost layer. The section must name both halves
        # so a user understands why the PO line cost isn't
        # authoritative for stock valuation. Mirrors DOC6's
        # same-named test on the send side.
        lower = body.lower()
        assert "expected unit cost" in lower
        assert "actual unit cost" in lower

    def test_section_names_partial_vs_full_status_flip(self) -> None:
        body = _section("Receiving stock against a PO")
        # ``app/purchase_orders.py:1257-1258`` flips the status to
        # RECEIVED iff every line has ``qty_received >= qty_ordered``,
        # else PARTIALLY_RECEIVED. The section must name both
        # branches so a user understands why a receipt sometimes
        # closes the PO and sometimes leaves the receive form
        # available.
        assert "received" in body.lower()
        assert "partially_received" in body


class TestReadingAuditTrailSection:
    """DOC8 — pin the audit-trail read walk-through against drift."""

    def test_section_is_filled(self) -> None:
        body = _section("Reading the audit trail for an item")
        assert "_TODO_" not in body, (
            "audit-trail section still has _TODO placeholder"
        )
        assert len(body.strip()) > 400, (
            "audit-trail section looks unsubstantial"
        )

    def test_section_references_admin_audit_route(self) -> None:
        body = _section("Reading the audit trail for an item")
        # ``/admin/audit`` is the prefix mounted by
        # ``app/audit_routes.py``'s router. A future rename of the
        # prefix fails this test and forces a docs update on the same
        # PR.
        assert "/admin/audit" in body

    def test_section_names_manager_and_admin_roles(self) -> None:
        body = _section("Reading the audit trail for an item")
        # The role gate at ``app/audit_routes.py:94`` is
        # ``Depends(require_role(Role.MANAGER))``; Admin passes via
        # ``app/auth.py``'s blanket admin override. Office and
        # Workshop both 403 (and the nav link is hidden from them
        # at ``app/templates/base.html:175-183``). The section must
        # name both allowed roles so a future Office or Workshop
        # reader knows the page isn't for them.
        assert "Manager" in body
        assert "Admin" in body

    def test_section_names_csv_export(self) -> None:
        body = _section("Reading the audit trail for an item")
        # AC1's CSV branch at ``app/audit_routes.py:105-125`` exports
        # every row (ignores pagination). The section must name the
        # literal ``?format=csv`` path so a future PR that drops the
        # CSV link or renames the query param fails this test.
        assert "/admin/audit?format=csv" in body

    def test_section_names_audit_columns(self) -> None:
        body = _section("Reading the audit trail for an item")
        # The HTML table at ``app/templates/admin_audit.html:25-30``
        # renders six columns: Time / Actor / Action / Entity /
        # Before / After. The section must name the four
        # load-bearing labels (the entity column is implicit in
        # the entity_type:entity_id discussion). Time / Actor /
        # Action are case-sensitive (exact column-header
        # vocabulary); before + after are case-insensitive because
        # they appear both as column labels and as JSON-dict prose.
        assert "Time" in body
        assert "Actor" in body
        assert "Action" in body
        lower = body.lower()
        assert "before" in lower
        assert "after" in lower

    def test_section_names_immutability_invariant(self) -> None:
        body = _section("Reading the audit trail for an item")
        # MISSION §9: "Do not delete the audit log. Do not provide
        # a way to edit it." Backed by ``apply_immutability_triggers``
        # at ``app/audit.py:128`` (SQLite + Postgres UPDATE + DELETE
        # triggers). The section must name the invariant explicitly
        # — both halves: append-only and cannot-be-edited — so a
        # future PR that softens the docs to "the audit log is
        # mostly read-only" fails this test.
        lower = body.lower()
        assert "append-only" in lower
        assert "cannot be edited" in lower

    def test_section_names_canonical_audit_actions(self) -> None:
        body = _section("Reading the audit trail for an item")
        # The action wire-names are the developer-facing vocabulary
        # the Action column renders verbatim. Pinning a sample of
        # canonical names (one per top-level domain — items, stock
        # movements, purchase orders, stock takes) catches a future
        # rename of any action enum (e.g. ``item.create`` instead
        # of ``item.created``, or ``stock_movement.IN`` capitalised).
        # Sourced via ``grep -rEn 'action="(item|stock_movement|
        # purchase_order|stock_take|checkout)\.'`` against ``app/``.
        assert "item.created" in body
        assert "stock_movement.in" in body
        assert "purchase_order.received" in body
        assert "stock_take.committed" in body

    def test_section_explains_no_item_filter_limitation(self) -> None:
        body = _section("Reading the audit trail for an item")
        # The v1 read view has no per-item filter form — A1b is
        # queued in the backlog. The section must call out the
        # limitation explicitly + name the two workaround paths
        # (browser search + CSV filter) so a Manager hunting for an
        # item's history isn't surprised by the missing filter.
        # Same posture as DOC4's ``test_section_explains_archived
        # _item_resolves`` (a load-bearing limitation that needs a
        # forcing function so it can't get silently removed).
        lower = body.lower()
        assert "filter" in lower
        assert "CSV" in body
        # At least one of the three keyboard-search tokens must
        # appear so the section directs users to a concrete
        # workaround rather than leaving them to discover it.
        assert (
            "cmd+f" in lower or "ctrl+f" in lower or "cmd-f" in lower
        )
