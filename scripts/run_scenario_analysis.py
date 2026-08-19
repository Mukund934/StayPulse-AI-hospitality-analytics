"""Publish the scenario engine.

There is no accuracy section in this report, and that is the point. A scenario
predicts nothing, so there is nothing to score. What the report has instead is
the arithmetic, the exact decomposition, and a clear statement of what each
result held constant.

Usage:
    python scripts/run_scenario_analysis.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import scenario as sc  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"


def run() -> dict[str, Any]:
    print("Building scenarios...", flush=True)
    payload = sc.summary()
    payload["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    position = payload["baseline"]
    print(f"  baseline occupancy {position['occupancy_pct']}%  "
          f"ADR {position['adr_inr']}  RevPAR {position['revpar_inr']}", flush=True)
    return payload


def write_report(payload: dict[str, Any]) -> None:
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "scenario.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )

    base = payload["baseline"]
    mix = payload["channel_mix_example"]
    lines: list[str] = [
        "# Scenario engine",
        "",
        f"_Generated {payload['generated_at']}._",
        "",
        "## A scenario is not a forecast",
        "",
        "This is the distinction the whole feature rests on.",
        "",
        "A **forecast** answers *what is going to happen*. It carries a model, it",
        "can be scored against reality, and this project scores it -- with an 80%",
        "interval whose measured out-of-sample coverage is 82.6%.",
        "",
        "A **scenario** answers *what would the books say if occupancy were five",
        "points higher*. It carries no model, predicts nothing, and cannot be right",
        "or wrong. Its entire value is that it is exact.",
        "",
        "Confusing the two is how a what-if tool becomes dishonest: a number",
        "produced by holding ADR fixed and moving occupancy gets presented as a",
        "projection, and the reader assumes someone believes it will happen. Every",
        "result here is labelled `scenario`, states what it held constant, and never",
        "claims the change is achievable. A test scans the output for forecast",
        "vocabulary and fails if it appears.",
        "",
        "## Baseline",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Rooms available | {base['rooms_available']:,} |",
        f"| Rooms sold | {base['rooms_sold']:,} |",
        f"| Occupancy | {base['occupancy_pct']}% |",
        f"| ADR | {base['adr_inr']:,} |",
        f"| RevPAR | {base['revpar_inr']:,} |",
        f"| Room revenue | {base['room_revenue_inr']:,} |",
        f"| Commission | {base['commission_inr']:,} |",
        f"| Net revenue (after commission + GST) | {base['net_revenue_inr']:,} |",
        "",
        "`RevPAR = ADR x Occupancy` holds exactly, and a test asserts it in the",
        "baseline and in every scenario result.",
        "",
        "## Worked examples",
        "",
        "| Scenario | Occupancy | ADR | RevPAR | Change |",
        "|---|---:|---:|---:|---:|",
    ]
    for example in payload["examples"]:
        levers = example["levers"]
        label_parts = []
        if levers["occupancy_pp"]:
            label_parts.append(f"occupancy {levers['occupancy_pp']:+g}pp")
        if levers["adr_pct"]:
            label_parts.append(f"ADR {levers['adr_pct']:+g}%")
        label = " and ".join(label_parts) or "no change"
        result = example["scenario"]
        lines.append(
            f"| {label} | {result['occupancy_pct']}% | {result['adr_inr']:,} | "
            f"{result['revpar_inr']:,} | {example['change']['revpar_inr']:+,} |"
        )

    combined = payload["examples"][2]
    occ_only = payload["examples"][0]["change"]["revpar_inr"]
    adr_only = payload["examples"][1]["change"]["revpar_inr"]
    both = combined["change"]["revpar_inr"]

    lines += [
        "",
        "### The interaction term is real and it is handled",
        "",
        f"Occupancy alone gives {occ_only:+,}. Rate alone gives {adr_only:+,}. Both",
        f"together give {both:+,} -- **not** {occ_only + adr_only:+,}.",
        "",
        f"The difference, {round(both - (occ_only + adr_only), 2):+,}, is the "
        "interaction:",
        "`RevPAR = ADR x Occupancy` is multiplicative, so selling more nights at a",
        "higher rate earns more than the two effects added. An implementation that",
        "adds them is wrong by exactly this amount, and a test fails if it starts",
        "doing so.",
        "",
        "The interaction has to be attributed somewhere. This uses the symmetric",
        "(Shapley) split -- each contribution measured against the *mean* of before",
        "and after -- which is the same convention `analytics.rootcause` already",
        "uses for observed movements. Using a different one would let the two",
        "modules disagree about the same movement.",
        "",
        "| Component | Contribution |",
        "|---|---:|",
        f"| Occupancy | {combined['decomposition']['occupancy_contribution_inr']:+,} |",
        f"| Rate | {combined['decomposition']['rate_contribution_inr']:+,} |",
        f"| **Residual** | **{combined['decomposition']['residual_inr']}** |",
        "",
        "The residual is zero, not small. A test asserts that rather than this",
        "report claiming it.",
        "",
        "## Sensitivity",
        "",
        "| Occupancy change | RevPAR | Change |",
        "|---:|---:|---:|",
    ]
    for row in payload["sensitivity"]["occupancy_pp"]:
        lines.append(
            f"| {row['lever_pp']:+g}pp | {row['revpar_inr']:,} | "
            f"{row['revpar_change_inr']:+,} |"
        )

    lines += [
        "",
        "| ADR change | RevPAR | Change |",
        "|---:|---:|---:|",
    ]
    for row in payload["sensitivity"]["adr_pct"]:
        lines.append(
            f"| {row['lever_pct']:+g}% | {row['revpar_inr']:,} | "
            f"{row['revpar_change_inr']:+,} |"
        )

    lines += [
        "",
        "One lever at a time, each holding the other constant. Reading two rows",
        "together and adding them understates the result, for the reason above.",
        "",
        "## Channel mix -- the one lever with measured economics",
        "",
        "Every other lever here is arithmetic on an identity. This one is priced",
        "from real data: commission per occupied night is **measured**, and it",
        "differs enormously by channel.",
        "",
        "| Channel | Nights | ADR | Commission/night | Net/night |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in sc.channel_economics():
        lines.append(
            f"| {row['channel']} | {row['nights']:,} | {row['adr_inr']:,} | "
            f"{row['commission_per_night_inr']:,} | {row['net_per_night_inr']:,} |"
        )

    lines += [
        "",
        f"**Example.** Moving {mix['lever']['share_pct']:g}% of "
        f"{mix['lever']['from_channel']} nights to "
        f"{mix['lever']['to_channel']} -- "
        f"{mix['lever']['nights_moved']:,} nights -- changes net revenue by "
        f"**{mix['change']['net_revenue_inr']:+,}**, or "
        f"{mix['change']['net_per_night_inr']:+,} per night.",
        "",
        "Almost all of that is commission, not rate: the two channels charge",
        "similar ADR, but one pays the OTA and one does not.",
        "",
        "### Why this is still a scenario and not a plan",
        "",
        "It assumes **the demand transfers** -- that a guest who booked through an",
        "OTA would have booked direct if the OTA had not been there. Nothing in",
        "this warehouse supports that, and for some channels it is plainly false: a",
        "walk-in is a walk-in because they walked in.",
        "",
        "It also excludes **acquisition cost**. Commission is recorded; the",
        "marketing spend needed to move a booking direct is not. The saving shown",
        "is gross of whatever it would cost to achieve, which for a real shift of",
        "this size would not be nothing.",
        "",
        "## What this engine cannot do",
        "",
    ]
    for limitation in payload["what_this_cannot_do"]:
        lines.append(f"- {limitation}")

    lines += [
        "",
        "This is the same gap that stops `opportunity_signals` naming a price and",
        "stops the overbooking simulator naming a level. There is no price",
        "elasticity and no demand response in this warehouse, so the engine can say",
        "what the books would show, and cannot say how to get there or whether the",
        "revenue is capturable.",
        "",
    ]
    (REPORTS / "SCENARIO.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    payload = run()
    write_report(payload)
    print("\nWrote reports/SCENARIO.md and reports/scenario.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
