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


def _h2_section(heading: str) -> str:
    """Return the body between ``## {heading}`` and the next ``## `` or end-of-doc."""
    text = _README.read_text(encoding="utf-8")
    marker = f"## {heading}"
    start = text.find(marker)
    assert start != -1, f"README missing section: {marker!r}"
    body_start = start + len(marker)
    next_h2 = text.find("\n## ", body_start)
    end = next_h2 if next_h2 != -1 else len(text)
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
        assert "_TODO_" not in body, "PO receive section still has _TODO placeholder"
        assert len(body.strip()) > 400, "PO receive section looks unsubstantial"

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
        assert "_TODO_" not in body, "audit-trail section still has _TODO placeholder"
        assert len(body.strip()) > 400, "audit-trail section looks unsubstantial"

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
        assert "cmd+f" in lower or "ctrl+f" in lower or "cmd-f" in lower


class TestQuickLinksAndTechStackTodoResolution:
    """DOC9a — pin the Quick Links + Tech Stack ``_TODO`` resolutions against drift.

    Two ``_TODO`` blocks were closed by DOC9a:

    1. **Quick links → changelog**: ``CHANGELOG.md`` exists at repo root and is
       now linked from the Quick links section.
    2. **Tech stack → PDF library**: ``reportlab`` was chosen during PO3 (see
       ``pyproject.toml`` dep + ``app/pdf.py`` renderer) — the README now names
       the choice instead of carrying the original "WeasyPrint or reportlab"
       placeholder.

    Two ``_TODO`` blocks remain *intentionally* unresolved (deferred to P4 +
    DOC10): the deployed-URL link in Quick links + the deploy target line in
    Tech stack + the ``## Deployment`` section + the Contributing + License
    footer. DOC9a does not pin the un-resolved ``_TODO``s so a future P4 / DOC10
    can flip them without touching this test class.
    """

    def test_quick_links_changelog_link_resolved(self) -> None:
        body = _h2_section("Quick links")
        # The link must exist with the canonical ``[Changelog](./CHANGELOG.md)``
        # markdown shape. A future PR that drops the link or renames the file
        # without updating the link fails the first assertion.
        assert "[Changelog](./CHANGELOG.md)" in body
        # The placeholder must be gone. A future PR that adds the link
        # without removing the ``_TODO`` placeholder fails the second
        # assertion. Two-pin shape catches both forward-direction
        # regressions (drop link / leave placeholder).
        assert "_TODO: changelog" not in body

    def test_tech_stack_pdf_library_resolved(self) -> None:
        body = _h2_section("Tech stack")
        # ``reportlab`` is the canonical lowercase library name (matches
        # PyPI + the import path). Case-sensitive substring catches a
        # future demotion to ``ReportLab`` / ``Reportlab`` that would
        # mismatch the actual import / dep name.
        assert "reportlab" in body
        # The pre-resolution placeholder phrase must be gone. Same
        # two-pin shape as ``test_quick_links_changelog_link_resolved``.
        assert "_TODO (WeasyPrint or reportlab" not in body

    def test_pdf_choice_matches_pyproject(self) -> None:
        # Forces a docs update on a future swap of the PDF library: if
        # ``pyproject.toml`` ever switches to ``weasyprint`` or removes
        # ``reportlab``, the README would no longer match and this test
        # fires. Same docs ↔ source consistency-pinning posture as A2's
        # ``test_audit_coverage.py`` cross-cutting source-text sweep.
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8").lower()
        assert "reportlab" in text, (
            "reportlab missing from pyproject.toml — README claims it is "
            "the PDF library; either restore the dep or update README"
        )
        assert "weasyprint" not in text, (
            "weasyprint present in pyproject.toml — README claims reportlab "
            "is the chosen PDF library; either remove the weasyprint dep "
            "or update README"
        )

    def test_changelog_file_exists_at_linked_path(self) -> None:
        # Forces a docs update on a future PR that moves
        # ``CHANGELOG.md`` without updating the README link (would be a
        # broken-link regression in user-facing docs). Pinned at the
        # exact path the README links to (``./CHANGELOG.md`` relative to
        # the README, which is repo root).
        changelog = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
        assert changelog.exists(), (
            "CHANGELOG.md missing at repo root — README links to "
            "./CHANGELOG.md from Quick links but the file does not exist"
        )

    def test_quick_links_deployed_url_resolved(self) -> None:
        # P4 closed the ``_TODO: deployed URL_`` placeholder by replacing
        # it with a note pointing to the Fly.io hostname.  Two-pin shape:
        # placeholder gone + a Fly.io reference present.
        body = _h2_section("Quick links")
        assert "_TODO: deployed URL" not in body, (
            "Quick links still carries the deployed-URL _TODO placeholder; "
            "fill it in per the P4 slice instructions"
        )
        assert "fly" in body.lower(), (
            "Quick links deployed-URL line does not reference Fly.io; "
            "expected a fly.dev hostname or 'fly launch' reference"
        )

    def test_tech_stack_deploy_target_resolved(self) -> None:
        # P4 replaced ``_TODO (Fly.io or Render)_`` with the chosen
        # deploy target. Two-pin: placeholder gone + Fly.io named.
        body = _h2_section("Tech stack")
        assert "_TODO (Fly.io or Render)" not in body, (
            "Tech stack still carries the deploy-target _TODO placeholder; "
            "fill it in per the P4 slice instructions"
        )
        assert "Fly.io" in body, (
            "Tech stack does not name Fly.io as the chosen deploy target; "
            "either update README or revise this test if the target changed"
        )


class TestContributingAndLicenseFooter:
    """DOC10 — pin the Contributing + License footer ``_TODO`` resolutions.

    Two ``_TODO`` placeholders were closed by DOC10:

    1. **Contributing**: explains the loop-driven posture, points at
       ``MISSION.md`` + ``PROGRESS.md`` + ``CHANGELOG.md``, and calls out
       that external pull requests are not the workflow (this is an
       internal UC build, not open-source).
    2. **License**: matches ``pyproject.toml``'s canonical declaration
       ``license = { text = "Proprietary" }`` — no rights granted to
       outside parties.

    Three ``_TODO`` blocks remain *intentionally* unresolved (deferred to
    P4): the deployed-URL link in Quick links + the deploy target line
    in Tech stack + the ``## Deployment`` section. DOC10 does not pin
    those so a future P4 can flip them without touching this class.
    """

    def test_contributing_section_is_filled(self) -> None:
        body = _h2_section("Contributing")
        assert "_TODO_" not in body, "Contributing section still has _TODO placeholder"
        # Substantial section: covers the loop posture, the canonical
        # source files, the no-external-PRs invariant, and where to
        # file feedback. ~150-200 words is comfortably above 200 chars.
        assert len(body.strip()) > 200, "Contributing section looks unsubstantial"

    def test_contributing_section_references_mission_and_progress(self) -> None:
        # Section's whole purpose is to point a curious reader at the
        # loop's two source-of-truth files. A future PR that drops
        # either reference fails this test on first run.
        body = _h2_section("Contributing")
        assert "MISSION.md" in body
        assert "PROGRESS.md" in body

    def test_contributing_section_names_loop_driven_posture(self) -> None:
        # Pins the project's "this is a Claude Code autonomous build
        # loop" identity. A future PR that softens the prose to "this
        # is a normal repo" would fail this test. Case-insensitive on
        # ``loop`` so prose like "the build loop" or "Loop" both pass.
        body = _h2_section("Contributing")
        assert "loop" in body.lower()

    def test_contributing_section_says_external_prs_are_not_workflow(self) -> None:
        # Forces the section to call out the no-external-PRs
        # convention. One-of-two disjunction (``pull request`` OR
        # ``external``) tolerates both natural phrasings:
        # "external pull requests are not the workflow" or
        # "this is an internal build, not external contribution".
        body = _h2_section("Contributing").lower()
        assert "pull request" in body or "external" in body

    def test_license_section_is_filled(self) -> None:
        body = _h2_section("License")
        assert "_TODO_" not in body, "License section still has _TODO placeholder"
        # ~50-100 words is comfortably above 50 chars.
        assert len(body.strip()) > 50, "License section looks unsubstantial"

    def test_license_section_names_proprietary(self) -> None:
        # Case-sensitive on the canonical capitalisation that matches
        # ``pyproject.toml``'s declaration. A future PR that demotes
        # ``Proprietary`` to ``proprietary`` would mismatch the source.
        body = _h2_section("License")
        assert "Proprietary" in body

    def test_license_matches_pyproject(self) -> None:
        # Forces a docs update on a future swap of ``pyproject.toml``'s
        # licence field. Same docs ↔ source consistency-pinning posture
        # as ``test_pdf_choice_matches_pyproject``. Pinned to the exact
        # canonical declaration string so a swap in either direction
        # (README→pyproject or pyproject→README) gets caught.
        pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
        text = pyproject.read_text(encoding="utf-8")
        assert 'license = { text = "Proprietary" }' in text, (
            "pyproject.toml no longer declares Proprietary licence; "
            "either restore the declaration or update README License"
        )

    def test_no_silent_open_source_license_in_readme(self) -> None:
        # 4-way absence pin against the most common open-source licence
        # names. A future PR that swaps the licence to MIT/Apache/GPL/BSD
        # without first updating ``pyproject.toml`` and PROGRESS.md
        # "Proposed scope changes" + ``MISSION.md`` would fail this
        # test. License flips are load-bearing legal scope changes;
        # this test forces them through the proper change process.
        body = _h2_section("License")
        for forbidden in ("MIT", "Apache", "GPL", "BSD"):
            assert forbidden not in body, (
                f"License section names {forbidden!r} but pyproject.toml "
                f"still declares Proprietary; either update pyproject + "
                f"MISSION + PROGRESS Proposed scope changes, or remove "
                f"the {forbidden!r} reference"
            )


class TestDeploySection:
    """P4 + DOC9 — pin the ``## Deployment`` section against drift.

    The section was a ``_TODO`` placeholder until the P4 slice landed
    ``Dockerfile`` + ``fly.toml`` and filled in the walk-through.  These
    tests guard the content against regressions (e.g. a future PR blanking
    the section) and against drift from the actual infra files (e.g. the
    release command changes in ``fly.toml`` but the README is not updated).
    """

    def test_section_is_filled(self) -> None:
        body = _h2_section("Deployment")
        assert "_TODO" not in body, "Deployment section still has _TODO placeholder"
        assert len(body.strip()) > 400, "Deployment section looks unsubstantial"

    def test_section_names_fly_io(self) -> None:
        body = _h2_section("Deployment")
        assert "Fly.io" in body

    def test_section_references_fly_toml(self) -> None:
        body = _h2_section("Deployment")
        assert "fly.toml" in body

    def test_section_references_secret_key(self) -> None:
        body = _h2_section("Deployment")
        assert "SECRET_KEY" in body

    def test_section_references_google_client_id(self) -> None:
        body = _h2_section("Deployment")
        assert "GOOGLE_CLIENT_ID" in body

    def test_section_names_postgres(self) -> None:
        body = _h2_section("Deployment")
        assert "Postgres" in body

    def test_section_names_alembic_migration_command(self) -> None:
        body = _h2_section("Deployment")
        assert "alembic upgrade head" in body

    def test_section_names_bootstrap_admin_email(self) -> None:
        body = _h2_section("Deployment")
        assert "BOOTSTRAP_ADMIN_EMAIL" in body


class TestDeployInfrastructure:
    """P4 — pin the existence and basic shape of deploy artefacts.

    These tests guard against accidental deletion of ``Dockerfile`` /
    ``fly.toml`` and against the release command drifting out of sync
    between ``fly.toml`` and the README's documented step.
    """

    def test_dockerfile_exists_at_repo_root(self) -> None:
        dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
        assert dockerfile.exists(), "Dockerfile missing at repo root — required for Fly.io deploy"

    def test_dockerfile_names_uvicorn(self) -> None:
        dockerfile = Path(__file__).resolve().parents[2] / "Dockerfile"
        text = dockerfile.read_text(encoding="utf-8")
        assert "uvicorn" in text, (
            "Dockerfile does not reference uvicorn; expected CMD to start uvicorn"
        )

    def test_fly_toml_exists_at_repo_root(self) -> None:
        fly_toml = Path(__file__).resolve().parents[2] / "fly.toml"
        assert fly_toml.exists(), "fly.toml missing at repo root — required for Fly.io deploy"

    def test_fly_toml_names_release_command(self) -> None:
        fly_toml = Path(__file__).resolve().parents[2] / "fly.toml"
        text = fly_toml.read_text(encoding="utf-8")
        assert "alembic upgrade head" in text, (
            "fly.toml does not set release_command to 'alembic upgrade head'; "
            "migrations would not run automatically on deploy"
        )
