"""Publish the model registry and drift report.

Usage:
    python scripts/run_model_registry.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import registry as reg  # noqa: E402

REPORTS = PROJECT_ROOT / "reports"


def main() -> int:
    print("Building the model registry...", flush=True)
    payload = reg.summary()
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "model_registry.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8")

    d = payload["drift_summary"]
    lines = [
        "# Model registry and drift",
        "",
        f"_Generated {payload['generated_at']}. {payload['models']} models over a "
        f"{payload['window_days']}-day window._",
        "",
        "## What this is",
        "",
        payload["what_this_is"],
        "",
        "## The drift measurement, and the mistake it avoids",
        "",
        "The obvious monitor compares MAE early against MAE late and alerts when it",
        "rises. Measured that way here, **every model degrades** -- the naive models",
        "by 15%, the moving averages by 44-46%, the production pickup model by 32%.",
        "",
        "Almost none of that is degradation.",
        "",
        "MAE is measured in room-nights, and the portfolio grew from roughly 29 to 39",
        "sellable units in March 2026. The series level rose about 26% between the two",
        "halves of the window, so the error scales with it. Normalising each error by",
        "the level of the series at its origin changes the verdict for half the table.",
        "",
        "| Model | Champion at | Absolute drift | Scale-relative | Verdict |",
        "|---|---|---:|---:|---|",
    ]
    for card in payload["registry"]:
        dr = card["drift"]
        if dr.get("measurable") and "scale_relative" in dr:
            lines.append(
                f"| `{card['model']}` | {card['champion_at_horizons'] or '—'} | "
                f"{dr['absolute']['change_pct']:+}% | "
                f"{dr['scale_relative']['change_pct']:+}% | **{dr['verdict']}** |")
    lines += [
        "",
        "An absolute-MAE monitor calls the two naive models degraded when they",
        "**improved** by about 7% relative to scale, and reports the production",
        "model as +32% when the scale-relative figure is +7.6%.",
        "",
        "This is the **fourth** time this project has been caught by the same family",
        "of error -- comparing across units of different scale without normalising.",
        "PART L-14 of the roadmap records the other three: pooled holiday multipliers,",
        "Simpson's paradox in the alert bias, and an unweighted calibration mean.",
        "Both figures are published here and the scale-relative one carries the",
        "verdict.",
        "",
        "## Registry",
        "",
    ]
    for card in payload["registry"]:
        lines += [
            f"### `{card['model']}`",
            "",
            f"- **Family** {card['family']}",
            f"- **Target** {card['target']}",
            f"- **Version** `{card['version']}`",
            f"- **Training window** {card['training_window']}",
            f"- **Features** {', '.join(card['features'][:8])}"
            + (" …" if len(card['features']) > 8 else ""),
            f"- **Champion at horizons** {card['champion_at_horizons'] or 'none'}",
            f"- **Status** {card['status']}",
        ]
        if card.get("calibration"):
            lines.append(
                f"- **Calibration** "
                f"{card['calibration']['weighted_mean_absolute_error_pp']}pp "
                f"weighted MAE over {card['calibration']['bins']} bins")
        if card["limitations"]:
            lines.append("- **Limitations**")
            for lim in card["limitations"]:
                if lim.strip():
                    lines.append(f"  - {lim}")
        lines.append("")

    (REPORTS / "MODEL_REGISTRY.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"  degrading: {d['degrading']}")
    print(f"  stable:    {d['stable']}")
    print("\nWrote reports/MODEL_REGISTRY.md and reports/model_registry.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
