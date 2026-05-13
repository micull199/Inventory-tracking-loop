"""Integration tests for migration 0022 (catalog_key backfill).

Seeds a DB upgraded only to 0021 (the column-add migration), inserts
``TaxonomyFieldDef`` rows that should match by ``key``, by ``name``, or
by neither, then runs ``alembic upgrade 0022_backfill_catalog_key`` and
asserts the right backfill + archive + audit behaviour.

Pattern matches ``test_migration_0016.py``: temp file-backed SQLite +
``settings.database_url`` patched for the duration of the test.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from app.config import settings


def _make_alembic_config(db_path: Path) -> Config:
    repo_root = Path(__file__).resolve().parents[2]
    cfg = Config(str(repo_root / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


@pytest.fixture(autouse=True)
def _patch_settings_url(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_path}")


@pytest.fixture
def db_path(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "test_migration_0022.sqlite"
    yield path
    if path.exists():
        path.unlink()


def _seed_node(raw: sqlite3.Connection, *, node_id: int, name: str) -> None:
    raw.execute(
        "INSERT INTO taxonomy_nodes (id, parent_id, name, sort_order, "
        "sku_prefix, archetype, next_sequence) "
        "VALUES (?, NULL, ?, 0, ?, 'bulk', 1)",
        (node_id, name, name[:3].upper()),
    )


def _seed_field_def(
    raw: sqlite3.Connection,
    *,
    fd_id: int,
    node_id: int,
    name: str,
    key: str,
    field_type: str = "text",
    options_json: str | None = None,
) -> None:
    raw.execute(
        "INSERT INTO taxonomy_field_defs (id, node_id, name, key, type, "
        "options_json, required, sort_order) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, 0)",
        (fd_id, node_id, name, key, field_type, options_json),
    )


class TestBackfillHappyPath:
    def test_match_by_key_sets_catalog_key(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0021_taxonomy_field_def_catalog_key")

        with sqlite3.connect(db_path) as raw:
            _seed_node(raw, node_id=1, name="Rings")
            _seed_field_def(
                raw,
                fd_id=1,
                node_id=1,
                name="Anything Goes",
                key="karat",  # matches catalog by key
                field_type="select",
                options_json='["9ct", "18ct"]',
            )
            raw.commit()

        command.upgrade(cfg, "0022_backfill_catalog_key")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT catalog_key, archived_at FROM taxonomy_field_defs WHERE id=1")
            ).first()
            assert row is not None
            assert row[0] == "karat"
            assert row[1] is None

    def test_match_by_label_sets_catalog_key(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0021_taxonomy_field_def_catalog_key")

        with sqlite3.connect(db_path) as raw:
            _seed_node(raw, node_id=1, name="Rings")
            _seed_field_def(
                raw,
                fd_id=1,
                node_id=1,
                name="Material",  # matches catalog label "Material"
                key="some_weird_key",
                field_type="select",
                options_json='["Silver", "Gold"]',
            )
            raw.commit()

        command.upgrade(cfg, "0022_backfill_catalog_key")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT catalog_key, archived_at FROM taxonomy_field_defs WHERE id=1")
            ).first()
            assert row is not None
            assert row[0] == "material"
            assert row[1] is None

    def test_case_insensitive_label_match(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0021_taxonomy_field_def_catalog_key")

        with sqlite3.connect(db_path) as raw:
            _seed_node(raw, node_id=1, name="Rings")
            _seed_field_def(
                raw,
                fd_id=1,
                node_id=1,
                name="KARAT",  # uppercase — should match
                key="kk",
                field_type="select",
                options_json='["9ct"]',
            )
            raw.commit()

        command.upgrade(cfg, "0022_backfill_catalog_key")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT catalog_key FROM taxonomy_field_defs WHERE id=1")
            ).first()
            assert row is not None
            assert row[0] == "karat"


class TestBackfillUnmappable:
    def test_no_match_archives_row_and_writes_audit(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0021_taxonomy_field_def_catalog_key")

        with sqlite3.connect(db_path) as raw:
            _seed_node(raw, node_id=1, name="Rings")
            _seed_field_def(
                raw,
                fd_id=1,
                node_id=1,
                name="Bespoke Whatsit",
                key="bespoke_whatsit",
            )
            raw.commit()

        command.upgrade(cfg, "0022_backfill_catalog_key")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text(
                    "SELECT catalog_key, archived_at, name, key "
                    "FROM taxonomy_field_defs WHERE id=1"
                )
            ).first()
            assert row is not None
            # Catalog key still NULL — slice 6's NOT NULL constraint will
            # be tightened only after another sweep that drops these rows
            # or maps them by hand. Archived rows are excluded from
            # day-to-day reads.
            assert row[0] is None
            assert row[1] is not None  # archived

            audit_rows = conn.execute(
                sa.text(
                    "SELECT actor_id, action, entity_type, entity_id, "
                    "before_json, after_json "
                    "FROM audit_log "
                    "WHERE action = 'taxonomy_field_def.archived_during_catalog_backfill'"
                )
            ).fetchall()
            assert len(audit_rows) == 1
            actor_id, _action, entity_type, entity_id, before, after = audit_rows[0]
            assert actor_id is None  # system event
            assert entity_type == "taxonomy_field_def"
            assert entity_id == 1
            before_payload = json.loads(before)
            after_payload = json.loads(after)
            assert before_payload["key"] == "bespoke_whatsit"
            assert before_payload["name"] == "Bespoke Whatsit"
            assert "reason" in after_payload
            assert after_payload["archived_at"]

    def test_already_archived_row_is_not_re_archived(self, db_path: Path) -> None:
        """If a row is already archived, we still write an audit row noting
        the unmatched state, but archived_at is not overwritten — preserves
        the original archival timestamp."""

        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0021_taxonomy_field_def_catalog_key")

        original_archived = "2025-01-01T00:00:00+00:00"
        with sqlite3.connect(db_path) as raw:
            _seed_node(raw, node_id=1, name="Rings")
            raw.execute(
                "INSERT INTO taxonomy_field_defs "
                "(id, node_id, name, key, type, required, sort_order, archived_at) "
                "VALUES (1, 1, 'Old Field', 'old_field', 'text', 0, 0, ?)",
                (original_archived,),
            )
            raw.commit()

        command.upgrade(cfg, "0022_backfill_catalog_key")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            row = conn.execute(
                sa.text("SELECT archived_at FROM taxonomy_field_defs WHERE id=1")
            ).first()
            assert row is not None
            # Original archival timestamp preserved (the UPDATE is gated
            # on ``archived_at IS NULL``).
            assert row[0] == original_archived


class TestBackfillMixed:
    def test_three_rows_one_per_outcome(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "0021_taxonomy_field_def_catalog_key")

        with sqlite3.connect(db_path) as raw:
            _seed_node(raw, node_id=1, name="Rings")
            _seed_field_def(
                raw,
                fd_id=1,
                node_id=1,
                name="anything",
                key="karat",  # by key
                field_type="select",
                options_json='["9ct"]',
            )
            _seed_field_def(
                raw,
                fd_id=2,
                node_id=1,
                name="Hallmark",  # by label
                key="some_other_key",
            )
            _seed_field_def(
                raw,
                fd_id=3,
                node_id=1,
                name="Unique Snowflake",
                key="snowflake",
            )
            raw.commit()

        command.upgrade(cfg, "0022_backfill_catalog_key")

        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            rows = conn.execute(
                sa.text(
                    "SELECT id, catalog_key, archived_at "
                    "FROM taxonomy_field_defs ORDER BY id"
                )
            ).fetchall()
            by_id = {r[0]: (r[1], r[2]) for r in rows}

            assert by_id[1][0] == "karat"
            assert by_id[1][1] is None
            assert by_id[2][0] == "hallmark"
            assert by_id[2][1] is None
            assert by_id[3][0] is None
            assert by_id[3][1] is not None

            audit_count = conn.execute(
                sa.text(
                    "SELECT COUNT(*) FROM audit_log "
                    "WHERE action = 'taxonomy_field_def.archived_during_catalog_backfill'"
                )
            ).scalar()
            assert audit_count == 1


class TestIdempotence:
    def test_re_running_is_a_noop(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "head")  # includes 0022 already

        # On a head DB every row already has catalog_key set (none exist
        # yet, but the migration's SELECT WHERE catalog_key IS NULL still
        # returns []). Downgrading + re-upgrading should change nothing.
        command.downgrade(cfg, "-1")
        command.upgrade(cfg, "head")

        # Nothing to assert beyond no exception — the migration handles
        # an empty result set cleanly.


class TestBackfillFromFreshHead:
    def test_fresh_upgrade_succeeds_with_no_rows(self, db_path: Path) -> None:
        cfg = _make_alembic_config(db_path)
        command.upgrade(cfg, "head")
        # No taxonomy_field_defs rows — upgrade to head must finish without
        # error.
        engine = sa.create_engine(f"sqlite:///{db_path}")
        with engine.connect() as conn:
            count = conn.execute(
                sa.text("SELECT COUNT(*) FROM taxonomy_field_defs")
            ).scalar()
            assert count == 0
