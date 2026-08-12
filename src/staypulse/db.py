"""Database access for StayPulse.

A single place to build engines so connection settings, timeouts and the
statement-level defaults are consistent everywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

from staypulse import config

# Supabase's session pooler tolerates ordinary pooling, but a portfolio pipeline
# is bursty and short-lived: a small pool with pre-ping avoids handing out a
# connection the pooler has already recycled.
_ENGINE: Engine | None = None


def get_engine(*, echo: bool = False) -> Engine:
    """Return the process-wide engine, creating it on first use."""
    global _ENGINE
    if _ENGINE is None:
        config.load_env()
        db = config.get_database_config()
        _ENGINE = create_engine(
            db.url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=2,
            pool_recycle=1800,
            connect_args={"connect_timeout": 20, "application_name": "staypulse"},
        )
    return _ENGINE


@contextmanager
def connect() -> Iterator[Connection]:
    """Autocommitting connection context manager."""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


def scalar(sql: str, **params: Any) -> Any:
    """Run a query returning exactly one value."""
    with connect() as conn:
        return conn.execute(text(sql), params).scalar_one()


def fetch_all(sql: str, **params: Any) -> list[dict[str, Any]]:
    """Run a query and return rows as dictionaries."""
    with connect() as conn:
        result = conn.execute(text(sql), params)
        return [dict(row) for row in result.mappings()]


def table_counts(schema: str) -> dict[str, int]:
    """Exact row count for every table in a schema. Used by reconciliation checks."""
    names = [
        r["table_name"]
        for r in fetch_all(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = :s AND table_type = 'BASE TABLE' ORDER BY table_name",
            s=schema,
        )
    ]
    counts: dict[str, int] = {}
    with connect() as conn:
        for name in names:
            # Identifiers cannot be bound as parameters; they come from
            # information_schema, not user input, and are quoted defensively.
            counts[name] = conn.execute(
                text(f'SELECT count(*) FROM "{schema}"."{name}"')
            ).scalar_one()
    return counts
