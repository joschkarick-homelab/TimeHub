"""One-time data migration: copy every row from the production Postgres into a
fresh SQLite file, preserving primary keys and foreign-key order.

Usage:
    SOURCE_URL=postgresql+psycopg://timehub:***@host:5432/timehub \
    TARGET_URL=sqlite:////absolute/path/timehub.sqlite \
    python -m scripts.migrate_pg_to_sqlite

The target schema is created from the models, so run this against the SAME code
revision the Hub image will run. After it finishes, stamp Alembic to head:
    DATABASE_URL=$TARGET_URL alembic stamp head
"""

import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from app import models  # noqa: F401 — registers all tables on Base.metadata
from app.db import Base

_BATCH = 1000


def copy_all(source: Engine, target: Engine) -> None:
    Base.metadata.create_all(target)
    # sorted_tables is parent-before-child → satisfies FK constraints on insert.
    for table in Base.metadata.sorted_tables:
        with source.connect() as src_conn:
            rows = [dict(r._mapping) for r in src_conn.execute(table.select())]
        if not rows:
            print(f"  {table.name}: 0 rows")
            continue
        with target.begin() as dst_conn:
            for start in range(0, len(rows), _BATCH):
                dst_conn.execute(table.insert(), rows[start : start + _BATCH])
        print(f"  {table.name}: {len(rows)} rows")


def main() -> int:
    source_url = os.environ.get("SOURCE_URL")
    target_url = os.environ.get("TARGET_URL")
    if not source_url or not target_url:
        print("Set SOURCE_URL (Postgres) and TARGET_URL (sqlite:///...).", file=sys.stderr)
        return 2
    print(f"Migrating {source_url} → {target_url}")
    copy_all(create_engine(source_url, future=True), create_engine(target_url, future=True))
    print("Done. Now run: DATABASE_URL=$TARGET_URL alembic stamp head")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
