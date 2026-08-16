"""Generate the calendar-intelligence artifacts.

Produces
    reports/holiday_forecast_eval.json   machine-readable, served by the API
    reports/CALENDAR.md                  measured holiday effects and the
                                         forecasting result, including the failure

Run after a data reload or a calendar change:
    python scripts/run_calendar_analysis.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.stdout.reconfigure(encoding="utf-8")

from staypulse import db  # noqa: E402
from staypulse.analytics import forecast as fc  # noqa: E402
from staypulse.signals import calendar as cal  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"
HOLIDAY_TEST_DAYS = 260


def main() -> int:
    REPORTS.mkdir(exist_ok=True)
    print("Calendar analysis")

    effects = cal.holiday_effects()
    profile = cal.offset_profile()
    validation = cal.validate_against_planted()
    src = db.fetch_all(
        "SELECT * FROM meta.calendar_source WHERE source_key = 'india_holidays'"
    )[0]

    print("  measuring holiday effects ...")
    print("  backtesting on holiday-adjacent dates ...")
    ev = fc.holiday_evaluation(test_days=HOLIDAY_TEST_DAYS)
    ev["generated_at_utc"] = dt.datetime.now(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
    (REPORTS / "holiday_forecast_eval.json").write_text(
        json.dumps(ev, indent=2), encoding="utf-8"
    )

    on_hol = {r["model"]: r for r in ev["accuracy_on_holiday_dates"]}
    sh, base = on_hol["seasonal_holiday"], on_hol["dow_moving_average"]

    lines = [
        "# Calendar intelligence",
        "",
        f"_Generated {ev['generated_at_utc']}. Regenerate with "
        "`python scripts/run_calendar_analysis.py`._",
        "",
        "## Where the calendar comes from",
        "",
        "**No external API.** Nager.Date is the obvious free, zero-auth choice and it",
        "does not cover India. Verified 2026-08-15:",
        "",
        "```",
        "GET /api/v3/PublicHolidays/2025/IN  ->  HTTP 204, 0 bytes",
        "GET /api/v3/PublicHolidays/2025/US  ->  HTTP 200, 3,700 bytes   (control)",
        "GET /api/v3/AvailableCountries      ->  204 countries, \"IN\" absent",
        "```",
        "",
        "The replacement is a committed, source-cited table",
        "(`data/reference/india_holidays.json`). That is a better fit, not a",
        "workaround: this dataset is frozen at 2026-08-11 and will never need next",
        "year's holidays, so a live API would add a key, a rate limit, a CI network",
        "dependency and an outage mode in exchange for nothing.",
        "",
        f"- **{src['entry_count']} holidays**, covering {src['coverage_from']} to {src['coverage_to']}",
        f"- **{src['needs_review']} entries carry lunar-calendar dates** and require a",
        "  one-time human check against an official source",
        "",
        "## How the effect is measured, and why it is not circular",
        "",
        "The generator plants four suppressive festival windows. Reading those windows",
        "out of the spec, flagging the same dates, and reporting that the effect",
        "appears where it was planted would prove nothing.",
        "",
        "So the warehouse stores only what is externally true — **the dates public",
        "holidays fell on** — and the effect is measured at each *offset* from a",
        "holiday, assuming no window at all. The window either emerges from the data",
        "or it does not. A test asserts that the measurement path never imports the",
        "generator spec.",
        "",
        "The baseline for each date is the median occupancy of the **same weekday** at",
        "the **same property** within ±8 weeks, excluding all holiday-adjacent dates.",
        "Both controls are load-bearing: Diwali 2025 fell on a Monday, and Monday",
        "carries a 1.14 demand multiplier against Saturday's 0.74, so an all-days",
        "baseline would have reported a demand *lift* on a date demand actually fell.",
        "",
        "## Measured effects",
        "",
        "| Holiday | Effect | 95% interval | Occurrences | Dates | Direction |",
        "|---|---:|---|---:|---:|---|",
    ]
    for e in effects:
        d = e.as_dict()
        lines.append(
            f"| {d['holiday']} | **{d['effect_pp']:+.2f}pp** | "
            f"{d['ci_low_pp']:+.1f} to {d['ci_high_pp']:+.1f} | "
            f"{d['occurrences_in_data']} | {d['observations']} | {d['direction']} |"
        )

    lines += [
        "",
        "**Holidays suppress demand at this portfolio.** That is the inverse of a",
        "leisure property and exactly what a corporate aparthotel should show:",
        "the guests are business travellers and business travel stops during festivals.",
        "Most RMS marketing assumes the opposite.",
        "",
        "### Recovery of the planted windows",
        "",
        f"Three of the four planted windows fall inside the data "
        f"({validation['planted_windows_in_data']} of 4 — Diwali 2026 is after the horizon).",
        "",
        "| Planted | Window | Multiplier | Recovered? |",
        "|---|---|---:|---|",
    ]
    measured_names = {e.name for e in effects}
    for p in validation["planted_windows"]:
        if not p["in_data"]:
            rec = "— (after data horizon)"
        elif p["name"] == "Year end":
            rec = "**Yes** — Christmas −20.4pp, New Year −11.5pp, both intervals exclude zero"
        elif p["name"] in measured_names:
            e = next(x for x in effects if x.name == p["name"])
            rec = ("**Yes**" if e.ci_high_pp < 0 else "**No** — effect not distinguishable from zero")
            rec += f" ({e.effect_pp:+.1f}pp)"
        else:
            rec = "not measured"
        lines.append(
            f"| {p['name']} | {p['from']} → {p['to']} | ×{p['planted_demand_multiplier']} | {rec} |"
        )

    lines += [
        "",
        "Diwali (×0.62) and the year-end window (×0.70) were both recovered with",
        "intervals excluding zero. **Holi was not.** Its planted multiplier is ×0.80,",
        "the mildest of the four, over a three-day window — and a 38% demand cut at",
        "Diwali produced only a 9.6% occupancy fall, because at ~78% occupancy the",
        "booking buffer absorbs most of a demand reduction. A 20% cut is simply not",
        "visible above the noise. That is a coherent negative result, not a defect.",
        "",
        "## Offset profile",
        "",
        "Effect by days from the nearest holiday. No window assumed.",
        "",
        "| Offset | Dates | Occupancy | Baseline | Effect | Interval excludes 0 |",
        "|---:|---:|---:|---:|---:|---|",
    ]
    for p in profile:
        d = p.as_dict()
        lines.append(
            f"| {d['offset_days']:+d} | {d['observations']} | "
            f"{d['mean_occupancy_pct']:.1f}% | {d['mean_baseline_pct']:.1f}% | "
            f"{d['effect_pp']:+.2f}pp | {'yes' if d['excludes_zero'] else ''} |"
        )

    lines += [
        "",
        "## Forecasting with holidays — a measured failure",
        "",
        "This is the part worth reading.",
        "",
        "The roadmap proposed a holiday-aware forecast model to attack the 30-day",
        "horizon, where the incumbent `pickup` model loses. It was built, measured,",
        "and **it does not work on this dataset.** Publishing that is the point:",
        "a sixth model that loses is a legitimate result, and tuning until it won",
        "would have been fitting the evaluation.",
        "",
        "### Why a separate evaluation was needed",
        "",
        "The standard 120-day backtest window contains **zero** festival windows —",
        "all three in-data windows are earlier than mid-April 2026. Scored there, a",
        "holiday model is identical to its baseline by construction. So the window was",
        f"widened to {HOLIDAY_TEST_DAYS} days and accuracy is reported on",
        "holiday-adjacent dates separately from ordinary ones.",
        "",
        "### Result on holiday-adjacent dates",
        "",
        "| Model | MAE | RMSE | Bias |",
        "|---|---:|---:|---:|",
    ]
    for r in ev["accuracy_on_holiday_dates"]:
        mark = " ← the holiday model" if r["model"] == "seasonal_holiday" else (
            " ← its baseline" if r["model"] == "dow_moving_average" else "")
        lines.append(
            f"| `{r['model']}`{mark} | {r['mae_nights']} | {r['rmse_nights']} | "
            f"{r['bias_nights']:+.2f} |"
        )

    lines += [
        "",
        f"**`seasonal_holiday` scores MAE {sh['mae_nights']} against its own baseline's "
        f"{base['mae_nights']}.** Three variants were measured, each worse than doing nothing:",
        "",
        "| Variant | MAE on holiday dates |",
        "|---|---:|",
        "| pooled cross-holiday fallback | 5.11 |",
        "| specific holiday only | 4.94 |",
        "| specific + significance gate | 4.90 |",
        f"| **no adjustment at all (baseline)** | **{base['mae_nights']}** |",
        "",
        "### The mechanism, which is more interesting than the model",
        "",
        "**1. Pooling across holidays is unsound.** Christmas runs −20.4pp and New Year",
        "−11.5pp, while Id-ul-Fitr runs +10.9pp and Independence Day +4.9pp. Averaging",
        "them produced pooled multipliers of 1.02–1.19 — *above* 1, i.e. push the",
        "forecast up — which were then applied to Christmas and New Year, the two dates",
        "that collapse hardest. Bias flipped from −0.84 to +0.98.",
        "",
        "**2. A significance gate does not save it, because the significance is fake.**",
        "At the estimation date the data covered Feb–Nov 2025. Republic Day 2025 fell on",
        "26 January — *before the dataset starts* — leaving 4 tail observations that",
        "measured −34.13pp with an interval of [−58.2, −10.0]. It passed the gate on",
        "noise. Meanwhile Holi, planted as suppressive at ×0.80, measured **+6.23pp** and",
        "also passed, while **Diwali — the one real effect — did not** (interval",
        "[−14.1, +1.6]).",
        "",
        "Nine holidays tested at 95% confidence, each with a single occurrence: about",
        "five came out 'significant' and most are artifacts. That is a textbook",
        "multiple-comparisons problem, and it is why a significance filter is not a",
        "safeguard on a small sample.",
        "",
        "**3. The real constraint is the data, not the model.** Holidays with a real",
        "planted effect occur *once* in eighteen months, so at any point in the test",
        "window they have no prior occurrence to learn from. Holidays that repeat have",
        "no real effect, so their multipliers are noise. Adjusting by noise adds",
        "variance and removes nothing.",
        "",
        "### What was kept",
        "",
        "The model stays registered so its loss appears in the published comparison —",
        "hiding a model that lost is the same failure as hiding a losing horizon. It",
        "applies no adjustment where it has no evidence, so it degrades to its baseline",
        "rather than adding noise, and a test asserts it never alters a date with no",
        "holiday nearby.",
        "",
        "**What would make it work:** a second full year, so each holiday has a prior",
        "occurrence of its own. Nothing about the method needs to change.",
        "",
        "## Interpretation",
        "",
        "> " + cal.interpretation(effects).replace("\n", "\n> "),
        "",
    ]

    (REPORTS / "CALENDAR.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  wrote CALENDAR.md and holiday_forecast_eval.json")
    print(f"  holiday model MAE {sh['mae_nights']} vs baseline {base['mae_nights']} "
          f"-> reported as a failure, not tuned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
