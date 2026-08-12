"""Environment doctor for StayPulse.

Verifies every external dependency before any build work runs, and prints a
report that is safe to paste anywhere: secret values are scrubbed from all
output, including from third-party exception messages.

Usage:
    python scripts/verify_env.py
"""

from __future__ import annotations

import importlib
import os
import platform
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import config  # noqa: E402

PASS, FAIL, WARN, SKIP = "PASS", "FAIL", "WARN", "SKIP"
_MARK = {PASS: "[ ok ]", FAIL: "[FAIL]", WARN: "[warn]", SKIP: "[skip]"}

results: list[tuple[str, str, str]] = []


def _secret_values() -> list[str]:
    """Every secret currently in the environment, longest first.

    Longest-first matters: if one secret is a substring of another, scrubbing the
    longer one first prevents a partial replacement leaving fragments behind.
    """
    values = [
        v.strip()
        for name in config.SECRET_VARS
        if (v := os.environ.get(name)) and v.strip()
    ]
    return sorted(values, key=len, reverse=True)


def scrub(text: str) -> str:
    """Remove any secret value from a string before it is displayed."""
    for secret in _secret_values():
        if len(secret) >= 4:
            text = text.replace(secret, "***REDACTED***")
    return text


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, scrub(detail)))
    line = f"  {_MARK[status]}  {name}"
    if detail:
        line += f"\n           {scrub(detail)}"
    print(line, flush=True)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}", flush=True)


# --------------------------------------------------------------------------
# 1. Runtime and packages
# --------------------------------------------------------------------------
def check_runtime() -> None:
    section("1. Runtime")
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        record("Python >= 3.10", PASS, f"{platform.python_version()} on {platform.system()}")
    else:
        record("Python >= 3.10", FAIL, f"found {platform.python_version()}")

    required = {
        "pandas": "dataframes",
        "numpy": "numerics",
        "sqlalchemy": "database engine",
        "psycopg": "postgres driver",
        "dotenv": "env loading",
        "pydantic": "schema validation",
        "faker": "synthetic data",
        "google.genai": "gemini client",
        "requests": "http",
        "pytest": "tests",
    }
    missing = []
    for module, purpose in required.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module} ({purpose})")
    if missing:
        record(
            "Python packages",
            FAIL,
            f"missing: {', '.join(missing)} -- run: pip install -r requirements.txt",
        )
    else:
        record("Python packages", PASS, f"all {len(required)} present")


# --------------------------------------------------------------------------
# 2. Configuration presence
# --------------------------------------------------------------------------
def check_config() -> None:
    section("2. Configuration")
    if config.ENV_PATH.exists():
        record(".env file", PASS, str(config.ENV_PATH))
    else:
        record(
            ".env file",
            FAIL,
            f"not found at {config.ENV_PATH} -- copy .env.example to .env and fill it in",
        )

    print()
    for name, status_text in config.describe_environment():
        marker = "  -" if status_text != "not set" else "  x"
        print(f"    {marker} {name:<22} {status_text}")


# --------------------------------------------------------------------------
# 3. Database
# --------------------------------------------------------------------------
def check_database() -> None:
    section("3. Supabase PostgreSQL")
    try:
        db = config.get_database_config()
    except config.ConfigError as exc:
        record("Database config", FAIL, str(exc))
        return

    record("Database config", PASS, f"from {db.source} -> {db.safe_display}")

    if db.is_session_pooler:
        record("Session pooler (IPv4)", PASS, "pooler host on port 5432 as expected")
    else:
        record(
            "Session pooler (IPv4)",
            WARN,
            f"host={db.host} port={db.url.port}. Expected *.pooler.supabase.com:5432 "
            "(Session mode). The direct db.*.supabase.co host is IPv6-only.",
        )

    try:
        from sqlalchemy import create_engine, text

        engine = create_engine(db.url, connect_args={"connect_timeout": 15})
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version()")).scalar_one()
            dbname = conn.execute(text("SELECT current_database()")).scalar_one()
            tables = conn.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            ).scalar_one()
        server = version.split(" on ")[0] if version else "unknown"
        record("Database connection", PASS, f"{server} | db={dbname} | public tables={tables}")
    except Exception as exc:  # noqa: BLE001 - report any driver failure verbatim but scrubbed
        record("Database connection", FAIL, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 4. Gemini
# --------------------------------------------------------------------------
def check_gemini() -> None:
    section("4. Gemini API")
    try:
        key = config.get_gemini_api_key()
    except config.ConfigError as exc:
        record("GEMINI_API_KEY", FAIL, str(exc))
        return
    record("GEMINI_API_KEY", PASS, f"present ({len(key)} chars, value hidden)")

    try:
        from google import genai

        client = genai.Client(api_key=key)
        names = []
        for model in client.models.list():
            actions = getattr(model, "supported_actions", None) or []
            if not actions or "generateContent" in actions:
                names.append(model.name.removeprefix("models/"))
            if len(names) >= 60:
                break
        if names:
            preview = ", ".join(sorted(names)[:6])
            record("Gemini connectivity", PASS, f"{len(names)} usable models. e.g. {preview}")
        else:
            record("Gemini connectivity", WARN, "authenticated but no generative models listed")
    except Exception as exc:  # noqa: BLE001
        record("Gemini connectivity", FAIL, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 5. OpenWeather (optional)
# --------------------------------------------------------------------------
def check_openweather() -> None:
    section("5. OpenWeather (optional)")
    key = config.get_openweather_api_key()
    if not key:
        record("OPENWEATHER_API_KEY", SKIP, "not set -- optional, feature stays disabled")
        return
    try:
        import requests

        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={"q": "Bengaluru,IN", "appid": key, "units": "metric"},
            timeout=15,
        )
        if resp.status_code == 200:
            payload = resp.json()
            record(
                "OpenWeather connectivity",
                PASS,
                f"Bengaluru {payload['main']['temp']}C, {payload['weather'][0]['description']}",
            )
        elif resp.status_code == 401:
            record(
                "OpenWeather connectivity",
                WARN,
                "401 Unauthorized. New keys can take up to ~2 hours to activate.",
            )
        else:
            record("OpenWeather connectivity", WARN, f"HTTP {resp.status_code}")
    except Exception as exc:  # noqa: BLE001
        record("OpenWeather connectivity", WARN, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 6. Desktop tooling and git
# --------------------------------------------------------------------------
def check_desktop_and_git() -> None:
    section("6. Desktop tooling and git")
    pbi = Path(r"C:\Program Files\Microsoft Power BI Desktop\bin\PBIDesktop.exe")
    record("Power BI Desktop", PASS if pbi.exists() else WARN,
           str(pbi) if pbi.exists() else "not at default path")

    excel_paths = [
        Path(r"C:\Program Files\Microsoft Office\root\Office16\EXCEL.EXE"),
        Path(r"C:\Program Files (x86)\Microsoft Office\root\Office16\EXCEL.EXE"),
    ]
    found = next((p for p in excel_paths if p.exists()), None)
    record("Excel", PASS if found else WARN, str(found) if found else "not at default path")

    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10
        )

    try:
        if git("rev-parse", "--git-dir").returncode != 0:
            record("Git repository", WARN, "not a git repository yet -- run: git init -b main")
            return

        # `git branch --show-current` works on an unborn branch; `rev-parse HEAD`
        # does not, because a repo with zero commits has no HEAD to resolve.
        branch = git("branch", "--show-current").stdout.strip() or "(detached)"
        commits = git("rev-list", "--count", "HEAD")
        n_commits = commits.stdout.strip() if commits.returncode == 0 else "0"
        has_remote = bool(git("remote", "-v").stdout.strip())

        detail = (
            f"branch={branch} | commits={n_commits} | "
            f"remote={'configured' if has_remote else 'NONE configured'}"
        )
        record("Git repository", PASS if has_remote else WARN, detail)
    except Exception as exc:  # noqa: BLE001
        record("Git repository", WARN, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# 7. Secret hygiene
# --------------------------------------------------------------------------
def check_secret_hygiene() -> None:
    section("7. Secret hygiene")
    gitignore = PROJECT_ROOT / ".gitignore"
    if gitignore.exists() and ".env" in gitignore.read_text(encoding="utf-8"):
        record(".env is gitignored", PASS)
    else:
        record(".env is gitignored", FAIL, "add `.env` to .gitignore before committing anything")

    example = PROJECT_ROOT / ".env.example"
    record(".env.example exists", PASS if example.exists() else FAIL,
           "placeholders only, safe to commit" if example.exists() else "missing")

    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=10,
        )
        if tracked.returncode == 0:
            record("`.env` not tracked by git", FAIL,
                   "CRITICAL: .env is tracked. Run: git rm --cached .env")
        else:
            record("`.env` not tracked by git", PASS)
    except Exception:  # noqa: BLE001
        record("`.env` not tracked by git", SKIP, "git not available")


def main() -> int:
    # ASCII only: the Windows console defaults to a codepage that mangles em dashes.
    print("=" * 74)
    print("  StayPulse - environment verification")
    print("  Secret values are never displayed. This output is safe to share.")
    print("=" * 74)

    loaded = config.load_env()
    if not loaded:
        print(f"\n  NOTE: no .env found at {config.ENV_PATH}; reading process environment only.")

    check_runtime()
    check_config()
    check_database()
    check_gemini()
    check_openweather()
    check_desktop_and_git()
    check_secret_hygiene()

    failures = [r for r in results if r[1] == FAIL]
    warnings = [r for r in results if r[1] == WARN]

    section("Summary")
    print(f"  {len(results)} checks | "
          f"{sum(1 for r in results if r[1] == PASS)} pass | "
          f"{len(warnings)} warn | {len(failures)} fail")
    if failures:
        print("\n  Blocking failures:")
        for name, _, detail in failures:
            print(f"    - {name}: {detail}")
        print("\n  Fix these before implementation continues.")
        return 1
    print("\n  Environment verified. Safe to proceed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
