"""Migration runner.

Applies numbered SQL files in `migrations/` in filename order, exactly once each,
recording a checksum so a migration edited after it was applied is detected rather
than silently diverging from the deployed schema.

Usage:
    python scripts/migrate.py            # apply pending migrations
    python scripts/migrate.py --status   # report without applying
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402

MIGRATIONS_DIR = PROJECT_ROOT / "migrations"


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"))


def ledger_exists(conn) -> bool:
    return bool(
        conn.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='meta' AND table_name='schema_migration'"
            )
        ).scalar()
    )


def applied_map(conn) -> dict[str, str]:
    """filename -> checksum for migrations already applied."""
    if not ledger_exists(conn):
        return {}
    rows = conn.execute(
        text("SELECT filename, checksum_sha256 FROM meta.schema_migration")
    ).all()
    return {r[0]: r[1] for r in rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="report without applying")
    args = parser.parse_args()

    files = migration_files()
    if not files:
        print("No migration files found.")
        return 1

    engine = db.get_engine()
    with engine.connect() as conn:
        applied = applied_map(conn)

    pending = [f for f in files if f.name not in applied]
    drifted = [
        f for f in files
        if f.name in applied and applied[f.name] != sha256(f.read_text(encoding="utf-8"))
    ]

    print(f"migrations dir : {MIGRATIONS_DIR}")
    print(f"total          : {len(files)}")
    print(f"already applied: {len(applied)}")
    print(f"pending        : {len(pending)}")
    if drifted:
        print("\n  WARNING - these migrations were edited after being applied:")
        for f in drifted:
            print(f"    {f.name}")
        print("  The deployed schema no longer matches the file. Add a new migration")
        print("  rather than editing an applied one.")

    if args.status:
        for f in files:
            mark = "applied" if f.name in applied else "PENDING"
            print(f"  [{mark:>7}] {f.name}")
        return 1 if drifted else 0

    if not pending:
        print("\nNothing to apply. Schema is current.")
        return 1 if drifted else 0

    print()
    for path in pending:
        sql = path.read_text(encoding="utf-8")
        started = time.perf_counter()
        try:
            with engine.begin() as conn:
                # Execute through the raw driver cursor with no parameters.
                # exec_driver_sql() hands the script to psycopg's placeholder
                # parser, which chokes on a literal '%' -- and this schema is full
                # of them ("5% without ITC", "roughly 33%"). psycopg only parses
                # placeholders when params are supplied, so passing none keeps the
                # comment text intact instead of forcing '%%' escapes into the
                # documentation that ships with the database.
                # The surrounding transaction still makes each migration atomic.
                with conn.connection.driver_connection.cursor() as cur:
                    cur.execute(sql)
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO meta.schema_migration "
                        "(filename, checksum_sha256, duration_ms) "
                        "VALUES (:f, :c, :d) "
                        "ON CONFLICT (filename) DO UPDATE "
                        "SET checksum_sha256 = EXCLUDED.checksum_sha256, "
                        "    applied_at = now(), duration_ms = EXCLUDED.duration_ms"
                    ),
                    {"f": path.name, "c": sha256(sql), "d": elapsed_ms},
                )
            print(f"  [ ok ] {path.name}  ({elapsed_ms} ms)")
        except Exception as exc:  # noqa: BLE001
            print(f"  [FAIL] {path.name}")
            print(f"         {type(exc).__name__}: {exc}")
            print("\n  Migration failed and was rolled back. Later migrations not attempted.")
            return 1

    with engine.connect() as conn:
        objs = conn.execute(
            text(
                "SELECT table_schema, count(*) FROM information_schema.tables "
                "WHERE table_schema IN ('raw','staging','mart','meta') "
                "AND table_type='BASE TABLE' GROUP BY table_schema ORDER BY table_schema"
            )
        ).all()
    print("\nSchema now contains:")
    for schema, n in objs:
        print(f"  {schema:<9} {n} tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
