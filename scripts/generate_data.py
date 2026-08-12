"""Generate the synthetic dataset and load it into the mart.

Usage:
    python scripts/generate_data.py                 # generate + load
    python scripts/generate_data.py --dry-run       # generate, report, do not load
    python scripts/generate_data.py --seed 12345
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402
from staypulse.generate import spec  # noqa: E402
from staypulse.generate.builder import Generator, dataset_fingerprint  # noqa: E402
from staypulse.generate.load import load, truncate_mart  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=spec.RANDOM_SEED)
    ap.add_argument("--guests", type=int, default=spec.GUEST_POOL)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    print("=" * 72)
    print("  StayPulse - synthetic dataset generation")
    print(f"  seed={args.seed}  period={spec.PERIOD_START} .. {spec.PERIOD_END}")
    print("  SYNTHETIC DATA. Not real operational data from any company.")
    print("=" * 72)

    t0 = time.perf_counter()
    gen = Generator(seed=args.seed)
    data = gen.generate(n_guests=args.guests)
    gen_s = time.perf_counter() - t0

    print(f"\nGenerated in {gen_s:.1f}s")
    for name, n in data.summary().items():
        print(f"  {name:<20} {n:>9,}")
    print(f"\n  fingerprint: {dataset_fingerprint(data)}")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    run_id = None
    with db.connect() as conn:
        run_id = conn.execute(text(
            "INSERT INTO meta.pipeline_run (pipeline, notes) "
            "VALUES ('generate_and_load', :n) RETURNING run_id"
        ), {"n": f"seed={args.seed} guests={args.guests}"}).scalar_one()

    try:
        print("\nTruncating mart (dim_date preserved - it is reference data)...")
        truncate_mart()
        print("Loading via COPY...")
        t1 = time.perf_counter()
        written = load(data)
        load_s = time.perf_counter() - t1

        total = sum(written.values())
        print(f"\nLoaded {total:,} rows in {load_s:.1f}s")
        for tbl, n in written.items():
            print(f"  {tbl:<26} {n:>9,}")

        with db.connect() as conn:
            conn.execute(text(
                "UPDATE meta.pipeline_run SET finished_at = now(), status='success', "
                "rows_out = :r, notes = notes || :s WHERE run_id = :id"
            ), {"r": total, "s": f" fingerprint={dataset_fingerprint(data)}", "id": run_id})
        return 0
    except Exception as exc:  # noqa: BLE001
        with db.connect() as conn:
            conn.execute(text(
                "UPDATE meta.pipeline_run SET finished_at = now(), status='failed', "
                "error_message = :e WHERE run_id = :id"
            ), {"e": f"{type(exc).__name__}: {exc}"[:2000], "id": run_id})
        print(f"\nLOAD FAILED: {type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    raise SystemExit(main())
