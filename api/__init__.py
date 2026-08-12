"""StayPulse API package.

The analytics library lives in `src/staypulse`, which is not installed as a
distribution. Rather than duplicating that code under `api/`, the source root is put
on `sys.path` here so the API imports the SAME validated modules the scripts, the
tests and the scheduled jobs use. One implementation of every metric is the entire
point of the project, and a second copy under the API would defeat it.

Doing it in the package __init__ means it applies to every entrypoint - uvicorn,
pytest, and Render's start command - without each one needing PYTHONPATH set.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
