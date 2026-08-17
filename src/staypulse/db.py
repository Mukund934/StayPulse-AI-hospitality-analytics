"""Database access for StayPulse.

A single place to build engines so connection settings, timeouts and the
statement-level defaults are consistent everywhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import Connection

from staypulse import config

# Supabase's session pooler tolerates ordinary pooling, but a portfolio pipeline
# is bursty and short-lived: a small pool with pre-ping avoids handing out a
# connection the pooler has already recycled.
_ENGINE: Engine | None = None

# When set, every query in this module runs on the bound connection instead of
# checking one out of the pool. See `bind` and `rollback_sandbox`.
_BOUND: ContextVar[Connection | None] = ContextVar("staypulse_bound_conn", default=None)


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
    """Autocommitting connection context manager.

    If a connection is bound to the current context, it is reused and neither
    committed nor closed here -- whoever bound it owns its transaction.
    """
    bound = _BOUND.get()
    if bound is not None:
        yield bound
        return
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


@contextmanager
def bind(conn: Connection) -> Iterator[Connection]:
    """Route every query in this module through `conn` for the duration.

    Without this, each call checks out its own pooled connection and therefore
    its own transaction, so uncommitted rows are invisible to the next call.
    """
    token = _BOUND.set(conn)
    try:
        yield conn
    finally:
        _BOUND.reset(token)


@contextmanager
def session() -> Iterator[Connection]:
    """One connection, one transaction, for a burst of related reads.

    Two reasons, and the second is the important one.

    Speed: every call otherwise checks a connection out of the pool and pre-pings
    it, which against a hosted database is a round trip per query. A dozen small
    queries spend most of their time on connection handling.

    Consistency: a reconstruction that issues twelve queries outside a shared
    transaction can see twelve different states of the database. For an as-of
    replay that is not a theoretical concern -- the book and the benchmark it is
    scored against would be allowed to disagree.

    Nests. An outer binding wins, so a session opened inside `rollback_sandbox`
    joins that transaction rather than opening a second connection that cannot
    see it. Without this the sandbox is invisible to exactly the code it exists
    to test, and every leakage assertion passes for the wrong reason -- which is
    how the control test in tests/test_replay.py found it.
    """
    bound = _BOUND.get()
    if bound is not None:
        yield bound
        return
    engine = get_engine()
    with engine.begin() as conn:
        with bind(conn):
            yield conn


@contextmanager
def rollback_sandbox() -> Iterator[Connection]:
    """A bound connection whose transaction is ALWAYS rolled back.

    WHY THIS EXISTS, given that it is the only write path in the codebase.

    The no-leakage claims in `analytics.replay` and `analytics.revenue` are
    claims about what a query does when rows exist that it must not see. The
    honest way to test that is to make those rows exist and check the answer
    does not move. Asserting the same thing by reading the SQL is not a test,
    it is a second opinion about the code from the person who wrote it.

    The rollback is in a `finally`, so it happens on success, on assertion
    failure and on exception alike. Nothing written inside this block reaches
    the database. It is used only by tests; production code never writes.
    """
    engine = get_engine()
    conn = engine.connect()
    trans = conn.begin()
    try:
        with bind(conn):
            yield conn
    finally:
        trans.rollback()
        conn.close()


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
