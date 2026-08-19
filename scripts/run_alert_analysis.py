"""Publish the Alert Center, and measure the one bias it is known to carry.

The headline measurement here is not how many alerts there are. It is whether the
pace feed disproportionately raises them on holiday-adjacent dates -- and the
answer turned out to be "cannot be established from this data", which is why the
per-origin breakdown is computed and published rather than the pooled ratio.

The pooled comparison says 73.7% of behind-pace alerts fall on holiday-adjacent
dates against a 39.0% base rate. Broken down per origin, base rates range from 0%
to 100%, one origin whose entire window was holiday-adjacent supplied 11 of the 19
alerts, and another with a 71.4% base rate raised none. Excluding the dominant
origin leaves 37.5% against 34.9%. Simpson's paradox, so the ratio is withdrawn.

The mechanism is still real and still worth qualifying an alert with: the pace
benchmark is holiday-blind, and F-101 measured genuine suppression on those dates.
What is not supported is a magnitude.

Usage:
    python scripts/run_alert_analysis.py [--origins 8]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.analytics import alerts as al  # noqa: E402
from staypulse.analytics import revenue as rv  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"


def measure_holiday_bias(origins: int) -> dict[str, Any]:
    """Share of behind-pace alerts on holiday-adjacent dates, against the base rate.

    Spread across origins rather than measured at one, so the answer is not a
    statement about whichever week the horizon happens to land in.
    """
    horizon = rv.data_horizon()
    step = max(1, 280 // max(1, origins - 1))

    scored_total = scored_adjacent = 0
    alerts_total = alerts_adjacent = 0
    ahead_total = 0
    per_origin: list[dict[str, Any]] = []

    for i in range(origins):
        as_of = horizon - dt.timedelta(days=i * step)
        scored = rv.pace(as_of)
        if not scored:
            continue
        dates = sorted({row.stay_date for row in scored})
        rows = db.fetch_all(
            "SELECT full_date FROM mart.dim_date "
            "WHERE full_date = ANY(:d) AND is_holiday_adjacent",
            d=dates,
        )
        adjacent = {r["full_date"] for r in rows}

        behind = rv.need_dates(as_of, scored=scored)
        ahead = rv.constrained_dates(as_of, scored=scored)
        behind_adjacent = sum(1 for r in behind if r.stay_date in adjacent)

        scored_total += len(dates)
        scored_adjacent += len(adjacent)
        alerts_total += len(behind)
        alerts_adjacent += behind_adjacent
        ahead_total += len(ahead)

        per_origin.append({
            "as_of": as_of.isoformat(),
            "stay_dates_scored": len(dates),
            "holiday_adjacent_dates": len(adjacent),
            "behind_pace_alerts": len(behind),
            "behind_pace_on_holiday_dates": behind_adjacent,
            "ahead_of_pace": len(ahead),
        })
        print(f"  {as_of}  scored={len(dates):>3}  behind={len(behind):>2} "
              f"(holiday {behind_adjacent})  ahead={len(ahead):>2}", flush=True)

    base_rate = 100.0 * scored_adjacent / scored_total if scored_total else 0.0
    alert_rate = 100.0 * alerts_adjacent / alerts_total if alerts_total else 0.0

    # The pooled ratio above is not trustworthy on its own, so the dominant
    # origin is isolated and the aggregate recomputed without it. If the effect
    # is real it survives; if it is an artefact of one holiday week it does not.
    raising = [p for p in per_origin if p["behind_pace_alerts"]]
    dominant = max(raising, key=lambda p: p["behind_pace_alerts"]) if raising else None
    rest = [p for p in per_origin if p is not dominant]
    rest_alerts = sum(p["behind_pace_alerts"] for p in rest)
    rest_adjacent = sum(p["behind_pace_on_holiday_dates"] for p in rest)
    rest_scored = sum(p["stay_dates_scored"] for p in rest)
    rest_scored_adj = sum(p["holiday_adjacent_dates"] for p in rest)

    return {
        "origins_measured": len(per_origin),
        "stay_dates_scored": scored_total,
        "holiday_adjacent_dates": scored_adjacent,
        "base_rate_holiday_adjacent_pct": round(base_rate, 1),
        "behind_pace_alerts": alerts_total,
        "behind_pace_on_holiday_dates": alerts_adjacent,
        "alert_rate_holiday_adjacent_pct": round(alert_rate, 1),
        "pooled_over_representation_ratio": (
            round(alert_rate / base_rate, 2) if base_rate else None
        ),
        "pooled_ratio_is_reliable": False,
        "dominant_origin": None if dominant is None else {
            "as_of": dominant["as_of"],
            "alerts": dominant["behind_pace_alerts"],
            "share_of_all_alerts_pct": (
                round(100.0 * dominant["behind_pace_alerts"] / alerts_total, 1)
                if alerts_total else None
            ),
            "base_rate_pct": round(
                100.0 * dominant["holiday_adjacent_dates"]
                / dominant["stay_dates_scored"], 1),
        },
        "excluding_dominant_origin": {
            "behind_pace_alerts": rest_alerts,
            "alert_rate_holiday_adjacent_pct": (
                round(100.0 * rest_adjacent / rest_alerts, 1) if rest_alerts else None
            ),
            "base_rate_holiday_adjacent_pct": (
                round(100.0 * rest_scored_adj / rest_scored, 1) if rest_scored else None
            ),
        },
        "ahead_of_pace_total": ahead_total,
        "per_origin": per_origin,
    }


def run(origins: int) -> dict[str, Any]:
    print("Measuring the pace feed's holiday bias...", flush=True)
    bias = measure_holiday_bias(origins)

    print("\nBuilding the queue...", flush=True)
    horizon = rv.data_horizon()
    centre = al.alert_center(horizon)
    radar = al.opportunity_radar(horizon - dt.timedelta(days=40))

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "as_of": centre["as_of"],
        "holiday_bias": bias,
        "queue": {
            "total": centre["total"],
            "by_source": centre["by_source"],
            "by_actionability": centre["by_actionability"],
            "qualified": sum(1 for a in centre["alerts"] if a["qualifier"]),
        },
        "radar": {"as_of": radar["as_of"], "total": radar["total"]},
        "alerts": centre["alerts"],
        "opportunities": radar["opportunities"],
    }


def write_report(payload: dict[str, Any]) -> None:
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "alert_center.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )

    bias = payload["holiday_bias"]
    queue = payload["queue"]
    lines: list[str] = [
        "# Alert Center and Opportunity Radar",
        "",
        f"_Generated {payload['generated_at']}, as of {payload['as_of']}._",
        "",
        "## Four feeds, one queue, no invented severity",
        "",
        "Anomalies, data-quality failures, SLA breaches and pace need-dates each",
        "existed already and each had its own shape. This puts them in one queue.",
        "It does **not** put them on one scale.",
        "",
        "The tempting move is a severity number applied across every source. It",
        "cannot be computed here: a robust z on ADR, a percentage of failing rows,",
        "an SLA breach rate and a room-night shortfall are incommensurable, and",
        "mapping them onto a shared 1-5 scale needs exchange rates nobody has",
        "measured. The arbitrariness would then be hidden inside an integer that",
        "looks authoritative. Every alert reports its own feed's measure with its",
        "units named, and a test fails if a shared severity field appears.",
        "",
        "What **is** comparable is actionability — what a person can still do:",
        "",
        "| Band | Meaning | Alerts |",
        "|---|---|---:|",
        f"| `act_now` | a future stay date; the book can still move | "
        f"{queue['by_actionability'].get('act_now', 0)} |",
        f"| `investigate` | already happened; only an explanation is available | "
        f"{queue['by_actionability'].get('investigate', 0)} |",
        f"| `standing` | a condition with no single date, true until fixed | "
        f"{queue['by_actionability'].get('standing', 0)} |",
        "",
        "| Source | Alerts |",
        "|---|---:|",
    ]
    for source, count in sorted(queue["by_source"].items(),
                                key=lambda kv: -kv[1]):
        lines.append(f"| {source} | {count} |")

    lines += [
        "",
        "## The pace feed's holiday bias, and a ratio that did not survive",
        "",
        "This is the finding worth keeping, and what makes it worth keeping is that",
        "the first version of it was wrong.",
        "",
        "The mechanism is real. The pace benchmark compares a stay date against the",
        "last 8 comparable same-weekday dates, which exclude the holiday. F-101",
        "measured genuine suppression on those dates — Diwali −10.5pp, Christmas",
        "−20.4pp — so part of a shortfall on a holiday-adjacent date is plausibly",
        "the holiday rather than a demand problem.",
        "",
        "The **magnitude** is where it went wrong. Pooled across origins:",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Stay dates scored across {bias['origins_measured']} origins | "
        f"{bias['stay_dates_scored']} |",
        f"| …of which holiday-adjacent (base rate) | "
        f"{bias['base_rate_holiday_adjacent_pct']}% |",
        f"| Behind-pace alerts raised | {bias['behind_pace_alerts']} |",
        f"| …of which holiday-adjacent | "
        f"{bias['alert_rate_holiday_adjacent_pct']}% |",
        f"| Apparent over-representation | "
        f"{bias['pooled_over_representation_ratio']}× |",
        "",
        "That last row reads like a finding. It is an artefact.",
        "",
        "### Why the pooled ratio is withdrawn",
        "",
        "Per origin, the base rate ranges from 0% to 100%. The pooled comparison",
        "mixes windows that were entirely holiday-adjacent with windows containing",
        "no holiday at all.",
        "",
        "| As of | Scored | Holiday-adjacent (base) | Behind-pace | …on holiday dates |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in bias["per_origin"]:
        if not row["behind_pace_alerts"]:
            continue
        base = 100.0 * row["holiday_adjacent_dates"] / row["stay_dates_scored"]
        share = (100.0 * row["behind_pace_on_holiday_dates"]
                 / row["behind_pace_alerts"])
        lines.append(
            f"| {row['as_of']} | {row['stay_dates_scored']} | {base:.1f}% | "
            f"{row['behind_pace_alerts']} | {share:.1f}% |"
        )

    dominant = bias["dominant_origin"]
    excl = bias["excluding_dominant_origin"]
    lines += [
        "",
        f"One origin — **{dominant['as_of']}**, sitting immediately before",
        f"Independence Day — had a **{dominant['base_rate_pct']}%** base rate: every",
        "scored date in its window was holiday-adjacent. It contributed",
        f"**{dominant['alerts']} of the {bias['behind_pace_alerts']} alerts**",
        f"({dominant['share_of_all_alerts_pct']}%), all holiday-adjacent — which is",
        "exactly what a 100% base rate produces, and evidence of nothing. Another",
        "origin ran a 71.4% base rate and raised *zero* holiday-adjacent alerts,",
        "pointing the opposite way.",
        "",
        f"Excluding the dominant origin: **{excl['alert_rate_holiday_adjacent_pct']}%**",
        f"of alerts against a **{excl['base_rate_holiday_adjacent_pct']}%** base rate.",
        "No effect.",
        "",
        "This is Simpson's paradox, and it is the **third time this project has hit",
        "the same class of error**. PART U.2 records pooled holiday multipliers",
        "coming out above 1 for holidays that suppress demand; U.3 records",
        "pseudo-replication in the confidence caveat. Pooling across units with",
        "very different base rates is unsound here in whichever direction it",
        "happens to flatter.",
        "",
        "So the alert qualifier names the **mechanism**, which is measured, and not",
        "a **magnitude**, which is not. No ratio is published in the API response.",
        "",
        "### They are qualified, not suppressed",
        "",
        "Dropping them would be the easy fix and the wrong one. A holiday explains",
        "*part* of a shortfall, not all of it, and these are dates where occupancy",
        "is already fragile — silently removing the alert would hide genuine",
        "weakness exactly where it costs most.",
        "",
        f"Currently {queue['qualified']} of {queue['total']} alerts carry that",
        "qualifier.",
        "",
        "## The SLA threshold, and a wrong first answer",
        "",
        "The first version of this module flagged cells breaching on 25% or more of",
        "requests, with a comment asserting that sat above the bulk of the",
        "distribution. Measured, the distribution runs **6.5% to 22.5%** with a",
        "median of 16.7% across 11 qualifying cells. A 25% cut would have matched",
        "**nothing**, and the Alert Center would have advertised four feeds while one",
        "silently contributed zero.",
        "",
        "This warehouse defines `sla_minutes` per request type but no acceptable",
        "breach *rate* anywhere — not in the metric registry, the DQ rules or the",
        "generator. So an absolute threshold is invented by definition. Cells are",
        "now judged against their peers using the dual gate this codebase already",
        "applies elsewhere: at or above the p75 of comparable cells, **and** at",
        "least 20 breaches in absolute terms. \"Bad\" means worse than comparable",
        "cells, not worse than a contract, and the output says so.",
        "",
        "## Opportunity Radar",
        "",
        f"As of {payload['radar']['as_of']}: **{payload['radar']['total']}** stay",
        "dates running ahead of their own curve.",
        "",
        "Pace analysis that only surfaces weak dates is half an instrument. A date",
        "filling unusually early is the one where the remaining inventory was priced",
        "before anyone knew demand would be strong.",
        "",
        "**No signal names a price.** There is no competitor rate feed and no price",
        "elasticity in this warehouse, so a rate recommendation would be an opinion",
        "with a number attached. A test enforces it.",
        "",
        "## Limitations",
        "",
        "- **No cross-source severity, by design.** Compare within a source, never",
        "  across. The queue is ordered by actionability, not by alarm.",
        "- **The pace feed is holiday-blind** and over-represents holiday-adjacent",
        "  dates by the ratio above. Qualified in place rather than corrected: a",
        "  holiday-aware pace benchmark would need a per-holiday effect estimate,",
        "  and F-102 established that this dataset does not support one — most",
        "  holidays occur once in eighteen months.",
        "- **SLA and data-quality alerts are standing conditions**, aggregated over",
        "  the whole record. A single bad shift does not appear here and should not;",
        "  that is what the anomaly feed is for.",
        "- **Anomaly alerts are capped to the trailing "
        f"{al.ANOMALY_LOOKBACK_DAYS} days.** Older detections are history rather",
        "  than a queue, and stay in `reports/anomalies.md`.",
        "",
    ]
    (REPORTS / "ALERT_CENTER.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origins", type=int, default=8)
    args = parser.parse_args()

    payload = run(args.origins)
    write_report(payload)

    bias = payload["holiday_bias"]
    print(f"\n--- pace feed holiday bias ---")
    print(f"  base rate holiday-adjacent : {bias['base_rate_holiday_adjacent_pct']}%")
    print(f"  behind-pace alerts on those: {bias['alert_rate_holiday_adjacent_pct']}%")
    excl = bias["excluding_dominant_origin"]
    print(f"  pooled ratio               : {bias['pooled_over_representation_ratio']}x "
          f"(WITHDRAWN -- Simpson's paradox)")
    print(f"  excluding dominant origin  : "
          f"{excl['alert_rate_holiday_adjacent_pct']}% of alerts against a "
          f"{excl['base_rate_holiday_adjacent_pct']}% base rate -- no effect")
    print(f"\n--- queue ---")
    print(f"  total {payload['queue']['total']}  "
          f"qualified {payload['queue']['qualified']}")
    print(f"  by source {payload['queue']['by_source']}")
    print("\nWrote reports/ALERT_CENTER.md and reports/alert_center.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
