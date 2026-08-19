"""Anomaly detection for strongly seasonal daily hospitality series.

METHOD CHOICE, and why not the obvious one.

Isolation Forest is the reflex answer and it is wrong here. On a univariate series
with hard weekly seasonality it learns that low-occupancy days are unusual and
flags every weekend, while missing the Tuesday running 40% below its own Tuesday
norm — which is the case Operations actually cares about. It also gives no
baseline, no magnitude and no explanation, so an alert cannot be acted on.

What this uses instead:

  1. A DAY-OF-WEEK AWARE trailing baseline. Corporate aparthotel demand is
     weekday-heavy, so Saturday must be compared with Saturdays. A plain rolling
     mean bakes the weekly cycle into the residual and fires every weekend.

  2. ROBUST scale via MAD, not standard deviation. One genuine outlier inflates
     sigma and hides the next one; MAD is unaffected. Scaled by 1.4826 so it
     estimates sigma for a normal distribution and the thresholds stay
     interpretable.

  3. DUAL THRESHOLDS. A deviation must be both statistically unusual AND
     materially large in absolute terms. Without the second gate, a quiet metric
     on a small property produces a stream of technically-significant, practically
     irrelevant alerts.

  4. A PUBLISHED FALSE-ALERT BUDGET. Monitoring m metrics across n segments daily
     is m x n tests per day; at 2 sigma that fires constantly. The threshold is
     derived from the budget rather than chosen for looking clean, and the expected
     false-alert rate is stated so Operations can plan around it.

Every anomaly carries what changed, the baseline, the magnitude, a confidence and
its likely drivers. An anomaly without a driver is a chart, not an alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# Robust-sigma multiplier: MAD x 1.4826 estimates sigma for a normal distribution.
MAD_TO_SIGMA = 1.4826

# Chosen for a family of roughly 40-70 daily tests. At |z| > 2 the expected false
# positive count is ~2-3 per day, which is alert fatigue. At 3.5 it is well under
# one per week. See false_alert_budget().
Z_THRESHOLD = 3.5

# Minimum trailing same-weekday observations before a baseline is trusted at all.
MIN_HISTORY = 6

# Materiality gates for the portfolio-level metrics, in each metric's own units.
#
# These were inline in scripts/run_anomaly_detection.py, which meant the Alert
# Center had a choice between importing a script or restating them -- and a
# restated gate is a second definition of "anomaly", which is exactly what the
# semantic layer exists to prevent. Revenue's gate is a fraction of the median
# rather than an absolute, because a rupee threshold ages with the portfolio.
PORTFOLIO_GATES: dict[str, float] = {
    "occupancy_pct": 8.0,
    "adr_inr": 350.0,
}
REVENUE_GATE_FRACTION_OF_MEDIAN = 0.15


@dataclass
class Anomaly:
    metric: str
    segment: str
    date: str
    actual: float
    baseline: float
    deviation: float
    deviation_pct: float
    robust_z: float
    direction: str
    confidence: str
    drivers: list[str] = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "metric": self.metric, "segment": self.segment, "date": self.date,
            "actual": round(self.actual, 2), "baseline": round(self.baseline, 2),
            "deviation": round(self.deviation, 2),
            "deviation_pct": round(self.deviation_pct, 1),
            "robust_z": round(self.robust_z, 2), "direction": self.direction,
            "confidence": self.confidence, "drivers": "; ".join(self.drivers),
        }


def _robust_scale(values: np.ndarray) -> float:
    if len(values) < 2:
        return 0.0
    mad = float(np.median(np.abs(values - np.median(values))))
    return mad * MAD_TO_SIGMA


def detect(
    frame: pd.DataFrame,
    *,
    metric: str,
    date_col: str = "stay_date",
    segment: str = "PORTFOLIO",
    lookback_weeks: int = 8,
    min_abs_change: float = 0.0,
    z_threshold: float = Z_THRESHOLD,
) -> list[Anomaly]:
    """Flag day-of-week-aware robust outliers in one metric series.

    `min_abs_change` is the materiality gate: the second half of the dual
    threshold. Set it in the metric's own units.
    """
    df = frame[[date_col, metric]].dropna().copy()
    if df.empty:
        return []
    df[date_col] = pd.to_datetime(df[date_col])
    df = df.sort_values(date_col).reset_index(drop=True)
    df["dow"] = df[date_col].dt.dayofweek

    out: list[Anomaly] = []
    for i in range(len(df)):
        row = df.iloc[i]
        # Trailing same-weekday history only. Strictly trailing: a baseline that
        # includes the point being tested dilutes its own signal.
        hist = df.iloc[:i]
        hist = hist[hist["dow"] == row["dow"]].tail(lookback_weeks)
        if len(hist) < MIN_HISTORY:
            continue

        vals = hist[metric].to_numpy(dtype=float)
        baseline = float(np.median(vals))
        scale = _robust_scale(vals)
        actual = float(row[metric])
        deviation = actual - baseline

        # A perfectly flat history means any movement is infinitely significant,
        # which is an artefact. Fall back to the materiality gate alone.
        if scale <= 1e-9:
            if abs(deviation) < max(min_abs_change, 1e-9):
                continue
            z = float(np.sign(deviation)) * (z_threshold + 1.0)
        else:
            z = deviation / scale

        if abs(z) < z_threshold:
            continue
        if abs(deviation) < min_abs_change:
            continue        # statistically unusual but not worth anyone's morning

        pct = 100.0 * deviation / baseline if baseline else 0.0
        confidence = ("high" if abs(z) >= z_threshold + 2 and len(hist) >= lookback_weeks
                      else "medium" if abs(z) >= z_threshold else "low")
        out.append(Anomaly(
            metric=metric,
            segment=segment,
            date=row[date_col].date().isoformat(),
            actual=actual, baseline=baseline, deviation=deviation,
            deviation_pct=pct, robust_z=z,
            direction="above" if deviation > 0 else "below",
            confidence=confidence,
        ))
    return out


def false_alert_budget(n_metrics: int, n_segments: int,
                       z_threshold: float = Z_THRESHOLD) -> dict:
    """Expected false-alert volume for the configured threshold.

    Published rather than hidden, because a detector whose false-alert rate is
    unknown cannot be trusted or tuned, and Operations deserves to know roughly
    how often a page will be nothing.
    """
    from math import erfc, sqrt

    tests_per_day = n_metrics * n_segments
    # Two-sided normal tail probability.
    p_flag = erfc(z_threshold / sqrt(2))
    per_day = tests_per_day * p_flag
    return {
        "metrics_monitored": n_metrics,
        "segments": n_segments,
        "tests_per_day": tests_per_day,
        "z_threshold": z_threshold,
        "per_test_false_positive_rate": p_flag,
        "expected_false_alerts_per_day": round(per_day, 3),
        "expected_false_alerts_per_month": round(per_day * 30, 2),
        "note": (
            f"At |z| > {z_threshold} across {tests_per_day} daily tests, expect about "
            f"{per_day * 30:.1f} false alerts a month. At |z| > 2.0 the same family "
            f"would produce roughly {tests_per_day * erfc(2.0 / sqrt(2)) * 30:.0f} a "
            f"month, which is why the threshold is not 2. Residuals are also fatter "
            f"tailed than normal, so treat this as a floor rather than a forecast."
        ),
    }


def attribute(
    frame: pd.DataFrame, anomaly: Anomaly, *, by: str, metric: str,
    date_col: str = "stay_date", top_n: int = 3,
) -> list[str]:
    """Rank which segments contributed most to a portfolio-level anomaly.

    Deterministic contribution analysis: no model, just each segment's share of the
    total change against its own trailing same-weekday baseline.
    """
    df = frame.copy()
    df[date_col] = pd.to_datetime(df[date_col])
    target = pd.Timestamp(anomaly.date)
    dow = target.dayofweek

    on_day = df[df[date_col] == target]
    if on_day.empty:
        return []

    drivers: list[tuple[str, float]] = []
    for seg, seg_rows in df.groupby(by):
        hist = seg_rows[(seg_rows[date_col] < target)
                        & (seg_rows[date_col].dt.dayofweek == dow)].tail(8)
        cur = seg_rows[seg_rows[date_col] == target]
        if hist.empty or cur.empty:
            continue
        base = float(np.median(hist[metric].to_numpy(dtype=float)))
        delta = float(cur[metric].iloc[0]) - base
        drivers.append((str(seg), delta))

    total = sum(abs(d) for _, d in drivers)
    if not total:
        return []
    ranked = sorted(drivers, key=lambda x: -abs(x[1]))[:top_n]
    return [
        f"{seg} {'+' if d > 0 else ''}{d:,.1f} ({100 * abs(d) / total:.0f}% of movement)"
        for seg, d in ranked if abs(d) > 0
    ]
