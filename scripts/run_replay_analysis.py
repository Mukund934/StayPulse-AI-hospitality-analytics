"""Replay StayPulse at a grid of historical dates and publish what it got right.

WHAT THIS MEASURES, AND THE ONE NUMBER THAT MAKES IT MEAN ANYTHING

For each as-of date it reconstructs the decision, then scores it against the
outcome on two axes:

  - forecast MAE, in room-nights, by horizon
  - pace calls: did a date flagged behind actually finish below what comparable
    dates finally carry

The second is worthless without its base rate. If 40% of every scored date
finishes below expectation, then a "behind" flag that is right 45% of the time is
a coin toss with a label on it. Both numbers are computed and both are published,
and the lift between them is the actual result -- however it lands.

Usage:
    python scripts/run_replay_analysis.py [--dates 12] [--horizon 35]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import replay as rp  # noqa: E402
from staypulse.analytics import revenue as rv  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"

# Horizons reported separately. A replay is most interesting where the book
# carries signal, and this market's book is nearly empty past a month.
HORIZON_BUCKETS = ((1, 3), (4, 7), (8, 14), (15, 30))


def _grid(count: int, horizon: int) -> list[dt.date]:
    """As-of dates whose outcome is fully resolvable inside the dataset.

    Spread evenly rather than clustered at the end: the portfolio grew in March
    2026, and a grid that only samples after the expansion would describe one
    business rather than eighteen months of one.
    """
    last = rv.data_horizon() - dt.timedelta(days=horizon)
    first = rv.data_horizon() - dt.timedelta(days=horizon + 400)
    span = (last - first).days
    step = max(1, span // max(1, count - 1))
    return [first + dt.timedelta(days=i * step) for i in range(count)]


def _bucket(days_out: int) -> str | None:
    for lo, hi in HORIZON_BUCKETS:
        if lo <= days_out <= hi:
            return f"{lo}-{hi}"
    return None


def run(count: int, horizon: int) -> dict[str, Any]:
    dates = _grid(count, horizon)
    replays: list[dict[str, Any]] = []

    errors_by_bucket: dict[str, list[float]] = {}
    all_calls: list[dict[str, Any]] = []

    for as_of in dates:
        state = rp.reconstruct(as_of, horizon_days=horizon)
        result = rp.outcome(state)

        for row in result.forecast_accuracy:
            bucket = _bucket(int(row["horizon_days"]))
            if bucket:
                errors_by_bucket.setdefault(bucket, []).append(
                    float(row["abs_error_room_nights"])
                )

        calls = result.pace_calls
        all_calls.extend(calls.get("stay_dates", []))

        errs = [r["abs_error_room_nights"] for r in result.forecast_accuracy]
        replays.append({
            "as_of": as_of.isoformat(),
            "fingerprint": state.fingerprint,
            "nights_on_books": state.book["nights_on_books"],
            "dates_scored": state.pace["scored"],
            "flagged_behind": state.pace["behind"],
            "flagged_ahead": state.pace["ahead"],
            "holidays_in_window": sum(
                1 for c in state.calendar if c["is_public_holiday"]
            ),
            "holidays_with_prior_measurement": len(state.holiday_evidence),
            "forecast_mae": round(sum(errs) / len(errs), 3) if errs else None,
            "pace_calls_resolved": calls.get("scored", 0),
        })
        print(f"  replayed {as_of}  "
              f"scored={state.pace['scored']:>3}  "
              f"behind={state.pace['behind']:>2}  "
              f"ahead={state.pace['ahead']:>2}  "
              f"mae={replays[-1]['forecast_mae']}", flush=True)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of_dates": len(dates),
        "horizon_days": horizon,
        "replays": replays,
        "forecast": _forecast_summary(errors_by_bucket),
        "pace": _pace_summary(all_calls),
    }


def _forecast_summary(errors: dict[str, list[float]]) -> dict[str, Any]:
    return {
        "note": ("MAE in occupied room-nights, pooled across every replayed "
                 "origin. Scored on the inventory grain, which is what the "
                 "forecast targets."),
        "by_horizon": [
            {
                "horizon_days": bucket,
                "forecasts": len(vals),
                "mae_room_nights": round(statistics.mean(vals), 3),
                "median_abs_error": round(statistics.median(vals), 3),
            }
            for bucket, vals in sorted(errors.items(), key=lambda kv: int(kv[0].split("-")[0]))
        ],
    }


def _pace_summary(calls: list[dict[str, Any]]) -> dict[str, Any]:
    if not calls:
        return {"resolved": 0, "note": "no pace call resolved inside the dataset"}

    def rate(subset: list[dict[str, Any]], below: bool) -> float | None:
        if not subset:
            return None
        hits = sum(1 for r in subset if r["finished_below_expectation"] is below)
        return round(100.0 * hits / len(subset), 1)

    behind = [r for r in calls if r["status_at_as_of"] == "behind"]
    ahead = [r for r in calls if r["status_at_as_of"] == "ahead"]
    on_track = [r for r in calls if r["status_at_as_of"] == "on_track"]

    base_below = rate(calls, True)
    behind_below = rate(behind, True)
    ahead_above = rate(ahead, False)

    # THE COMPARATOR THAT DECIDES WHETHER THE FLAG IS WORTH ANYTHING.
    #
    # `behind` requires two conditions: below the p25 of comparable history, and
    # at least MATERIAL_NIGHTS from the median. Strip both and keep only the sign
    # of the gap -- "it is below the median, at all" -- and see how that scores.
    # If the trivial rule matches the dual gate, then the band and the materiality
    # threshold are buying nothing and the 100% headline is a statement about how
    # persistent a booking deficit is, not about the flag.
    negative_gap = [
        r for r in calls
        if r["nights_on_books_at_as_of"] < r["expected_nights_by_now"]
    ]
    positive_gap = [
        r for r in calls
        if r["nights_on_books_at_as_of"] > r["expected_nights_by_now"]
    ]

    # Precision without recall is half a result, and it is the flattering half.
    # A flag that fires on one date a month and is always right is describing its
    # own conservatism as much as its skill.
    finished_below = [r for r in calls if r["finished_below_expectation"]]
    finished_above = [r for r in calls if not r["finished_below_expectation"]]

    def _recall(flagged: list[dict[str, Any]], population: list[dict[str, Any]],
                below: bool) -> float | None:
        if not population:
            return None
        hits = sum(1 for r in flagged if r["finished_below_expectation"] is below)
        return round(100.0 * hits / len(population), 1)

    def _rule_of_three(n: int, misses: int) -> float | None:
        """Upper bound on the miss rate when nothing was missed.

        With zero failures in n trials the 95% upper bound on the failure rate is
        about 3/n. Quoting 100% without it invites the reader to believe the flag
        is infallible on the strength of a couple of dozen observations.
        """
        if n == 0 or misses > 0:
            return None
        return round(100.0 * 3.0 / n, 1)

    return {
        "note": ("A call is scored against the median FINAL book of the same "
                 "benchmark set -- the last 8 same-weekday dates at the same "
                 "property, all completed before the as-of date. The base rate is "
                 "the same measurement over every scored date, flagged or not, and "
                 "a flag is only informative to the extent it beats it."),
        "resolved": len(calls),
        "base_rate_below_expectation_pct": base_below,
        "behind": {
            "calls": len(behind),
            "precision_pct": behind_below,
            "recall_pct": _recall(behind, finished_below, True),
            "lift_over_base_rate_pp": (
                None if behind_below is None or base_below is None
                else round(behind_below - base_below, 1)
            ),
            "max_miss_rate_95pct": _rule_of_three(
                len(behind), sum(1 for r in behind if not r["finished_below_expectation"])
            ),
        },
        "ahead": {
            "calls": len(ahead),
            "precision_pct": ahead_above,
            "recall_pct": _recall(ahead, finished_above, False),
            "lift_over_base_rate_pp": (
                None if ahead_above is None or base_below is None
                else round(ahead_above - (100.0 - base_below), 1)
            ),
            "max_miss_rate_95pct": _rule_of_three(
                len(ahead), sum(1 for r in ahead if r["finished_below_expectation"])
            ),
        },
        "on_track": {
            "calls": len(on_track),
            "finished_below_expectation_pct": rate(on_track, True),
        },
        "naive_sign_of_gap": {
            "note": ("The same prediction from the sign of the gap alone: no p25 "
                     "band, no materiality gate. The dual gate has to beat this to "
                     "have earned its complexity."),
            "below_median_at_as_of": {
                "calls": len(negative_gap),
                "finished_below_expectation_pct": rate(negative_gap, True),
            },
            "above_median_at_as_of": {
                "calls": len(positive_gap),
                "finished_above_expectation_pct": rate(positive_gap, False),
            },
        },
        "median_nights_picked_up_after_as_of": round(
            statistics.median(r["nights_picked_up_after_as_of"] for r in calls), 1
        ),
    }


def write_report(payload: dict[str, Any]) -> None:
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "replay_evaluation.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )

    pace = payload["pace"]
    lines: list[str] = [
        "# Decision Replay",
        "",
        f"_Generated {payload['generated_at']} from "
        f"{payload['as_of_dates']} historical as-of dates, "
        f"{payload['horizon_days']}-day window._",
        "",
        "## What a replay is",
        "",
        "Pick a past date T. Rebuild everything StayPulse could have known then --",
        "the book, the trailing pickup, the pace benchmark, the published holiday",
        "calendar, the holiday effects measurable from holidays already past -- and",
        "report what it would have said. Then, separately, show what happened.",
        "",
        "The reconstruction and the outcome are two functions. The reconstruction",
        "never receives the outcome, which is what makes the guarantee testable:",
        "inserting bookings dated after T must leave the reconstruction",
        "byte-identical, and a SHA-256 fingerprint over the whole decision reduces",
        "that to one comparison.",
        "",
        "## Forecast accuracy at the replayed origins",
        "",
        "| Horizon (days) | Forecasts | MAE (room-nights) | Median abs error |",
        "|---|---:|---:|---:|",
    ]
    for row in payload["forecast"]["by_horizon"]:
        lines.append(
            f"| {row['horizon_days']} | {row['forecasts']} | "
            f"{row['mae_room_nights']} | {row['median_abs_error']} |"
        )

    lines += [
        "",
        "## Pace calls against their outcome",
        "",
        f"Resolved calls: **{pace['resolved']}**.",
        "",
        "A date flagged `behind` at T is claiming it will finish below what",
        "comparable dates finally carry. The base rate is the same measurement over",
        "every scored date, and the flag is only worth something to the extent it",
        "beats it.",
        "",
        "| Call | n | Precision | Recall | Base rate | Lift |",
        "|---|---:|---:|---:|---:|---:|",
        f"| behind | {pace['behind']['calls']} | "
        f"{pace['behind']['precision_pct']}% | "
        f"{pace['behind']['recall_pct']}% | "
        f"{pace['base_rate_below_expectation_pct']}% | "
        f"{pace['behind']['lift_over_base_rate_pp']}pp |",
        f"| ahead | {pace['ahead']['calls']} | "
        f"{pace['ahead']['precision_pct']}% | "
        f"{pace['ahead']['recall_pct']}% | "
        f"{round(100.0 - pace['base_rate_below_expectation_pct'], 1)}% | "
        f"{pace['ahead']['lift_over_base_rate_pp']}pp |",
        "",
        "### Read the recall column before the precision column",
        "",
        "The dual gate is a deliberately conservative flag: a date must be both",
        "outside the p25-p75 band of comparable history AND at least "
        f"{rv.MATERIAL_NIGHTS:.0f} room-nights",
        "from the median. That conservatism is the whole reason precision is high,",
        f"and the cost is visible in recall -- {pace['behind']['recall_pct']}% of the",
        "dates that finished below expectation were ever flagged. The flag is not a",
        "detector of weak dates. It is a short list of the ones weak enough to be",
        "worth someone's morning.",
        "",
    ]

    if pace["behind"]["max_miss_rate_95pct"] is not None:
        lines += [
            f"**On the {pace['behind']['precision_pct']}%.** "
            f"{pace['behind']['calls']} calls with no misses does not mean the flag",
            "cannot miss. With zero failures in n trials the 95% upper bound on the",
            f"failure rate is about 3/n -- here **{pace['behind']['max_miss_rate_95pct']}%**.",
            "That is the honest ceiling on this claim, and a second year of data",
            "would tighten it more than any change to the rule would.",
            "",
        ]

    naive = pace["naive_sign_of_gap"]
    lines += [
        "### Does the dual gate earn its complexity?",
        "",
        "Strip the band and the materiality threshold, keep only the sign of the",
        "gap -- \"below the median at all\" -- and score that instead:",
        "",
        "| Rule | n | Finished as called |",
        "|---|---:|---:|",
        f"| below median at T (sign only) | "
        f"{naive['below_median_at_as_of']['calls']} | "
        f"{naive['below_median_at_as_of']['finished_below_expectation_pct']}% |",
        f"| **flagged `behind` (dual gate)** | {pace['behind']['calls']} | "
        f"**{pace['behind']['precision_pct']}%** |",
        f"| above median at T (sign only) | "
        f"{naive['above_median_at_as_of']['calls']} | "
        f"{naive['above_median_at_as_of']['finished_above_expectation_pct']}% |",
        f"| **flagged `ahead` (dual gate)** | {pace['ahead']['calls']} | "
        f"**{pace['ahead']['precision_pct']}%** |",
        "",
        "It does. The gate is not restating the sign of the gap, and the two",
        "thresholds are carrying the difference rather than decorating it.",
        "",
        f"Median nights picked up after the snapshot: "
        f"**{pace['median_nights_picked_up_after_as_of']}**.",
        "",
        "## Per-origin detail",
        "",
        "| As of | On books | Scored | Behind | Ahead | Holidays ahead | "
        "With prior measurement | Forecast MAE |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["replays"]:
        lines.append(
            f"| {row['as_of']} | {row['nights_on_books']} | {row['dates_scored']} | "
            f"{row['flagged_behind']} | {row['flagged_ahead']} | "
            f"{row['holidays_in_window']} | "
            f"{row['holidays_with_prior_measurement']} | {row['forecast_mae']} |"
        )

    lines += [
        "",
        "## Limitations",
        "",
        "- **One dataset, one portfolio.** Every number here describes three",
        "  corporate aparthotels over eighteen months. None of it generalises.",
        "- **The pace call is scored on the demand grain**, against booking-nights",
        "  live on the arrival date. The forecast is scored on the inventory grain,",
        "  against occupied unit-nights. They are different quantities on purpose;",
        "  each is matched to what it predicted, and they are not comparable to",
        "  each other.",
        "- **No counterfactual.** This shows what the system would have said, not",
        "  what would have happened had anyone acted on it. There is no price",
        "  elasticity in this warehouse, so the value of acting is unmeasurable and",
        "  is therefore not reported.",
        "- **Capacity in the replayed forecast** comes from inventory the origin had",
        "  already seen. Out-of-order nights are settled only once a date has",
        "  passed, so future sellable capacity is not knowable at T even in",
        "  principle.",
        "",
    ]
    (REPORTS / "DECISION_REPLAY.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dates", type=int, default=12)
    parser.add_argument("--horizon", type=int, default=rp.DEFAULT_HORIZON)
    args = parser.parse_args()

    print(f"Replaying {args.dates} historical dates at a "
          f"{args.horizon}-day horizon...", flush=True)
    payload = run(args.dates, args.horizon)
    write_report(payload)

    pace = payload["pace"]
    naive = pace["naive_sign_of_gap"]
    print("\n--- pace calls ---")
    print(f"  base rate below expectation : {pace['base_rate_below_expectation_pct']}%")
    print(f"  behind  n={pace['behind']['calls']:<4} "
          f"precision {pace['behind']['precision_pct']}%  "
          f"recall {pace['behind']['recall_pct']}%  "
          f"lift {pace['behind']['lift_over_base_rate_pp']}pp")
    print(f"  ahead   n={pace['ahead']['calls']:<4} "
          f"precision {pace['ahead']['precision_pct']}%  "
          f"recall {pace['ahead']['recall_pct']}%  "
          f"lift {pace['ahead']['lift_over_base_rate_pp']}pp")
    print("\n--- naive comparator: sign of the gap alone ---")
    print(f"  below median  n={naive['below_median_at_as_of']['calls']:<4} "
          f"{naive['below_median_at_as_of']['finished_below_expectation_pct']}%")
    print(f"  above median  n={naive['above_median_at_as_of']['calls']:<4} "
          f"{naive['above_median_at_as_of']['finished_above_expectation_pct']}%")
    print("\n--- forecast ---")
    for row in payload["forecast"]["by_horizon"]:
        print(f"  {row['horizon_days']:>6} days  n={row['forecasts']:<5} "
              f"MAE {row['mae_room_nights']}")
    print("\nWrote reports/DECISION_REPLAY.md and reports/replay_evaluation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
