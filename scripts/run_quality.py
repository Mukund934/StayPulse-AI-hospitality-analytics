"""Run the data-quality framework and print a scorecard.

Usage:
    python scripts/run_quality.py
    python scripts/run_quality.py --no-persist    # do not write meta.dq_result
    python scripts/run_quality.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.quality import runner  # noqa: E402

DIM_ORDER = ["completeness", "uniqueness", "validity", "consistency", "accuracy", "timeliness"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-persist", action="store_true")
    ap.add_argument("--json", type=str, default=None)
    args = ap.parse_args()

    report = runner.run_all(persist_results=not args.no_persist)
    results = report["results"]

    print("=" * 88)
    print("  StayPulse - data quality scorecard")
    print(f"  {report['checked_at']}   run_id={report['run_id']}")
    print("=" * 88)

    for dim in DIM_ORDER:
        subset = [r for r in results if r.rule.dimension == dim]
        if not subset:
            continue
        print(f"\n{dim.upper()}")
        for r in sorted(subset, key=lambda x: x.rule.rule_id):
            mark = "PASS" if r.passed else "FAIL"
            print(f"  [{mark}] {r.rule.rule_id:<34} "
                  f"{r.rows_failed:>7,} / {r.rows_checked:>8,}  "
                  f"({r.failure_pct:5.2f}% | tol {r.rule.threshold_pct:4.1f}% | {r.rule.severity})")
            if r.error:
                print(f"         ERROR: {r.error}")
            elif not r.passed and r.sample_keys:
                print(f"         sample: {r.sample_keys}")

    print("\n" + "-" * 88)
    print("DEFECT DETECTION RECALL  (did the framework catch what was planted?)")
    print("-" * 88)
    for d in report["defect_recall"]:
        mark = "CAUGHT " if d["detected"] else "MISSED "
        print(f"  [{mark}] {d['defect_class']:<28} {d['rows_detected']:>7,} rows   "
              f"via {', '.join(d['rules'])}")

    print("\n" + "-" * 88)
    print("FRESHNESS")
    print("-" * 88)
    for f in report["freshness"]:
        print(f"  {f['source']:<24} last seen {str(f['last_seen'])[:19]}  "
              f"({float(f['hours_old'] or 0):,.1f}h old)")

    print("\n" + "=" * 88)
    print(f"  rules run       : {report['total_rules']}")
    print(f"  passed          : {report['passed']}")
    print(f"  failed          : {report['failed']}")
    print(f"  errored         : {report['errored']}")
    print(f"  rows affected   : {report['rows_affected']:,}")
    print(f"  QUALITY SCORE   : {report['quality_score']:.2f} / 100   "
          "(severity-weighted pass rate)")
    print("=" * 88)

    if args.json:
        payload = {
            "checked_at": report["checked_at"],
            "quality_score": report["quality_score"],
            "total_rules": report["total_rules"],
            "passed": report["passed"],
            "failed": report["failed"],
            "rows_affected": report["rows_affected"],
            "defect_recall": report["defect_recall"],
            "rules": [{
                "rule_id": r.rule.rule_id,
                "dimension": r.rule.dimension,
                "severity": r.rule.severity,
                "description": r.rule.description,
                "expectation": r.rule.expectation,
                "rows_checked": r.rows_checked,
                "rows_failed": r.rows_failed,
                "failure_pct": round(r.failure_pct, 4),
                "passed": r.passed,
                "error": r.error,
            } for r in results],
        }
        Path(args.json).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"\n  JSON written to {args.json}")

    # An errored rule is a broken check and must not read as healthy.
    return 1 if report["errored"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
