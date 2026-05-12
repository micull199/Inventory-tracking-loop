"""Integration tests for the 0016 taxonomy archetype+prefix migration.

These tests drive Alembic directly via ``alembic.command.upgrade`` /
``downgrade`` against a temp file-backed SQLite database. File-backed
(not in-memory) so the migration's separate connections see the same
schema — Alembic's online mode opens a fresh connection per command, so
``:memory:`` would yield an empty DB on the second call.

``migrations/env.py`` overrides the URL from ``app.config.settings`` at
module import. To redirect the migration at our temp DB we have to:

1. Set ``DATABASE_URL`` env var to the temp path, and
2. Re-import / patch ``app.config.settings`` so the env-driven value wins.

The test handles this by monkeypatching ``settings.database_url`` for the
duration of each migration call.

What this exercises:

1. Fresh DB: full upgrade to head succeeds; the new partial unique index
   ``uq_taxonomy_sku_prefix_top`` rejects a duplicate top-level prefix.
2. Pre-seeded DB (upgrade only to 0015, insert raw rows, then upgrade to
   0016): the backfill assigns sensible prefixes, archetype=``bulk`` at
   depth 0, NULL at depth 1, and ``next_sequence`` is bumped past the max
   numeric suffix of existing items on the leaf.
3. Downgrade: the three new ``taxonomy_nodes`` columns and the
   ``items.assigned_sequence`` column disappear.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.config import settings


def _make_alembic_config(db_path: Path) -> Config:
    """Build an Alembic ``Config`` pointed at a file-backed SQLite DB.

    Uses the project's existing ``alembic.ini`` for ``script_location`` but
    overrides the URL so the test doesn't touch ``dev.db``.

    The ``settings.database_url`` patch is what actually takes effect
    inside ``migrations/env.py`` (it overrides the ini value); the
    ini-level set here is belt-and-braces.
    """

    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(autouse=True)
def _patch_settings_url(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``app.config.settings.database_url`` at the temp DB.

    ``migrations/env.py`` reads from ``settings.database_url`` to build the
    engine. Without this patch the migration would target the real
    ``dev.db`` from settings rather than the per-test temp file.
    """

    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "test_migration_0016.sqlite"
    yield path
    if path.exists():
        path.unlink()


class TestFreshDBUpgrade:
    def test_partial_unique_index_rejects_duplicate_top_level_prefix(self, db_path: Path) -> None:
        # Upgrade through 0016 on a fresh DB.
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "head")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            # Insert a top-level row with a known prefix.
            conn.execute(
                sa.text(
                    "INSERT INTO taxonomy_nodes (id, parent_id, name, sku_prefix, "
                    "archetype, sort_order, next_sequence) "
                    "VALUES (1, NULL, 'Test One', 'TST', 'bulk', 0, 1)"
                )
            )
            conn.commit()

            # Inserting a second top-level with the same prefix should fail.
            duplicate_insert = sa.text(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, "
                "sku_prefix, archetype, sort_order, next_sequence) "
                "VALUES (2, NULL, 'Test Two', 'TST', 'bulk', 0, 1)"
            )
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(duplicate_insert)

    def test_partial_unique_index_allows_same_prefix_under_different_parents(
        self, db_path: Path
    ) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "head")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            # Two top-levels, each with the same child prefix ``SUB`` — this
            # is allowed because the child index is scoped per-parent.
            conn.execute(
                sa.text(
                    "INSERT INTO taxonomy_nodes (id, parent_id, name, sku_prefix, "
                    "archetype, sort_order, next_sequence) "
                    "VALUES (1, NULL, 'Alpha', 'ALP', 'bulk', 0, 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO taxonomy_nodes (id, parent_id, name, sku_prefix, "
                    "archetype, sort_order, next_sequence) "
                    "VALUES (2, NULL, 'Beta', 'BET', 'bulk', 0, 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO taxonomy_nodes (id, parent_id, name, sku_prefix, "
                    "archetype, sort_order, next_sequence) "
                    "VALUES (3, 1, 'Alpha Sub', 'SUB', NULL, 0, 1)"
                )
            )
            conn.execute(
                sa.text(
                    "INSERT INTO taxonomy_nodes (id, parent_id, name, sku_prefix, "
                    "archetype, sort_order, next_sequence) "
                    "VALUES (4, 2, 'Beta Sub', 'SUB', NULL, 0, 1)"
                )
            )
            conn.commit()

            # Duplicate child prefix under the *same* parent must fail.
            duplicate_child = sa.text(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, "
                "sku_prefix, archetype, sort_order, next_sequence) "
                "VALUES (5, 1, 'Another Sub', 'SUB', NULL, 0, 1)"
            )
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(duplicate_child)


class TestSeededUpgrade:
    def test_backfill_prefix_archetype_and_sequence(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        # First: upgrade only as far as 0015 (the pre-0016 schema).
        command.upgrade(cfg, "0015_taxonomy_defaults_json")

        # Seed: 2 top-level rows + 2 children + items with SKUs RAW-0001 and
        # RAW-0002 sitting on the *Raw Materials* top-level leaf id=1.
        # (Items on the leaf whose prefix backfills to ``RAW`` so the
        # ``next_sequence`` backfill picks up the integers.)
        # Use a direct sqlite3 connection so we control the schema-level
        # writes without involving SQLAlchemy's row-level checks.
        with sqlite3.connect(db_path) as raw:
            raw.execute(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order) "
                "VALUES (1, NULL, 'Raw Materials', 0)"
            )
            raw.execute(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order) "
                "VALUES (2, NULL, 'Tools', 10)"
            )
            raw.execute(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order) "
                "VALUES (3, 1, 'Silver', 0)"
            )
            raw.execute(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order) "
                "VALUES (4, 1, 'Gold', 10)"
            )
            # Both items live on the Raw Materials top-level leaf — SKU
            # prefix matches the top-level's would-be backfilled prefix.
            raw.execute(
                "INSERT INTO items (id, sku, name, taxonomy_node_id, unit, "
                "tracking_mode, requires_checkout, current_qty, "
                "reorder_threshold, reorder_qty) "
                "VALUES (1, 'RAW-0001', 'Bench Pin', 1, 'ea', 'qty', 0, 0, 0, 0)"
            )
            raw.execute(
                "INSERT INTO items (id, sku, name, taxonomy_node_id, unit, "
                "tracking_mode, requires_checkout, current_qty, "
                "reorder_threshold, reorder_qty) "
                "VALUES (2, 'RAW-0002', 'Other Item', 1, 'ea', 'qty', 0, 0, 0, 0)"
            )
            raw.commit()

        # Now apply 0016.
        command.upgrade(cfg, "0016_taxonomy_archetype_and_prefix")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, parent_id, name, sku_prefix, archetype, "
                    "next_sequence FROM taxonomy_nodes ORDER BY id"
                )
            ).fetchall()
            by_id = {r[0]: r for r in rows}

            # All four rows have prefixes assigned.
            for r in rows:
                assert r[3] is not None, f"sku_prefix missing on {r}"
                assert r[3].isupper() or r[3].isalnum()

            # Top-level rows get archetype='bulk'; depth-1 rows stay NULL.
            assert by_id[1][4] == "bulk"
            assert by_id[2][4] == "bulk"
            assert by_id[3][4] is None
            assert by_id[4][4] is None

            # Raw Materials backfills to ``RAW``; next_sequence picks up the
            # max integer suffix of its two items + 1 == 3.
            assert by_id[1][3] == "RAW"
            assert by_id[1][5] == 3

            # Tools backfills to ``TOO``; no items, so the server default 1
            # sticks.
            assert by_id[2][3] == "TOO"
            assert by_id[2][5] == 1

            # The two children get derived alpha prefixes.
            assert by_id[3][3] == "SIL"
            assert by_id[4][3] == "GOL"

            # items.assigned_sequence column exists and is NULL on existing rows.
            items = conn.execute(
                sa.text("SELECT id, assigned_sequence FROM items ORDER BY id")
            ).fetchall()
            assert items == [(1, None), (2, None)]

    def test_sibling_prefix_disambiguation(self, db_path: Path) -> None:
        # Two top-levels whose names collapse to the same alpha-only
        # candidate (``RAW``) must end up with distinct prefixes (``RAW`` +
        # ``RAW2``).
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0015_taxonomy_defaults_json")
        with sqlite3.connect(db_path) as raw:
            raw.execute(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order) "
                "VALUES (1, NULL, 'Raw Materials', 0)"
            )
            raw.execute(
                "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order) "
                "VALUES (2, NULL, 'Raw Castings', 10)"
            )
            raw.commit()

        command.upgrade(cfg, "0016_taxonomy_archetype_and_prefix")
        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            prefixes = {
                row[0]: row[1]
                for row in conn.execute(
                    sa.text("SELECT id, sku_prefix FROM taxonomy_nodes ORDER BY id")
                ).fetchall()
            }
            assert prefixes[1] == "RAW"
            assert prefixes[2] == "RAW2"


class TestDowngrade:
    def test_downgrade_removes_new_columns(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        # Upgrade only to 0016 so a downgrade-by-one returns to 0015.
        # ``head`` now includes 0017 (field_visibility_json) and downgrading
        # that revision would leave 0016's columns intact.
        command.upgrade(cfg, "0016_taxonomy_archetype_and_prefix")
        command.downgrade(cfg, "-1")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            inspector = sa.inspect(conn)
            taxonomy_cols = {c["name"] for c in inspector.get_columns("taxonomy_nodes")}
            assert "archetype" not in taxonomy_cols
            assert "sku_prefix" not in taxonomy_cols
            assert "next_sequence" not in taxonomy_cols

            item_cols = {c["name"] for c in inspector.get_columns("items")}
            assert "assigned_sequence" not in item_cols

            # The pair of partial unique indexes should also be gone.
            index_names = {ix["name"] for ix in inspector.get_indexes("taxonomy_nodes")}
            assert "uq_taxonomy_sku_prefix_top" not in index_names
            assert "uq_taxonomy_sku_prefix_child" not in index_names
