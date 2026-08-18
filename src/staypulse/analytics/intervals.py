"""Prediction intervals for the occupancy forecast, and the coverage test that
decides whether they mean anything.

WHY EMPIRICAL RESIDUALS RATHER THAN A DISTRIBUTION

A textbook interval assumes the errors are normal and independent. Daily occupancy
errors here are neither: they are bounded below by zero, bounded above by
inventory, skewed, and correlated within a week. Fitting a normal to them produces
a symmetric interval that is too wide on one side and too narrow on the other, and
nothing in the output shows it.

There are already thousands of scored forecasts in the rolling-origin backtest. The
quantiles of those errors ARE the interval, with no distributional assumption to be
wrong about. Where the error distribution is skewed the interval comes out
asymmetric, which is the correct answer rather than a defect.


THE MISTAKE THIS MODULE IS BUILT TO AVOID

An interval fitted on the same residuals it is then scored against will hit its
nominal coverage almost exactly, because that is what a quantile does to its own
sample. The number looks like validation and is arithmetic. Measured here: 80.9%
in sample against a nominal 80%, while the same intervals covered 71.5% out of
sample. Both are reported, and only the second is a result.


THE SUBTLE PART, AND IT IS THE WHOLE FEATURE

"Residuals available at origin O" is NOT "residuals from origins before O".

A forecast made at origin O' for horizon h targets O' + h. Its error is knowable
only once that target has happened. At origin O, a 30-day-ahead forecast made
yesterday has no error yet -- its target is 29 days in the future. Filtering on
`origin < O` therefore pulls in errors that had not been observed, and it does so
most heavily at exactly the long horizons where the interval matters.

The filter is `target <= O`.


TWO CORRECTIONS, AND WHY NEITHER IS A TUNED FUDGE FACTOR

The plain empirical quantile under-covered badly: 71.5% against a nominal 80%.
Two changes fixed it, and it matters that each was derived rather than searched
for, because a widening factor tuned until coverage hit its target would be
fitting the evaluation.

1. SCALE-RELATIVE RESIDUALS. Measured first, then fixed: the error spread grows
   from sd 2.06 to sd 3.52 across the study window, tracking the portfolio's
   growth from ~29 to ~39 sellable unit-nights a day. Absolute residuals from a
   smaller business systematically under-state the spread of a larger one. So the
   residual is divided by the level of the series at its origin, calibrated in
   relative terms, and multiplied back by the level at prediction time. This is
   the same lesson `revenue.BENCHMARK_WINDOW` records for the pace benchmark: a
   baseline must track the level of the business rather than average over its
   history.

2. CONFORMAL QUANTILE SELECTION. A plug-in empirical quantile from n residuals
   under-covers in finite samples. The split-conformal correction takes the
   ceil((n+1)(1-a/2))-th order statistic instead, which carries a finite-sample
   guarantee of AT LEAST the nominal level. It is a derived adjustment with a
   known direction, not a knob. It is why the headline result slightly
   over-covers, which is the correct direction for an interval to err.

All four combinations are computed and published, so the progression from 71.5%
to the final figure is visible rather than asserted.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from staypulse.analytics import forecast as fc

# Nominal coverage levels published. 80% is the headline because it is the level
# an operator can act on without the interval being so wide it says nothing; 50%
# and 95% are included because a method tuned to one level fails at the others,
# and reporting three is what makes that checkable.
LEVELS: tuple[float, ...] = (0.5, 0.8, 0.95)

# Residuals required before an interval is published for a (model, horizon).
#
# Below this the empirical quantile is a statement about two or three observations
# wearing a percentage. The project already takes this position for the pace
# benchmark (MIN_SUPPORT) and the holiday effect (MIN_EFFECT_OBS); this is the
# same rule applied to the same kind of claim.
MIN_RESIDUALS = 20

# Origins reserved for calibration before out-of-sample coverage is scored. With
# fewer than this the first evaluation origins are being judged on intervals built
# from almost nothing, which measures the warm-up rather than the method.
MIN_CALIBRATION_ORIGINS = 20

# Trailing days of realised occupancy used as the level the residuals are scaled
# by. Ends at the ORIGIN, so it is knowable there -- the same rule as
# `forecast.CAPACITY_WINDOW`, for the same reason.
SCALE_WINDOW = 28

# Backtest window used for the interval study. Longer than the 120-day headline
# backtest on purpose: the `target <= origin` rule means a 30-day horizon consumes
# a month of history before it has a single usable residual, and a short window
# would leave the long horizons with nothing to calibrate on.
STUDY_DAYS = 365


@dataclass(frozen=True)
class Method:
    """One way of turning residuals into an interval."""

    name: str
    scaled: bool
    conformal: bool


METHODS: tuple[Method, ...] = (
    Method("absolute_plain", scaled=False, conformal=False),
    Method("absolute_conformal", scaled=False, conformal=True),
    Method("scaled_plain", scaled=True, conformal=False),
    Method("scaled_conformal", scaled=True, conformal=True),
)

# The published default, chosen on measured out-of-sample coverage rather than on
# preference. `summary()` reports every method so the choice can be re-checked.
DEFAULT_METHOD = METHODS[3]


@dataclass
class Interval:
    """An empirical interval for one model at one horizon.

    Offsets are in room-nights when `scaled` is false, and in units of the
    series level when it is true -- in which case they mean nothing until
    multiplied by a level, which `bound()` does.
    """

    model: str
    horizon: int
    level: float
    lo_offset: float
    hi_offset: float
    n_residuals: int
    scaled: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "horizon_days": self.horizon,
            "level": self.level,
            "lower_offset": round(self.lo_offset, 3),
            "upper_offset": round(self.hi_offset, 3),
            "offsets_are": "fraction of series level" if self.scaled else "room-nights",
            "residuals": self.n_residuals,
        }


# ---------------------------------------------------------------------------
def _quantile_pair(values: np.ndarray, level: float, conformal: bool
                   ) -> tuple[float, float]:
    """Empirical error quantiles bracketing `level` of the distribution.

    `error` is prediction minus actual, so an actual is recovered as
    `prediction - error`. The LOWER bound on the actual therefore comes from the
    UPPER quantile of the error, and vice versa. Getting that backwards produces
    an interval that is inverted and still looks plausible on a chart.

    With `conformal`, the quantile level is raised to the split-conformal order
    statistic, ceil((n+1)(1-a/2))/n, which is what buys the finite-sample
    guarantee. It always widens and never narrows.
    """
    alpha = 1.0 - level
    n = len(values)
    if conformal:
        hi_level = min(1.0, math.ceil((n + 1) * (1.0 - alpha / 2.0)) / n)
        lo_level = max(0.0, math.floor((n + 1) * (alpha / 2.0)) / n)
    else:
        hi_level, lo_level = 1.0 - alpha / 2.0, alpha / 2.0
    lo = float(np.quantile(values, lo_level, method="lower"))
    hi = float(np.quantile(values, hi_level, method="higher"))
    return lo, hi


def series_level(actuals: pd.Series | None = None) -> pd.Series:
    """Trailing mean occupied room-nights, indexed by date.

    This is the level residuals are scaled by. It ends at each date, so the value
    at an origin uses only nights that had already happened there.
    """
    if actuals is None:
        actuals = fc.daily_actuals()["occupied"].astype(float)
    return actuals.rolling(SCALE_WINDOW).mean()


def _prepare(results: pd.DataFrame) -> pd.DataFrame:
    """Attach the origin-time series level and the scale-relative residual."""
    level = series_level()
    out = results.copy()
    out["scale"] = out["origin"].map(level)
    out = out.dropna(subset=["scale"])
    out = out[out["scale"] > 0]
    out["relative_error"] = out["error"] / out["scale"]
    return out


def from_residuals(
    results: pd.DataFrame,
    level: float = 0.8,
    method: Method = DEFAULT_METHOD,
    min_residuals: int = MIN_RESIDUALS,
) -> dict[tuple[str, int], Interval]:
    """(model, horizon) -> interval, from the residuals in `results`.

    No temporal filtering is applied here. The caller decides which residuals were
    available, because that decision is the difference between a calibration and a
    leak, and burying it in a default would hide it.
    """
    out: dict[tuple[str, int], Interval] = {}
    if results.empty:
        return out

    column = "relative_error" if method.scaled else "error"
    if column not in results.columns:
        raise KeyError(
            f"{column!r} missing; pass residuals through `_prepare` before scaling"
        )

    for (model, horizon), group in results.groupby(["model", "horizon"]):
        values = group[column].to_numpy(dtype=float)
        if len(values) < min_residuals:
            continue
        lo, hi = _quantile_pair(values, level, method.conformal)
        out[(str(model), int(horizon))] = Interval(
            model=str(model),
            horizon=int(horizon),
            level=level,
            lo_offset=lo,
            hi_offset=hi,
            n_residuals=len(values),
            scaled=method.scaled,
        )
    return out


def bound(prediction: float, interval: Interval, scale: float) -> tuple[float, float]:
    """Turn a point forecast and an error interval into a bounded prediction.

    Clipped at zero because negative room-nights do not exist. Deliberately NOT
    clipped at capacity: sellable inventory for a future date is not knowable at
    the origin -- out-of-order nights are settled only once the date has passed --
    so an upper clip would be the same hindsight the replay work exists to
    prevent.
    """
    factor = scale if interval.scaled else 1.0
    lower = prediction - interval.hi_offset * factor
    upper = prediction - interval.lo_offset * factor
    return max(0.0, lower), max(0.0, upper)


# ---------------------------------------------------------------------------
def coverage(
    test_days: int = STUDY_DAYS,
    level: float = 0.8,
    origin_step: int = 3,
    method: Method = DEFAULT_METHOD,
    min_calibration: int = MIN_CALIBRATION_ORIGINS,
    results: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """Out-of-sample coverage: how often the interval actually contained the truth.

    Walks forward through the backtest origins. At each evaluation origin the
    interval is rebuilt from residuals whose TARGET had already been realised by
    then, so nothing is calibrated on an error nobody could have seen.

    In-sample coverage is computed alongside it from the full residual set. It is
    reported as a contrast, not as evidence.
    """
    raw = fc.backtest(test_days=test_days, origin_step=origin_step) \
        if results is None else results
    prepared = _prepare(raw)

    origins = sorted(prepared["origin"].unique())
    evaluation_origins = origins[min_calibration:]

    records: list[dict[str, Any]] = []
    for origin in evaluation_origins:
        # THE LINE THAT MAKES THIS OUT OF SAMPLE. `target <= origin`, not
        # `origin < origin`: an error is only knowable once its target happened.
        known = prepared[prepared["target"] <= origin]
        built = from_residuals(known, level=level, method=method)
        if not built:
            continue

        for row in prepared[prepared["origin"] == origin].itertuples():
            interval = built.get((row.model, int(row.horizon)))
            if interval is None:
                continue
            lower, upper = bound(float(row.prediction), interval, float(row.scale))
            records.append({
                "model": row.model,
                "horizon": int(row.horizon),
                "covered": bool(lower <= float(row.actual) <= upper),
                "width": upper - lower,
                "calibration_residuals": interval.n_residuals,
            })

    scored = pd.DataFrame(records)
    return {
        "level": level,
        "method": method.name,
        "test_window_days": test_days,
        "origins_total": len(origins),
        "origins_reserved_for_calibration": min_calibration,
        "origins_evaluated": len(evaluation_origins),
        "forecasts_scored": int(len(scored)),
        "out_of_sample": _summarise(scored, level),
        "in_sample": _in_sample_coverage(prepared, level, method),
        "note": (
            "Out-of-sample coverage rebuilds the interval at every evaluation "
            "origin from residuals whose target had already been realised by that "
            "origin. In-sample coverage uses the whole residual set and is shown "
            "only for contrast: an empirical quantile reproduces its own nominal "
            "level by construction, so a matching in-sample figure is arithmetic "
            "rather than validation."
        ),
    }


def _in_sample_coverage(prepared: pd.DataFrame, level: float,
                        method: Method) -> dict[str, Any]:
    """Coverage of an interval fitted on the very residuals it is scored against."""
    built = from_residuals(prepared, level=level, method=method)
    hits, total = 0, 0
    for row in prepared.itertuples():
        interval = built.get((row.model, int(row.horizon)))
        if interval is None:
            continue
        lower, upper = bound(float(row.prediction), interval, float(row.scale))
        total += 1
        hits += int(lower <= float(row.actual) <= upper)
    return {
        "forecasts_scored": total,
        "coverage_pct": round(100.0 * hits / total, 1) if total else None,
    }


def _summarise(scored: pd.DataFrame, level: float) -> dict[str, Any]:
    """Coverage overall, per model and per horizon."""
    if scored.empty:
        return {"coverage_pct": None, "note": "nothing scored"}

    target_pct = 100.0 * level

    def _block(df: pd.DataFrame) -> dict[str, Any]:
        pct = round(100.0 * float(df["covered"].mean()), 1)
        return {
            "forecasts": int(len(df)),
            "coverage_pct": pct,
            "deviation_pp": round(pct - target_pct, 1),
            "median_width_nights": round(float(df["width"].median()), 2),
            "median_calibration_residuals": int(df["calibration_residuals"].median()),
        }

    return {
        **_block(scored),
        "target_pct": target_pct,
        "by_model": {
            str(model): _block(group) for model, group in scored.groupby("model")
        },
        "by_horizon": {
            str(int(horizon)): _block(group)
            for horizon, group in scored.groupby("horizon")
            if int(horizon) in fc.REPORTED_HORIZONS
        },
    }


# ---------------------------------------------------------------------------
def forward(
    as_of: dt.date | None = None,
    horizon: int = fc.MAX_HORIZON,
    model: str = "pickup",
    level: float = 0.8,
    calibration_days: int = 180,
    method: Method = DEFAULT_METHOD,
) -> dict[str, Any]:
    """A forward forecast carrying an interval around every point.

    The interval is calibrated on residuals from before `as_of` only, under the
    same `target <= as_of` rule the coverage study uses, so a forecast made at a
    historical date is bounded by errors that had actually been observed by then.

    A horizon with too few residuals gets `null` bounds rather than a number.
    Publishing a two-observation quantile because the column expects a number is
    how an interval becomes decoration.
    """
    points = fc.forward(as_of=as_of, horizon=horizon, model=model)
    actuals = fc.daily_actuals()["occupied"].astype(float)
    origin = pd.Timestamp(as_of) if as_of else actuals.index.max()

    levels = series_level(actuals)
    scale = float(levels.loc[:origin].iloc[-1]) if len(levels.loc[:origin]) else 0.0

    history = _prepare(
        fc.backtest(test_days=calibration_days, origin_step=3, max_horizon=horizon)
    )
    known = history[(history["target"] <= origin) & (history["model"] == model)]
    built = from_residuals(known, level=level, method=method)

    rows: list[dict[str, Any]] = []
    for point in points:
        interval = built.get((model, int(point["horizon_days"])))
        if interval is None or scale <= 0:
            rows.append({
                **point,
                "lower_room_nights": None,
                "upper_room_nights": None,
                "interval_residuals": 0 if interval is None else interval.n_residuals,
                "interval_note": (
                    f"fewer than {MIN_RESIDUALS} observed residuals at this horizon"
                ),
            })
            continue
        lower, upper = bound(float(point["predicted_room_nights"]), interval, scale)
        rows.append({
            **point,
            "lower_room_nights": round(lower, 1),
            "upper_room_nights": round(upper, 1),
            "interval_residuals": interval.n_residuals,
            "interval_note": None,
        })

    return {
        "as_of": origin.date().isoformat(),
        "model": model,
        "level": level,
        "method": method.name,
        "calibration_window_days": calibration_days,
        "series_level_room_nights": round(scale, 2),
        "method_note": (
            "Empirical quantiles of the model's own backtest residuals at each "
            "horizon, using only residuals whose target had been realised by the "
            "as-of date. Residuals are scaled by the trailing level of the series "
            "because the error spread grows with the size of the portfolio, and "
            "the quantile is the split-conformal order statistic rather than the "
            "plug-in estimate. No distributional assumption; the interval is "
            "asymmetric wherever the error distribution is."
        ),
        "caveat": (
            "Bounds are clipped at zero and NOT at capacity. Sellable inventory "
            "for a future date is not knowable at the origin, so an upper clip "
            "would import hindsight."
        ),
        "forecast": rows,
    }


def summary(test_days: int = STUDY_DAYS) -> dict[str, Any]:
    """Coverage for every level and every method. The headline artifact.

    Every method is reported, not just the one that wins. The progression from the
    plain empirical quantile to the published default is the evidence that the
    default was chosen on measured coverage rather than preference, and a reader
    who suspects the corrections were tuned can check them at 50% and 95% -- where
    a method fitted to 80% would come apart.
    """
    results = fc.backtest(test_days=test_days, origin_step=3)
    return {
        "target": "daily occupied room-nights, portfolio total",
        "default_method": DEFAULT_METHOD.name,
        "study_window_days": test_days,
        "coverage": [
            coverage(test_days=test_days, level=lvl, method=m, results=results)
            for lvl in LEVELS
            for m in METHODS
        ],
    }
