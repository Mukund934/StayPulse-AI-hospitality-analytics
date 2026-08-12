"""Export day-partitioned CSVs for the Excel / Power Query folder-connector demo.

Partitioned on purpose. One tidy file would make the folder connector pointless --
the whole exercise is combining many files under a single sample-file transform, the
way a real daily export actually arrives.

Usage:
    python scripts/export_excel_feed.py --days 45
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pandas as pd  # noqa: E402

from staypulse import db  # noqa: E402

OUT = PROJECT_ROOT / "excel" / "feed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    for old in OUT.glob("*.csv"):
        old.unlink()

    rows = db.fetch_all("""
        SELECT stay_date, property_code, rooms_available, rooms_sold,
               rooms_out_of_order, room_revenue_net_inr,
               occupancy_pct, adr_inr, revpar_inr
        FROM mart.v_daily_kpi
        WHERE stay_date > (SELECT max(stay_date) - :d FROM mart.v_daily_kpi)
        ORDER BY stay_date, property_code
    """, d=args.days)
    df = pd.DataFrame(rows)
    if df.empty:
        print("no rows")
        return 1

    written = 0
    for day, chunk in df.groupby("stay_date"):
        chunk.to_csv(OUT / f"daily_kpi_{day}.csv", index=False, encoding="utf-8")
        written += 1

    print(f"{written} daily files, {len(df):,} rows total -> excel/feed/")
    print(f"columns: {', '.join(df.columns)}")
    print("\nNext: Excel -> Data -> Get Data -> From Folder -> excel/feed")
    print("See excel/README.md for the transform steps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
