"""Environment and connection configuration for StayPulse.

Design rules enforced here:

1. Secrets are read from the local ``.env`` (gitignored) or the real environment.
   They are never written to disk, never logged, and never included in any
   diagnostic output. :func:`describe_environment` returns presence and length
   only -- never a prefix, suffix, or any character of a secret value.

2. The database URL is *constructed*, not string-concatenated. Supabase pooler
   passwords routinely contain ``?``, ``#`` or ``@``; pasting one of those into a
   URI unencoded silently truncates the password and produces an authentication
   error that looks like a wrong credential. ``sqlalchemy.engine.URL.create``
   percent-encodes every component correctly, so the password is pasted verbatim
   into ``PGPASSWORD`` and never hand-escaped.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL, make_url

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"

#: Driver used for every connection. psycopg 3.
DRIVER = "postgresql+psycopg"

#: Variables that hold secret material. Values from these are never displayed.
SECRET_VARS = frozenset(
    {"PGPASSWORD", "DATABASE_URL", "GEMINI_API_KEY", "OPENWEATHER_API_KEY"}
)


def load_env(*, override: bool = False) -> bool:
    """Load ``.env`` into the process environment. Returns True if the file exists."""
    if ENV_PATH.exists():
        load_dotenv(ENV_PATH, override=override)
        return True
    return False


def _clean(name: str) -> str | None:
    """Read an env var, treating blank/whitespace-only as absent."""
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


@dataclass(frozen=True)
class DatabaseConfig:
    """Resolved database connection settings.

    ``source`` records which branch produced the URL, so diagnostics can explain
    *why* a connection looks the way it does without revealing the credential.
    """

    url: URL
    source: str

    @property
    def safe_display(self) -> str:
        """Connection string with the password masked. Safe to print or log."""
        return self.url.render_as_string(hide_password=True)

    @property
    def host(self) -> str:
        return self.url.host or "<unset>"

    @property
    def is_session_pooler(self) -> bool:
        """Supabase's session-mode pooler is the IPv4-compatible endpoint on 5432."""
        return "pooler.supabase.com" in (self.url.host or "") and self.url.port == 5432


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or self-contradictory."""


def get_database_config() -> DatabaseConfig:
    """Build the database URL from the environment.

    ``DATABASE_URL`` wins when set; otherwise the discrete ``PG*`` parameters are
    assembled. The discrete path is preferred because it removes any need for the
    user to percent-encode their own password.
    """
    raw_url = _clean("DATABASE_URL")
    if raw_url:
        url = make_url(raw_url)
        # Normalise the bare `postgres://` / `postgresql://` schemes onto psycopg 3.
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername=DRIVER)
        return DatabaseConfig(url=url, source="DATABASE_URL")

    host = _clean("PGHOST")
    user = _clean("PGUSER")
    password = _clean("PGPASSWORD")
    database = _clean("PGDATABASE") or "postgres"
    port_raw = _clean("PGPORT") or "5432"

    missing = [
        name
        for name, value in (("PGHOST", host), ("PGUSER", user), ("PGPASSWORD", password))
        if not value
    ]
    if missing:
        raise ConfigError(
            "Database is not configured. Set DATABASE_URL, or all of "
            f"PGHOST/PGUSER/PGPASSWORD (missing: {', '.join(missing)}). "
            f"Expected .env at {ENV_PATH}"
        )

    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"PGPORT must be an integer, got {port_raw!r}") from exc

    # URL.create percent-encodes each component, so `?`/`#`/`@` in the password
    # are handled correctly without the caller escaping anything.
    url = URL.create(
        drivername=DRIVER,
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    return DatabaseConfig(url=url, source="PG* discrete parameters")


def get_gemini_api_key() -> str:
    key = _clean("GEMINI_API_KEY")
    if not key:
        raise ConfigError(f"GEMINI_API_KEY is not set. Expected .env at {ENV_PATH}")
    return key


def get_openweather_api_key() -> str | None:
    """OpenWeather is optional; absence is a valid state, not an error."""
    return _clean("OPENWEATHER_API_KEY")


def describe_environment() -> list[tuple[str, str]]:
    """Presence report for diagnostics.

    Returns ``(variable, status)`` pairs. For secret variables the status is the
    word ``set`` plus a character count -- never any part of the value itself.
    """
    tracked = [
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
        "PGUSER",
        "PGPASSWORD",
        "DATABASE_URL",
        "GEMINI_API_KEY",
        "OPENWEATHER_API_KEY",
    ]
    report: list[tuple[str, str]] = []
    for name in tracked:
        value = _clean(name)
        if value is None:
            report.append((name, "not set"))
        elif name in SECRET_VARS:
            report.append((name, f"set ({len(value)} chars, value hidden)"))
        else:
            report.append((name, f"set -> {value}"))
    return report
