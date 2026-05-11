"""PDF rendering for purchase orders (PO3).

A pure helper that takes already-resolved view data (the same shape the
detail-page route builds) and returns PDF bytes via reportlab.

Why reportlab and not WeasyPrint: MISSION §5 lists both. WeasyPrint needs
native libs (libgobject-2.0-0 / pango / cairo) that aren't installed on the
loop / dev environment, and the loop runner can't install brew packages.
Reportlab is pure-Python — `uv add reportlab` is enough. The tradeoff is
that the layout is built imperatively rather than templated; that's
acceptable here because a PO PDF is closer to a printed form than a copy of
the on-screen detail view, and the surface is small.

Compression is disabled (`canvas.setPageCompression(0)`). The PDF stream
holds the supplier name, SKUs, line cells, etc. as ASCII so integration
tests can byte-search the output without a PDF parser dependency. POs are
small (single page in v1) so the size delta is negligible. If a future
polish pass re-enables compression the byte-search tests will need to
switch to a proper extractor (pypdf or similar).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from io import BytesIO
from typing import Any

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas


def _fmt_decimal(value: Decimal | None) -> str:
    """Render a Decimal as a plain string, or ``—`` for None."""
    if value is None:
        return "—"
    return str(value)


def _fmt_date(value: date | None) -> str:
    if value is None:
        return "—"
    return value.isoformat()


def _line_total(qty: Decimal, unit_cost: Decimal | None) -> Decimal | None:
    if unit_cost is None:
        return None
    return qty * unit_cost


def render_po_pdf(
    *,
    po: dict[str, Any],
    supplier: dict[str, Any],
    lines: list[dict[str, Any]],
) -> bytes:
    """Render a PO as PDF bytes.

    ``po`` keys: ``id``, ``status`` (str), ``created_at`` (datetime),
    ``expected_date`` (date | None), ``notes`` (str | None).
    ``supplier`` keys: ``name`` (str), ``archived`` (bool).
    ``lines``: list of dicts with ``sku``, ``name``, ``unit``,
    ``qty_ordered`` (Decimal), ``expected_unit_cost`` (Decimal | None).
    """
    buf = BytesIO()
    canv = Canvas(buf, pagesize=A4)
    # Make the stream byte-searchable so integration tests don't need a PDF
    # parser dependency. See module docstring for the tradeoff.
    canv.setPageCompression(0)

    page_width, page_height = A4
    left = 20 * mm
    right = page_width - 20 * mm
    y = page_height - 25 * mm

    # Title
    canv.setFont("Helvetica-Bold", 16)
    canv.drawString(left, y, f"Purchase Order #{po['id']}")
    canv.setFont("Helvetica", 10)
    canv.drawRightString(right, y, f"Status: {po['status']}")
    y -= 12 * mm

    # Supplier block (left) + meta block (right).
    canv.setFont("Helvetica-Bold", 11)
    canv.drawString(left, y, "Supplier")
    canv.setFont("Helvetica", 10)
    supplier_label = supplier["name"]
    if supplier.get("archived"):
        supplier_label = f"{supplier_label} (archived)"
    canv.drawString(left, y - 5 * mm, supplier_label)

    canv.setFont("Helvetica-Bold", 11)
    canv.drawString(left + 90 * mm, y, "Details")
    canv.setFont("Helvetica", 10)
    created_at = po["created_at"]
    created_iso = created_at.date().isoformat() if hasattr(created_at, "date") else str(created_at)
    canv.drawString(left + 90 * mm, y - 5 * mm, f"Created: {created_iso}")
    canv.drawString(
        left + 90 * mm,
        y - 10 * mm,
        f"Expected: {_fmt_date(po.get('expected_date'))}",
    )

    y -= 18 * mm
    if po.get("notes"):
        canv.setFont("Helvetica-Bold", 11)
        canv.drawString(left, y, "Notes")
        canv.setFont("Helvetica", 10)
        canv.drawString(left, y - 5 * mm, str(po["notes"]))
        y -= 12 * mm

    # Lines table
    y -= 4 * mm
    headers = [
        ("SKU", left),
        ("Name", left + 35 * mm),
        ("Unit", left + 95 * mm),
        ("Qty ordered", left + 115 * mm),
        ("Unit cost", left + 145 * mm),
        ("Line total", right),
    ]
    canv.setFont("Helvetica-Bold", 10)
    for label, x in headers[:-1]:
        canv.drawString(x, y, label)
    canv.drawRightString(headers[-1][1], y, headers[-1][0])
    y -= 2 * mm
    canv.line(left, y, right, y)
    y -= 6 * mm

    canv.setFont("Helvetica", 10)
    grand_total = Decimal("0")
    has_priced_line = False
    for line in lines:
        qty = line["qty_ordered"]
        cost = line.get("expected_unit_cost")
        total = _line_total(qty, cost)
        if total is not None:
            grand_total += total
            has_priced_line = True

        canv.drawString(left, y, str(line["sku"]))
        canv.drawString(left + 35 * mm, y, str(line["name"])[:30])
        canv.drawString(left + 95 * mm, y, str(line["unit"]))
        canv.drawString(left + 115 * mm, y, _fmt_decimal(qty))
        canv.drawString(left + 145 * mm, y, _fmt_decimal(cost))
        canv.drawRightString(right, y, _fmt_decimal(total))
        y -= 6 * mm

    y -= 2 * mm
    canv.line(left, y, right, y)
    y -= 6 * mm
    canv.setFont("Helvetica-Bold", 11)
    if has_priced_line:
        canv.drawRightString(right, y, f"Total: {grand_total}")
    else:
        canv.drawRightString(right, y, "Total: —")

    canv.showPage()
    canv.save()
    return buf.getvalue()
