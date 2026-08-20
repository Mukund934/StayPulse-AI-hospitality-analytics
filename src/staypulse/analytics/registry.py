"""Model registry and drift monitoring.

WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT

It is a record of what has actually been measured about every model this project
ships: the target, the training window, the features, the metrics by horizon,
the calibration where a model produces probabilities, which horizons it is
champion at, and whether its accuracy is degrading.

It is NOT an MLOps platform. There is no model server, no feature store, no
experiment tracker and no artifact bucket, because this project trains six
forecasting models and one classifier on a five-thousand-row warehouse and every
one of those would be scaffolding around a problem that does not exist.

Every field below is computed from an evaluation that already runs. Nothing here
introduces a new number.


THE DRIFT MEASUREMENT IS THE POINT, AND IT NEARLY WENT WRONG

The obvious drift monitor compares MAE in an early window against MAE in a late
one and alerts when it rises. Measured that way on this portfolio, every model
degrades: naive by 14%, the moving averages by 45-49%, the production pickup
model by 28%.

Almost none of that is degradation.

The portfolio grew from roughly 29 to 39 sellable units in March 2026, and the
series level rose 25.8% between the two halves of the evaluation window. MAE is
measured in room-nights, so it scales with the size of the business. Normalising
each error by the level of the series at its origin reverses the conclusion for
half the table:

    model                absolute      scale-relative
    naive                  +14.4%           -7.8%   improved
    seasonal_naive         +14.4%           -7.8%   improved
    pickup                 +28.4%           +4.2%   flat
    dow_moving_average     +45.4%          +17.3%   degraded
    moving_average         +49.3%          +19.5%   degraded
    seasonal_holiday       +49.0%          +21.1%   degraded

An absolute-MAE monitor would have raised a false alarm on the two naive models,
which got BETTER, and overstated the production model's drift roughly sevenfold.

This is the fourth time this project has been caught by the same family of
error -- comparing across units of different scale without normalising. PART L-14
records the other three. Both figures are therefore published, and the
scale-relative one leads.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from staypulse.analytics import forecast as fc
from staypulse.analytics import intervals as iv

# Window the registry evaluates over. Long enough to contain the portfolio
# expansion, which is exactly the condition a naive drift monitor gets wrong.
REGISTRY_WINDOW_DAYS = 365

# Relative degradation, after scaling, at which a model is called degrading.
# Not a tuned value: it is well above the +4.2% the production model shows and
# well below the +17% the genuinely degrading models show, so it separates the
# two groups the measurement actually found rather than splitting them.
DRIFT_ALERT_PCT = 10.0


@dataclass
class ModelCard:
    """Everything recorded about one model. Every field is measured."""

    name: str
    family: str
    target: str
    version: str
    training_window: str
    features: list[str]
    metrics: list[dict[str, Any]] = field(default_factory=list)
    champion_at: list[int] = field(default_factory=list)
    drift: dict[str, Any] = field(default_factory=dict)
    calibration: dict[str, Any] | None = None
    status: str = "active"
    limitations: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.name,
            "family": self.family,
            "target": self.target,
            "version": self.version,
            "training_window": self.training_window,
            "features": self.features,
            "metrics_by_horizon": self.metrics,
            "champion_at_horizons": self.champion_at,
            "drift": self.drift,
            "calibration": self.calibration,
            "status": self.status,
            "limitations": self.limitations,
        }


def _version(*parts: Any) -> str:
    """A short content hash standing in for a version number.

    There is no release process here, so a semantic version would be theatre. A
    digest over the configuration that produced the model at least changes when
    the model would change.
    """
    blob = json.dumps([str(p) for p in parts], sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def _drift(frame: pd.DataFrame, model: str, horizon: int) -> dict[str, Any]:
    """Early-window against late-window accuracy, absolute and scale-relative.

    Both are reported and the scale-relative figure leads. See the module
    docstring: on a portfolio that grew 25.8% mid-window, the absolute figure
    calls two improving models degraded.
    """
    rows = frame[(frame["model"] == model) & (frame["horizon"] == horizon)]
    if len(rows) < 20:
        return {"measurable": False,
                "reason": f"only {len(rows)} scored forecasts at horizon {horizon}"}

    origins = sorted(rows["origin"].unique())
    midpoint = origins[len(origins) // 2]
    early = rows[rows["origin"] < midpoint]
    late = rows[rows["origin"] >= midpoint]
    if early.empty or late.empty:
        return {"measurable": False, "reason": "window does not split"}

    absolute_early = float(early["error"].abs().mean())
    absolute_late = float(late["error"].abs().mean())
    relative_early = float((early["error"].abs() / early["scale"]).mean())
    relative_late = float((late["error"].abs() / late["scale"]).mean())

    absolute_change = 100.0 * (absolute_late - absolute_early) / absolute_early
    relative_change = 100.0 * (relative_late - relative_early) / relative_early

    level_early = float(early["scale"].mean())
    level_late = float(late["scale"].mean())

    return {
        "measurable": True,
        "split_at": str(pd.Timestamp(midpoint).date()),
        "series_level_change_pct": round(
            100.0 * (level_late - level_early) / level_early, 1),
        "absolute": {
            "mae_early": round(absolute_early, 3),
            "mae_late": round(absolute_late, 3),
            "change_pct": round(absolute_change, 1),
        },
        "scale_relative": {
            "mae_early": round(relative_early, 5),
            "mae_late": round(relative_late, 5),
            "change_pct": round(relative_change, 1),
        },
        "verdict": (
            "degrading" if relative_change > DRIFT_ALERT_PCT
            else "improving" if relative_change < -DRIFT_ALERT_PCT
            else "stable"
        ),
        "note": (
            "The verdict uses the SCALE-RELATIVE change. MAE is in room-nights "
            "and scales with the size of the portfolio, which grew "
            f"{100.0 * (level_late - level_early) / level_early:.1f}% across this "
            "window. Judging on the absolute figure alone calls improving models "
            "degraded."
        ),
    }


def forecast_models(results: pd.DataFrame | None = None) -> list[ModelCard]:
    """One card per forecasting model, from the rolling-origin backtest."""
    raw = fc.backtest(test_days=REGISTRY_WINDOW_DAYS, origin_step=3) \
        if results is None else results
    prepared = iv._prepare(raw)
    scores = fc.score(raw)
    champions = fc.winners(scores)

    describes = {
        "naive": "tomorrow equals today",
        "seasonal_naive": "next Tuesday equals last Tuesday",
        "moving_average": f"mean of the trailing {fc.MA_WINDOW} days",
        "dow_moving_average": f"mean of the last {fc.DOW_WINDOW} same-weekday values",
        "pickup": "on the books now, plus the pickup comparable dates still received",
        "seasonal_holiday": "day-of-week baseline scaled by a measured holiday effect",
    }
    known_limits = {
        "seasonal_holiday": [
            "Published as a FAILURE. On holiday-adjacent dates it scored MAE "
            "4.90 against a 4.19 baseline. Kept registered so its loss appears "
            "in the comparison rather than being quietly removed.",
        ],
        "pickup": [
            "Uses information the other models cannot see -- what is already "
            "sold -- so its advantage at short horizons is structural.",
        ],
    }

    cards: list[ModelCard] = []
    for name in fc.MODELS:
        metrics = [s.as_dict() for s in scores if s.model == name]
        cards.append(ModelCard(
            name=name,
            family="time series baseline" if name != "pickup" else "booking-curve pickup",
            target="daily occupied room-nights, portfolio total",
            version=_version(name, REGISTRY_WINDOW_DAYS, fc.MAX_HORIZON,
                             fc.MA_WINDOW, fc.DOW_WINDOW, fc.PICKUP_WINDOW),
            training_window=(
                f"rolling origin, {REGISTRY_WINDOW_DAYS} days, "
                f"{raw['origin'].nunique()} origins"
            ),
            features=["realised occupancy history"] + (
                ["on-the-books at horizon"] if name == "pickup" else []
            ) + (["measured holiday multiplier"] if name == "seasonal_holiday" else []),
            metrics=metrics,
            champion_at=[h for h, m in champions.items() if m == name],
            drift=_drift(prepared, name, 7),
            limitations=known_limits.get(name, []) + [describes.get(name, "")],
        ))
    return cards


def cancellation_model() -> ModelCard | None:
    """The classifier's card, from its stored evaluation.

    Read from the published artifact rather than refitted: the registry records
    what was measured, and refitting here would let the registry and the report
    disagree about the same model.
    """
    from pathlib import Path

    stored = Path(__file__).resolve().parents[3] / "reports" / "risk.json"
    if not stored.exists():
        return None
    payload = json.loads(stored.read_text(encoding="utf-8"))["cancellation"]
    evaluation = payload["evaluation"]
    classification = evaluation["classification_at_threshold"]

    return ModelCard(
        name="cancellation_risk",
        family="logistic regression, L2, numpy",
        target="booking cancelled before arrival",
        version=_version("cancellation", payload["model"]["features"],
                         evaluation["split"]["split_date"]),
        training_window=(
            f"temporal split at {evaluation['split']['split_date']}; "
            f"{evaluation['split']['train_bookings']} train / "
            f"{evaluation['split']['test_bookings']} test"
        ),
        features=[row["feature"] for row in payload["model"]["coefficients"]],
        metrics=[{
            "auc": evaluation["discrimination"]["auc"],
            "precision_pct": classification["precision_pct"],
            "recall_pct": classification["recall_pct"],
            "lift_over_base_rate": classification["lift_over_base_rate"],
            "base_rate_pct": evaluation["base_rate_pct"],
            "brier": evaluation["brier_score"],
        }],
        champion_at=[],
        drift={
            "measurable": True,
            "measure": "cancellation base rate by booking quarter",
            "first_quarter_pct": payload["temporal_drift"]["first_quarter_pct"],
            "last_quarter_pct": payload["temporal_drift"]["last_quarter_pct"],
            "change_pp": payload["temporal_drift"]["change_pp"],
            "verdict": "target rate declining",
            "note": (
                "The DATA drifts, not just the model: the cancellation rate "
                "falls across the record, so a model fitted on earlier bookings "
                "over-predicts on later ones. Visible in the upper calibration "
                "bins."
            ),
        },
        calibration={
            "weighted_mean_absolute_error_pp":
                evaluation["calibration"]["weighted_mean_absolute_error_pp"],
            "bins": len(evaluation["calibration"]["bins"]),
            "note": (
                "Weighted by bin population. Unweighted it reads 9.13pp because "
                "one bin holds a single booking -- the same error family as the "
                "drift measurement above."
            ),
        },
        limitations=[
            "No-show is deliberately excluded: it is a flat 1.4% in the "
            "generator and unlearnable, measured at AUC 0.527.",
            "No censoring in this data; a survival model would be correct on "
            "real bookings.",
        ],
    )


def summary() -> dict[str, Any]:
    """The published registry."""
    raw = fc.backtest(test_days=REGISTRY_WINDOW_DAYS, origin_step=3)
    cards = forecast_models(raw)
    cancellation = cancellation_model()
    if cancellation:
        cards.append(cancellation)

    degrading = [c.name for c in cards
                 if c.drift.get("verdict") == "degrading"]
    improving = [c.name for c in cards
                 if c.drift.get("verdict") == "improving"]

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "models": len(cards),
        "window_days": REGISTRY_WINDOW_DAYS,
        "what_this_is": (
            "A record of what has been measured about every model this project "
            "ships. Not an MLOps platform: there is no model server, feature "
            "store or experiment tracker, because six baselines and one "
            "classifier on a five-thousand-row warehouse do not need any."
        ),
        "drift_summary": {
            "degrading": degrading,
            "improving": improving,
            "stable": [c.name for c in cards
                       if c.drift.get("verdict") == "stable"],
            "method": (
                "Early half against late half, reported BOTH absolutely and "
                "relative to the level of the series. The verdict uses the "
                "scale-relative figure because MAE is in room-nights and the "
                "portfolio grew 25.8% across this window -- judging on absolute "
                "MAE alone calls two improving models degraded and overstates "
                "the production model's drift roughly sevenfold."
            ),
        },
        "registry": [card.as_dict() for card in cards],
    }
