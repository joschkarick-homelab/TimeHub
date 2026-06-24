"""The migration chain must actually run — production boots via
`alembic upgrade head`, but the rest of the suite builds the schema with
`Base.metadata.create_all`, so the migrations themselves are otherwise never
exercised. Run the full chain (and the downgrade path) against a throwaway
SQLite database."""

import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

from alembic import command
from alembic.config import Config


def _alembic_config(connection) -> Config:
    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    # env.py picks this up instead of building its own engine from settings.
    cfg.attributes["connection"] = connection
    return cfg


def test_migration_chain_upgrades_and_downgrades():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'mig.sqlite'}"
        engine = create_engine(url)
        with engine.begin() as conn:
            cfg = _alembic_config(conn)

            command.upgrade(cfg, "head")
            tables = set(inspect(conn).get_table_names())
            # Core tables created somewhere along the chain must exist.
            assert {"users", "projects", "time_entries", "import_formats"} <= tables

            # The whole chain must be reversible without raising.
            command.downgrade(cfg, "base")
            remaining = set(inspect(conn).get_table_names()) - {"alembic_version"}
            assert remaining == set()
        engine.dispose()


def test_status_columns_are_indexed():
    """M9: sync-status filter columns must carry an index after the chain."""
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'mig.sqlite'}"
        engine = create_engine(url)
        with engine.begin() as conn:
            command.upgrade(_alembic_config(conn), "head")
            insp = inspect(conn)
            te_indexed = {c for ix in insp.get_indexes("time_entries") for c in ix["column_names"]}
            es_indexed = {c for ix in insp.get_indexes("entry_syncs") for c in ix["column_names"]}
            assert "sync_status" in te_indexed
            assert "status" in es_indexed
        engine.dispose()


def test_import_formats_schema_matches_model_nullability():
    """transforms/target_rules are nullable in both the model and migration
    0004 — guards against the drift the model previously had."""
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{Path(tmp) / 'mig.sqlite'}"
        engine = create_engine(url)
        with engine.begin() as conn:
            command.upgrade(_alembic_config(conn), "head")
            cols = {c["name"]: c for c in inspect(conn).get_columns("import_formats")}
            assert cols["transforms"]["nullable"] is True
            assert cols["target_rules"]["nullable"] is True
        engine.dispose()
