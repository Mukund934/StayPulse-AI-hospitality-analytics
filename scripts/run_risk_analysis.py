"""Publish the cancellation risk model and the overbooking simulator.

Two features, two very different kinds of claim, and the report keeps them apart.

The cancellation model claims to predict. Everything about it is reported out of
sample, against a temporal split, with the base rate beside every figure -- and
its coefficients are checked against the mechanism the generator actually
planted, which is the closest thing to ground truth this project will ever have.

The overbooking simulator claims only arithmetic. It computes the outcome
distribution at every level and the breakeven cost ratio, and it stops there,
because the cost of walking a guest does not exist in this warehouse.

Usage:
    python scripts/run_risk_analysis.py
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import cancellation as cx  # noqa: E402
from staypulse.analytics import overbooking as ob  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"


def run() -> dict[str, Any]:
    print("Fitting the cancellation model...", flush=True)
    cancellation = cx.summary()
    evaluation = cancellation["evaluation"]
    print(f"  AUC {evaluation['discrimination']['auc']}  "
          f"lift {evaluation['classification_at_threshold']['lift_over_base_rate']}  "
          f"calibration "
          f"{evaluation['calibration']['weighted_mean_absolute_error_pp']}pp",
          flush=True)

    print("Simulating overbooking...", flush=True)
    overbooking = ob.summary()
    print(f"  example {overbooking['example']['stay_date']}: "
          f"{overbooking['example']['on_books']} on books, "
          f"capacity {overbooking['example']['capacity']}", flush=True)

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "cancellation": cancellation,
        "overbooking": overbooking,
    }


def write_report(payload: dict[str, Any]) -> None:
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "risk.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )

    model = payload["cancellation"]
    ev = model["evaluation"]
    truth = model["ground_truth"]
    drift = model["temporal_drift"]
    noshow = model["no_show"]
    over = payload["overbooking"]
    example = over["example"]
    times = "x"

    lines: list[str] = [
        "# Cancellation risk and overbooking",
        "",
        f"_Generated {payload['generated_at']}._",
        "",
        "## What is predicted, and what is not",
        "",
        "The obvious target is **wash** -- cancelled or no-show -- because that is",
        "the number an overbooking policy consumes. It is the wrong thing to model,",
        "and the generator says why.",
        "",
        "Cancellation has a mechanism:",
        "",
        "```",
        "p_cancel = clip(channel.cancel_rate * (1 + 0.55*tanh((lead_days-10)/14)),",
        "                0.01, 0.62)",
        "```",
        "",
        "No-show does not:",
        "",
        "```",
        "elif rng.random() < 0.014",
        "```",
        "",
        "a flat 1.4% applied to every booking that survived the cancellation draw,",
        "independent of channel, lead time, price and length of stay. **No model can",
        "predict it.** Pooling the two would dilute a real signal with a constant, so",
        "this models cancellation and demonstrates the unlearnability of no-show",
        "rather than asserting it.",
        "",
        f"Fitted on the same features, no-show scores **AUC {noshow['auc']}** -- a",
        f"coin toss -- against an observed rate of {noshow['no_show_rate_pct']}%",
        f"versus the planted {noshow['generator_constant_pct']}%. That is the correct",
        "result, not a modelling failure.",
        "",
        "## Cancellation model",
        "",
        f"Logistic regression, L2, {model['model']['features']} booking-time features.",
        "",
        f"- **Split:** temporal -- train on bookings made before "
        f"{ev['split']['split_date']} ({ev['split']['train_bookings']} bookings), "
        f"test on the {ev['split']['test_bookings']} made after.",
        f"- **Base rate:** {ev['base_rate_pct']}%",
        f"- **AUC:** {ev['discrimination']['auc']}",
        f"- **Precision:** {ev['classification_at_threshold']['precision_pct']}% at a "
        f"threshold equal to the base rate",
        f"- **Recall:** {ev['classification_at_threshold']['recall_pct']}%",
        f"- **Lift over base rate:** "
        f"{ev['classification_at_threshold']['lift_over_base_rate']}{times}",
        f"- **Brier score:** {ev['brier_score']}",
        "",
        "**Accuracy is deliberately not reported.** On this base rate a model that",
        "predicts 'never cancels' is around 88% accurate and completely useless.",
        "",
        "### Calibration",
        "",
        f"Weighted mean absolute error: "
        f"**{ev['calibration']['weighted_mean_absolute_error_pp']}pp**.",
        "",
        "| Predicted band | Bookings | Mean predicted | Observed | Gap |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in ev["calibration"]["bins"]:
        lines.append(
            f"| {row['bin']} | {row['bookings']} | {row['mean_predicted_pct']}% | "
            f"{row['observed_pct']}% | {row['gap_pp']:+}pp |"
        )

    lines += [
        "",
        "**The weighting is not a detail.** An unweighted mean over these bins",
        "reports 9.13pp, because a bin holding a single booking counts as much as",
        "one holding 344. Weighted by population it is 2.03pp. This is the same",
        "failure recorded in PART L-14 -- an average over units with very different",
        "weights is not a summary of those units -- caught here in a metric rather",
        "than in a finding.",
        "",
        "The upper bins do drift: the model over-predicts above roughly 27%. That is",
        "the temporal drift below arriving as miscalibration.",
        "",
        "### Recovery of the planted mechanism",
        "",
        "The generator's mechanism is known, so recovery is checkable -- the closest",
        "thing to ground truth this project has.",
        "",
        f"- **Lead time:** expected {truth['lead_time']['expected']}, recovered "
        f"**{truth['lead_time']['net_direction']}**",
        f"- **Channel ordering:** Spearman "
        f"**{truth['channel_ranking']['spearman']}** against the planted rates",
        "",
        "| | Ordering |",
        "|---|---|",
        f"| Planted | {' > '.join(truth['channel_ranking']['planted'])} |",
        f"| Recovered | {' > '.join(truth['channel_ranking']['recovered'])} |",
        "",
        "The channel coefficients are relative to the dropped dummy level, which",
        "sits in the intercept, so their absolute values are not the planted rates.",
        "The **ordering** is the testable claim.",
        "",
        "### Temporal drift, and why the split is temporal",
        "",
        f"Cancellation falls from **{drift['first_quarter_pct']}%** to",
        f"**{drift['last_quarter_pct']}%** across the record "
        f"({drift['change_pp']:+}pp).",
        "",
        "| Booking quarter | Bookings | Cancel rate |",
        "|---|---:|---:|",
    ]
    for row in drift["quarters"]:
        lines.append(
            f"| {row['quarter']} | {row['bookings']} | {row['cancel_rate_pct']}% |"
        )

    lines += [
        "",
        "A model fitted on earlier bookings therefore meets a lower base rate when",
        "scored on later ones and over-predicts by construction. **A random split",
        "would mix the eras and hide this entirely**, reporting a calibration the",
        "model does not have when used the way it would actually be used --",
        "forwards.",
        "",
        "## Overbooking simulator",
        "",
        "### The number this refuses to produce",
        "",
        "Every overbooking treatment ends with an optimal level, and it is always",
        "the same arithmetic: accept one more booking while the expected cost of the",
        "extra walk is below the expected cost of the extra empty room. That needs a",
        "**cost ratio** -- the cost of walking a guest relative to an empty room --",
        "and this warehouse does not contain one. There is no relocation cost, no",
        "compensation field, no goodwill model. `walk_in` is a booking channel and",
        "`relocation` is a guest segment; neither is the cost of a walk.",
        "",
        "So no level is recommended. What is computed is the outcome distribution at",
        "every level, and the **breakeven ratio** at which each level starts to pay",
        "-- both fully determined by the data.",
        "",
        f"### Example: {example['stay_date']}, as of {example['as_of']}",
        "",
        f"{example['on_books']} bookings on the books against "
        f"{example['capacity']} sellable rooms, "
        f"{example['survival_rate_pct']}% survival.",
        "",
        "The date is **chosen, not fixed**: most dates in this portfolio are",
        "undersold, and on an undersold date every overbooking level is walk-free,",
        "which demonstrates nothing.",
        "",
        "| Overbook by | Accepted | E[arrivals] | P(any walk) | E[walks] | "
        "E[empty] | Breakeven ratio |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for level in example["levels"]:
        breakeven = level["breakeven_cost_ratio"]
        lines.append(
            f"| {level['overbook_by']} | {level['bookings_accepted']} | "
            f"{level['expected_arrivals']} | "
            f"{level['probability_of_walking_anyone_pct']}% | "
            f"{level['expected_walks']} | {level['expected_empty_rooms']} | "
            f"{breakeven if breakeven else 'n/a'} |"
        )

    lines += [
        "",
        "Read the breakeven column as: *overbooking by this much pays off only if",
        "walking a guest costs you less than this many empty rooms.*",
        "",
        "### How much the answer depends on the cost you cannot look up",
        "",
        "| Cost ratio (walk / empty room) | Recommended overbook |",
        "|---:|---:|",
    ]
    for row in example["sensitivity"]:
        lines.append(f"| {row['cost_ratio']} | {row['recommended_overbook']} |")

    lines += [
        "",
        "The recommendation moves across the plausible range, which is precisely why",
        "no single figure is published. An operator who prices a walk at twice an",
        "empty room and one who prices it at fifty times get materially different",
        "policies from the same data.",
        "",
        "## Limitations",
        "",
        "- **No cost of walking a guest.** Recorded in PART H. Until a relocation",
        "  cost per incident exists, the breakeven table is the answer and the",
        "  optimum is not computable.",
        "- **No censoring.** Every booking here has a settled outcome. Real booking",
        "  data contains reservations whose fate is not yet known, and a survival",
        "  model would be the right tool there.",
        "- **The mechanism is known because it was planted.** Recovering it validates",
        "  the method, not the method's performance on real data.",
        "- **Heterogeneity narrows the arrival distribution** for mathematical",
        "  reasons as well as predictive ones. A Poisson-binomial is tighter than the",
        "  binomial with the same mean, so per-booking probabilities justify slightly",
        "  more aggressive overbooking even when they predict no better. Both are",
        "  published so the difference is not mistaken for model quality.",
        "- **One synthetic portfolio.** None of these numbers generalise.",
        "",
    ]
    (REPORTS / "RISK.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    payload = run()
    write_report(payload)
    print("\nWrote reports/RISK.md and reports/risk.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
