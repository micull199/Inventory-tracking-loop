"""Seed demo data for local manual testing.

Idempotent: detects a prior run by the presence of supplier ``London Bullion Co``
and exits early unless ``--force`` is given. ``--force`` does NOT delete prior
demo rows; it just appends a fresh wave, so prefer wiping ``dev.db`` and
re-running migrations for a clean slate.

Usage::

    uv run python scripts/seed_demo_data.py
    uv run python scripts/seed_demo_data.py --force
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

# Allow `python scripts/seed_demo_data.py` from the repo root without -m.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.cost_engine import consume_fifo, record_receipt
from app.db import SessionLocal
from app.models import (
    CostLayerSource,
    FieldType,
    Item,
    ItemFieldValue,
    Location,
    MovementType,
    StockMovement,
    Supplier,
    TaxonomyFieldDef,
    TaxonomyNode,
    TrackingMode,
)

RNG = random.Random(20260508)
NOW = datetime.now(UTC)

SUPPLIERS: list[tuple[str, str, str]] = [
    ("London Bullion Co", "sales@londonbullion.example", "+44 20 7000 0001"),
    ("Diamond Imports Ltd", "orders@diaimports.example", "+44 20 7000 0002"),
    ("Jewellers Tools UK", "trade@jtools.example", "+44 161 555 0003"),
    ("Acme Findings", "hello@acmefindings.example", "+44 121 555 0004"),
]

LOCATIONS: list[tuple[str, str]] = [
    ("Workshop Bench", "Primary workbench for active jobs"),
    ("Safe — Vault", "Locked storage for high-value stock"),
    ("Display Case A", "Front-of-shop display"),
    ("Display Case B", "Window display"),
    ("Quarantine", "Items pending QC"),
]

TAXONOMY: dict[str, list[str]] = {
    "Precious Metals": ["Gold", "Silver", "Platinum"],
    "Gemstones": ["Diamonds", "Coloured Stones"],
    "Findings": ["Clasps", "Earring Hooks", "Chains"],
    "Tools": [],
    "Consumables": [],
}

# Field defs keyed by leaf path ("Parent/Child" or "Standalone").
FieldDefSpec = tuple[str, str, FieldType, list[str] | None, bool]
FIELD_DEFS: dict[str, list[FieldDefSpec]] = {
    "Precious Metals/Gold": [
        ("Purity", "purity", FieldType.SELECT, ["9ct", "14ct", "18ct", "22ct", "24ct"], True),
        ("Form", "form", FieldType.SELECT, ["wire", "sheet", "casting grain"], False),
    ],
    "Precious Metals/Silver": [
        ("Purity", "purity", FieldType.SELECT, ["925 sterling", "999 fine"], True),
        ("Form", "form", FieldType.SELECT, ["wire", "sheet", "casting grain"], False),
    ],
    "Gemstones/Diamonds": [
        ("Carat", "carat", FieldType.DECIMAL, None, True),
        ("Colour", "colour", FieldType.SELECT, ["D", "E", "F", "G", "H", "I", "J"], False),
        (
            "Clarity",
            "clarity",
            FieldType.SELECT,
            ["FL", "IF", "VVS1", "VVS2", "VS1", "VS2", "SI1"],
            False,
        ),
    ],
    "Tools": [
        ("Brand", "brand", FieldType.TEXT, None, False),
    ],
}


# (name, optional per-item field values dict)
ItemSpec = tuple[str, dict[str, object]]

ITEM_DEFS: dict[str, dict[str, object]] = {
    "Precious Metals/Gold": {
        "supplier": "London Bullion Co",
        "location": "Safe — Vault",
        "unit": "g",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("55.00"), Decimal("95.00")),
        "qty_in": (Decimal("20"), Decimal("250")),
        "reorder_threshold": Decimal("30"),
        "reorder_qty": Decimal("100"),
        "items": [
            ("18ct Yellow Gold Wire 1.0mm", {"purity": "18ct", "form": "wire"}),
            ("18ct White Gold Wire 1.0mm", {"purity": "18ct", "form": "wire"}),
            ("18ct Yellow Gold Sheet 0.8mm", {"purity": "18ct", "form": "sheet"}),
            ("14ct Yellow Gold Casting Grain", {"purity": "14ct", "form": "casting grain"}),
            ("9ct Yellow Gold Wire 0.8mm", {"purity": "9ct", "form": "wire"}),
            ("9ct White Gold Sheet 0.5mm", {"purity": "9ct", "form": "sheet"}),
            ("22ct Yellow Gold Wire 1.2mm", {"purity": "22ct", "form": "wire"}),
            ("24ct Gold Casting Grain", {"purity": "24ct", "form": "casting grain"}),
            ("18ct Rose Gold Wire 0.8mm", {"purity": "18ct", "form": "wire"}),
            ("14ct Rose Gold Sheet 0.6mm", {"purity": "14ct", "form": "sheet"}),
        ],
    },
    "Precious Metals/Silver": {
        "supplier": "London Bullion Co",
        "location": "Safe — Vault",
        "unit": "g",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("0.85"), Decimal("1.40")),
        "qty_in": (Decimal("100"), Decimal("1500")),
        "reorder_threshold": Decimal("200"),
        "reorder_qty": Decimal("500"),
        "items": [
            ("925 Silver Wire 0.6mm", {"purity": "925 sterling", "form": "wire"}),
            ("925 Silver Wire 1.0mm", {"purity": "925 sterling", "form": "wire"}),
            ("925 Silver Wire 1.5mm", {"purity": "925 sterling", "form": "wire"}),
            ("925 Silver Sheet 0.5mm", {"purity": "925 sterling", "form": "sheet"}),
            ("925 Silver Sheet 0.8mm", {"purity": "925 sterling", "form": "sheet"}),
            ("925 Silver Sheet 1.0mm", {"purity": "925 sterling", "form": "sheet"}),
            ("925 Silver Casting Grain", {"purity": "925 sterling", "form": "casting grain"}),
            ("999 Fine Silver Sheet 0.5mm", {"purity": "999 fine", "form": "sheet"}),
            ("999 Fine Silver Wire 1.0mm", {"purity": "999 fine", "form": "wire"}),
            ("999 Fine Silver Casting Grain", {"purity": "999 fine", "form": "casting grain"}),
        ],
    },
    "Precious Metals/Platinum": {
        "supplier": "London Bullion Co",
        "location": "Safe — Vault",
        "unit": "g",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("30.00"), Decimal("48.00")),
        "qty_in": (Decimal("5"), Decimal("60")),
        "reorder_threshold": Decimal("10"),
        "reorder_qty": Decimal("25"),
        "items": [
            ("950 Platinum Wire 0.8mm", {}),
            ("950 Platinum Wire 1.0mm", {}),
            ("950 Platinum Sheet 0.5mm", {}),
            ("950 Platinum Sheet 1.0mm", {}),
            ("950 Platinum Casting Grain", {}),
            ("Pt/Ir 90/10 Wire 0.8mm", {}),
        ],
    },
    "Gemstones/Diamonds": {
        "supplier": "Diamond Imports Ltd",
        "location": "Safe — Vault",
        "unit": "ea",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("120.00"), Decimal("1800.00")),
        "qty_in": (Decimal("1"), Decimal("18")),
        "reorder_threshold": Decimal("2"),
        "reorder_qty": Decimal("5"),
        "items": [
            ("Round Brilliant 2.0mm", {"carat": Decimal("0.03"), "colour": "G", "clarity": "VS1"}),
            ("Round Brilliant 2.5mm", {"carat": Decimal("0.06"), "colour": "F", "clarity": "VS2"}),
            ("Round Brilliant 3.0mm", {"carat": Decimal("0.10"), "colour": "G", "clarity": "VS1"}),
            ("Round Brilliant 3.5mm", {"carat": Decimal("0.16"), "colour": "H", "clarity": "SI1"}),
            ("Round Brilliant 4.0mm", {"carat": Decimal("0.25"), "colour": "F", "clarity": "VVS2"}),
            ("Round Brilliant 5.0mm", {"carat": Decimal("0.50"), "colour": "E", "clarity": "VVS1"}),
            ("Princess 3.0mm", {"carat": Decimal("0.12"), "colour": "G", "clarity": "VS2"}),
            ("Princess 4.0mm", {"carat": Decimal("0.30"), "colour": "F", "clarity": "VS1"}),
            ("Oval 4x3mm", {"carat": Decimal("0.18"), "colour": "G", "clarity": "VS2"}),
            ("Marquise 5x2.5mm", {"carat": Decimal("0.15"), "colour": "H", "clarity": "SI1"}),
            ("Pear 5x3mm", {"carat": Decimal("0.20"), "colour": "F", "clarity": "VS2"}),
            ("Emerald Cut 5x3mm", {"carat": Decimal("0.30"), "colour": "E", "clarity": "VVS2"}),
        ],
    },
    "Gemstones/Coloured Stones": {
        "supplier": "Diamond Imports Ltd",
        "location": "Safe — Vault",
        "unit": "ea",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("8.00"), Decimal("450.00")),
        "qty_in": (Decimal("2"), Decimal("30")),
        "reorder_threshold": Decimal("3"),
        "reorder_qty": Decimal("10"),
        "items": [
            ("Ruby Round 3mm", {}),
            ("Ruby Oval 5x3mm", {}),
            ("Blue Sapphire Round 3mm", {}),
            ("Blue Sapphire Oval 6x4mm", {}),
            ("Pink Sapphire Round 3mm", {}),
            ("Emerald Round 3mm", {}),
            ("Emerald Octagon 6x4mm", {}),
            ("Tanzanite Oval 6x4mm", {}),
            ("Aquamarine Round 4mm", {}),
            ("Amethyst Oval 8x6mm", {}),
        ],
    },
    "Findings/Clasps": {
        "supplier": "Acme Findings",
        "location": "Workshop Bench",
        "unit": "ea",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("1.20"), Decimal("18.00")),
        "qty_in": (Decimal("20"), Decimal("200")),
        "reorder_threshold": Decimal("25"),
        "reorder_qty": Decimal("100"),
        "items": [
            ("Silver Lobster Clasp 10mm", {}),
            ("Silver Lobster Clasp 12mm", {}),
            ("Silver Bolt Ring 6mm", {}),
            ("Silver Bolt Ring 8mm", {}),
            ("9ct Gold Lobster Clasp 10mm", {}),
            ("18ct Gold Lobster Clasp 12mm", {}),
            ("Silver Magnetic Clasp", {}),
            ("Silver Toggle Clasp 15mm", {}),
        ],
    },
    "Findings/Earring Hooks": {
        "supplier": "Acme Findings",
        "location": "Workshop Bench",
        "unit": "pair",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("0.80"), Decimal("12.00")),
        "qty_in": (Decimal("25"), Decimal("250")),
        "reorder_threshold": Decimal("30"),
        "reorder_qty": Decimal("100"),
        "items": [
            ("Silver French Hook", {}),
            ("Silver Lever Back", {}),
            ("Silver Hoop 12mm", {}),
            ("Silver Hoop 18mm", {}),
            ("9ct Gold French Hook", {}),
            ("18ct Gold Lever Back", {}),
        ],
    },
    "Findings/Chains": {
        "supplier": "Acme Findings",
        "location": "Workshop Bench",
        "unit": "m",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("3.00"), Decimal("65.00")),
        "qty_in": (Decimal("5"), Decimal("60")),
        "reorder_threshold": Decimal("5"),
        "reorder_qty": Decimal("20"),
        "items": [
            ("Silver Curb Chain 1.5mm", {}),
            ("Silver Curb Chain 2.0mm", {}),
            ("Silver Belcher Chain 2.0mm", {}),
            ("Silver Snake Chain 1.0mm", {}),
            ("Silver Box Chain 1.2mm", {}),
            ("9ct Gold Curb Chain 1.5mm", {}),
            ("18ct Gold Belcher Chain 1.8mm", {}),
            ("18ct Gold Box Chain 1.0mm", {}),
        ],
    },
    "Tools": {
        "supplier": "Jewellers Tools UK",
        "location": "Workshop Bench",
        "unit": "ea",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": True,
        "unit_cost": (Decimal("8.00"), Decimal("180.00")),
        "qty_in": (Decimal("1"), Decimal("8")),
        "reorder_threshold": Decimal("1"),
        "reorder_qty": Decimal("2"),
        "items": [
            ("Bench Pin & Anvil", {"brand": "Eurotool"}),
            ("Ring Mandrel — Steel", {"brand": "Pepe Tools"}),
            ("Jeweller's Saw Frame 3in", {"brand": "Knew Concepts"}),
            ("Half-round Pliers", {"brand": "Lindstrom"}),
            ("Chain-nose Pliers", {"brand": "Lindstrom"}),
            ("Flush Cutters", {"brand": "Lindstrom"}),
            ("Bezel Roller", {"brand": "Fretz"}),
            ("Mallet — Rawhide", {"brand": "Eurotool"}),
        ],
    },
    "Consumables": {
        "supplier": "Jewellers Tools UK",
        "location": "Workshop Bench",
        "unit": "ea",
        "tracking_mode": TrackingMode.QTY,
        "requires_checkout": False,
        "unit_cost": (Decimal("2.00"), Decimal("45.00")),
        "qty_in": (Decimal("5"), Decimal("80")),
        "reorder_threshold": Decimal("10"),
        "reorder_qty": Decimal("25"),
        "items": [
            ("Saw Blades #2/0 (gross)", {}),
            ("Saw Blades #4/0 (gross)", {}),
            ("Polishing Compound — Tripoli", {}),
            ("Polishing Compound — Rouge", {}),
            ("Pickle — Sparex (1kg)", {}),
            ("Boric Acid (500g)", {}),
            ("Easy Silver Solder (1ft strip)", {}),
            ("Hard Silver Solder (1ft strip)", {}),
        ],
    },
}


def _sku_prefix(path: str) -> str:
    """Build a short SKU prefix from a leaf path, e.g. ``Precious Metals/Gold`` -> ``PM-GO``."""
    parts = path.split("/")
    if len(parts) == 2:
        return "-".join(p[:2].upper().replace(" ", "") for p in parts)
    return parts[0][:3].upper().replace(" ", "")


def _round_money(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.01"))


def _round_qty(d: Decimal) -> Decimal:
    return d.quantize(Decimal("0.0001"))


def _already_seeded(db) -> bool:  # type: ignore[no-untyped-def]
    return (
        db.execute(
            select(Supplier.id).where(Supplier.name == SUPPLIERS[0][0])
        ).first()
        is not None
    )


def seed() -> dict[str, int]:
    counts = {"suppliers": 0, "locations": 0, "nodes": 0, "field_defs": 0, "items": 0, "movements": 0}
    with SessionLocal() as db:
        if _already_seeded(db) and "--force" not in sys.argv:
            print("Demo data already present (supplier 'London Bullion Co' exists).")
            print("Re-run with --force to append another wave (not idempotent).")
            return counts

        # Suppliers
        suppliers: dict[str, Supplier] = {}
        for name, email, phone in SUPPLIERS:
            existing = db.execute(select(Supplier).where(Supplier.name == name)).scalar_one_or_none()
            if existing is not None:
                suppliers[name] = existing
                continue
            sup = Supplier(name=name, email=email, phone=phone)
            db.add(sup)
            db.flush()
            suppliers[name] = sup
            counts["suppliers"] += 1

        # Locations
        locations: dict[str, Location] = {}
        for name, notes in LOCATIONS:
            existing = db.execute(select(Location).where(Location.name == name)).scalar_one_or_none()
            if existing is not None:
                locations[name] = existing
                continue
            loc = Location(name=name, notes=notes)
            db.add(loc)
            db.flush()
            locations[name] = loc
            counts["locations"] += 1

        # Taxonomy: top-level + children. Reuse existing nodes by (parent_id, name).
        nodes_by_path: dict[str, TaxonomyNode] = {}
        for parent_name, children in TAXONOMY.items():
            parent = db.execute(
                select(TaxonomyNode).where(
                    TaxonomyNode.parent_id.is_(None),
                    TaxonomyNode.name == parent_name,
                )
            ).scalar_one_or_none()
            if parent is None:
                parent = TaxonomyNode(name=parent_name, parent_id=None, sort_order=0)
                db.add(parent)
                db.flush()
                counts["nodes"] += 1
            nodes_by_path[parent_name] = parent
            for idx, child_name in enumerate(children):
                child = db.execute(
                    select(TaxonomyNode).where(
                        TaxonomyNode.parent_id == parent.id,
                        TaxonomyNode.name == child_name,
                    )
                ).scalar_one_or_none()
                if child is None:
                    child = TaxonomyNode(
                        name=child_name, parent_id=parent.id, sort_order=idx
                    )
                    db.add(child)
                    db.flush()
                    counts["nodes"] += 1
                nodes_by_path[f"{parent_name}/{child_name}"] = child

        # Field defs (attached to leaves). Reuse by (node_id, key).
        field_defs_by_leaf: dict[str, list[TaxonomyFieldDef]] = {}
        for leaf_path, specs in FIELD_DEFS.items():
            node = nodes_by_path[leaf_path]
            defs: list[TaxonomyFieldDef] = []
            for idx, (fname, fkey, ftype, options, required) in enumerate(specs):
                existing = db.execute(
                    select(TaxonomyFieldDef).where(
                        TaxonomyFieldDef.node_id == node.id,
                        TaxonomyFieldDef.key == fkey,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    defs.append(existing)
                    continue
                fd = TaxonomyFieldDef(
                    node_id=node.id,
                    name=fname,
                    key=fkey,
                    type=ftype,
                    options_json=options,
                    required=required,
                    sort_order=idx,
                )
                db.add(fd)
                db.flush()
                defs.append(fd)
                counts["field_defs"] += 1
            field_defs_by_leaf[leaf_path] = defs

        # defaults_json on each leaf so the create-item form pre-fills
        for leaf_path, cfg in ITEM_DEFS.items():
            node = nodes_by_path[leaf_path]
            node.defaults_json = {
                "unit": cfg["unit"],
                "tracking_mode": cfg["tracking_mode"].value
                if isinstance(cfg["tracking_mode"], TrackingMode)
                else cfg["tracking_mode"],
                "requires_checkout": bool(cfg["requires_checkout"]),
                "reorder_threshold": str(cfg["reorder_threshold"]),
                "reorder_qty": str(cfg["reorder_qty"]),
                "supplier_id": suppliers[cfg["supplier"]].id,  # type: ignore[index]
                "location_id": locations[cfg["location"]].id,  # type: ignore[index]
            }
        db.flush()

        # Items + initial receipt
        consume_pool: list[Item] = []
        for leaf_path, cfg in ITEM_DEFS.items():
            node = nodes_by_path[leaf_path]
            prefix = _sku_prefix(leaf_path)
            unit_cost_lo, unit_cost_hi = cfg["unit_cost"]  # type: ignore[misc]
            qty_lo, qty_hi = cfg["qty_in"]  # type: ignore[misc]
            for idx, (item_name, field_values) in enumerate(cfg["items"], start=1):  # type: ignore[arg-type]
                sku = f"{prefix}-{idx:03d}"
                if db.execute(select(Item.id).where(Item.sku == sku)).first() is not None:
                    continue
                item = Item(
                    sku=sku,
                    name=item_name,
                    taxonomy_node_id=node.id,
                    unit=cfg["unit"],  # type: ignore[arg-type]
                    tracking_mode=cfg["tracking_mode"],  # type: ignore[arg-type]
                    requires_checkout=bool(cfg["requires_checkout"]),
                    reorder_threshold=cfg["reorder_threshold"],  # type: ignore[arg-type]
                    reorder_qty=cfg["reorder_qty"],  # type: ignore[arg-type]
                    supplier_id=suppliers[cfg["supplier"]].id,  # type: ignore[index]
                    location_id=locations[cfg["location"]].id,  # type: ignore[index]
                )
                db.add(item)
                db.flush()
                counts["items"] += 1

                # Field values for leaves with field defs
                for fd in field_defs_by_leaf.get(leaf_path, []):
                    raw = field_values.get(fd.key)
                    if raw is None:
                        continue
                    fv = ItemFieldValue(item_id=item.id, field_def_id=fd.id)
                    if fd.type is FieldType.DECIMAL and isinstance(raw, Decimal):
                        fv.value_decimal = raw
                    elif fd.type is FieldType.NUMBER and isinstance(raw, int):
                        fv.value_number = raw
                    elif fd.type is FieldType.BOOLEAN and isinstance(raw, bool):
                        fv.value_bool = raw
                    elif fd.type is FieldType.MULTISELECT and isinstance(raw, list):
                        fv.value_json = raw
                    else:
                        fv.value_text = str(raw)
                    db.add(fv)

                # Initial receipt
                qty_received = _round_qty(
                    Decimal(str(RNG.uniform(float(qty_lo), float(qty_hi))))
                )
                unit_cost = _round_money(
                    Decimal(str(RNG.uniform(float(unit_cost_lo), float(unit_cost_hi))))
                )
                received_at = NOW - timedelta(days=RNG.randint(7, 90))
                movement = StockMovement(
                    item_id=item.id,
                    type=MovementType.IN,
                    qty=qty_received,
                    reason="seed: initial receipt",
                    user_id=None,
                    created_at=received_at,
                )
                db.add(movement)
                db.flush()
                record_receipt(
                    db,
                    item=item,
                    qty=qty_received,
                    unit_cost=unit_cost,
                    source=CostLayerSource.MANUAL_IN,
                    movement=movement,
                    received_at=received_at,
                )
                counts["movements"] += 1

                if RNG.random() < 0.30:
                    consume_pool.append(item)

        # A scattering of consumes so the dashboard / movements page show outs
        for item in consume_pool:
            available = item.current_qty
            if available <= 0:
                continue
            max_take = available * Decimal("0.4")
            if max_take <= 0:
                continue
            take = _round_qty(
                Decimal(str(RNG.uniform(float(max_take * Decimal("0.2")), float(max_take))))
            )
            if take <= 0:
                continue
            movement = StockMovement(
                item_id=item.id,
                type=MovementType.OUT,
                qty=take,
                reason="seed: workshop consumption",
                user_id=None,
                created_at=NOW - timedelta(days=RNG.randint(0, 6)),
            )
            db.add(movement)
            db.flush()
            consume_fifo(db, item=item, qty=take, movement=movement)
            counts["movements"] += 1

        db.commit()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Append demo data even if sentinel supplier already exists",
    )
    parser.parse_args()
    counts = seed()
    print("Seed complete:")
    for k, v in counts.items():
        print(f"  {k:>11}: {v}")


if __name__ == "__main__":
    main()
