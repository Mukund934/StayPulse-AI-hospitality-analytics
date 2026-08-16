"""Load the curated holiday calendar into `mart.dim_date`.

Reads data/reference/india_holidays.json, populates the calendar columns added by
migration 007, and records provenance in `meta.calendar_source`.

WHAT THIS COMPUTES, AND WHAT IT REFUSES TO

It computes only things that follow from the holiday DATES themselves: which dates
are holidays, how far each date sits from the nearest one, whether a date is
adjacent, whether it is a bridge day, and whether it sits in a long weekend.

It does NOT compute or store any demand effect. The size and shape of the holiday
effect is measured from booking data by `staypulse.signals.calendar`, and it is
deliberately kept out of the schema so that validating the measurement against the
generator's planted windows stays non-circular.

Run:
    python scripts/load_calendar.py
    python scripts/load_calendar.py --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402

SOURCE = PROJECT_ROOT / "data" / "reference" / "india_holidays.json"

# How far from a public holiday a date is still considered affected.
#
# Chosen as a documented parameter rather than discovered from the data, because
# discovering it from the same data used to measure the effect would be fitting the
# window to the answer. Seven days covers the week either side, which is the
# horizon over which a corporate trip would be moved or dropped. The measured
# offset profile in `signals.calendar` reports every offset separately, so a reader
# can see where the effect actually starts and stops regardless of this radius.
ADJACENCY_RADIUS_DAYS = 7


def load_source() -> dict:
    if not SOURCE.exists():
        raise SystemExit(f"Calendar source not found: {SOURCE}")
    return json.loads(SOURCE.read_text(encoding="utf-8"))


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_working_day(day: dt.date, holidays: set[dt.date]) -> bool:
    return day.isoweekday() <= 5 and day not in holidays


def compute_context(dates: list[dt.date], holidays: dict[dt.date, dict]) -> list[dict]:
    """Derive per-date calendar context from the holiday dates alone."""
    holiday_days = set(holidays)
    rows: list[dict] = []

    # Sorted once so ties resolve to the EARLIER holiday deterministically: the
    # comparison below only replaces on a strictly smaller distance, so the first
    # holiday encountered at a given distance wins.
    ordered = sorted(holiday_days)

    for day in dates:
        nearest, offset = None, None
        for hday in ordered:
            delta = (day - hday).days
            if offset is None or abs(delta) < abs(offset):
                nearest, offset = holidays[hday]["name"], delta

        is_holiday = day in holiday_days
        adjacent = offset is not None and abs(offset) <= ADJACENCY_RADIUS_DAYS

        # A bridge day is a lone working day wedged between a holiday/weekend on
        # both sides. In a corporate market people simply take it off.
        prev_day, next_day = day - dt.timedelta(days=1), day + dt.timedelta(days=1)
        bridge = (
            _is_working_day(day, holiday_days)
            and not _is_working_day(prev_day, holiday_days)
            and not _is_working_day(next_day, holiday_days)
        )

        # A long weekend is a non-working day whose run of consecutive non-working
        # days reaches three or more.
        long_weekend = False
        if not _is_working_day(day, holiday_days):
            run, probe = 1, day - dt.timedelta(days=1)
            while not _is_working_day(probe, holiday_days) and run < 10:
                run += 1
                probe -= dt.timedelta(days=1)
            probe = day + dt.timedelta(days=1)
            while not _is_working_day(probe, holiday_days) and run < 10:
                run += 1
                probe += dt.timedelta(days=1)
            long_weekend = run >= 3

        info = holidays.get(day, {})
        rows.append({
            "d": day,
            "is_holiday": is_holiday,
            "name": info.get("name"),
            "scope": info.get("scope"),
            "confidence": info.get("confidence"),
            "offset": offset,
            "nearest": nearest,
            "adjacent": adjacent,
            "long_weekend": long_weekend,
            "bridge": bridge,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="report without writing")
    args = parser.parse_args()

    payload = load_source()
    entries = payload["holidays"]
    holidays = {dt.date.fromisoformat(h["date"]): h for h in entries}
    lunar = sum(1 for h in entries if h.get("confidence") == "lunar")

    print("Calendar load")
    print(f"  source        : {SOURCE.relative_to(PROJECT_ROOT)}")
    print(f"  entries       : {len(entries)}")
    print(f"  needs review  : {lunar} lunar-calendar dates (human check required)")
    print(f"  adjacency     : +/-{ADJACENCY_RADIUS_DAYS} days")

    dates = [r["full_date"] for r in db.fetch_all(
        "SELECT full_date FROM mart.dim_date ORDER BY full_date"
    )]
    if not dates:
        raise SystemExit("dim_date is empty; run the generator first.")

    rows = compute_context(dates, holidays)
    in_range = [r for r in rows if r["is_holiday"]]
    print(f"  dim_date rows : {len(dates):,} ({dates[0]} .. {dates[-1]})")
    print(f"  holidays hit  : {len(in_range)} of {len(entries)} fall inside dim_date")
    print(f"  adjacent days : {sum(1 for r in rows if r['adjacent']):,}")
    print(f"  bridge days   : {sum(1 for r in rows if r['bridge'])}")
    print(f"  long weekends : {sum(1 for r in rows if r['long_weekend'])}")

    if args.dry_run:
        print("\n  --dry-run: nothing written.")
        return 0

    with db.connect() as conn:
        conn.execute(text("""
            UPDATE mart.dim_date SET
                is_public_holiday = false, holiday_name = NULL, holiday_scope = NULL,
                holiday_confidence = NULL, days_to_holiday = NULL, nearest_holiday = NULL,
                is_holiday_adjacent = false, is_long_weekend = false, is_bridge_day = false
        """))
        for r in rows:
            conn.execute(text("""
                UPDATE mart.dim_date SET
                    is_public_holiday   = :h,
                    holiday_name        = :n,
                    holiday_scope       = :s,
                    holiday_confidence  = :c,
                    days_to_holiday     = :o,
                    nearest_holiday     = :nn,
                    is_holiday_adjacent = :a,
                    is_long_weekend     = :lw,
                    is_bridge_day       = :b
                WHERE full_date = :d
            """), {
                "h": r["is_holiday"], "n": r["name"], "s": r["scope"],
                "c": r["confidence"], "o": r["offset"], "nn": r["nearest"],
                "a": r["adjacent"], "lw": r["long_weekend"], "b": r["bridge"],
                "d": r["d"],
            })

        conn.execute(text("""
            INSERT INTO meta.calendar_source
                (source_key, description, origin, coverage_from, coverage_to,
                 entry_count, needs_review, checksum_sha256)
            VALUES (:k, :d, :o, :f, :t, :n, :r, :c)
            ON CONFLICT (source_key) DO UPDATE SET
                description = EXCLUDED.description, origin = EXCLUDED.origin,
                coverage_from = EXCLUDED.coverage_from, coverage_to = EXCLUDED.coverage_to,
                entry_count = EXCLUDED.entry_count, needs_review = EXCLUDED.needs_review,
                loaded_at = now(), checksum_sha256 = EXCLUDED.checksum_sha256
        """), {
            "k": "india_holidays",
            "d": "Curated Indian national and Karnataka holidays for the dataset window",
            "o": "committed file, data/reference/india_holidays.json - no external API "
                 "(Nager.Date does not cover India, verified HTTP 204)",
            "f": min(holidays), "t": max(holidays),
            "n": len(entries), "r": lunar, "c": checksum(SOURCE),
        })

    print("\n  Loaded. Provenance recorded in meta.calendar_source.")
    if lunar:
        print(f"  HUMAN ACTION: {lunar} lunar-calendar dates need a one-time check "
              f"against an official source.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
