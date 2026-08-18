"""Occupancy forecasting, benchmarked against models that are hard to beat.

THE POINT OF THIS MODULE IS THE COMPARISON, NOT THE FORECAST

A single forecast with an error number attached proves nothing: without a baseline
there is no way to know whether 12% error is good, bad or worse than repeating last
Tuesday. So five models are run over the same rolling-origin backtest and the table
is published whichever way it comes out. If seasonal naive wins, that is the result
and it gets reported -- it is a common and respectable outcome on 18 months of daily
data, and claiming otherwise would be the easiest thing in this project to fake.


THE MODELS, IN INCREASING ORDER OF WHAT THEY ASSUME

  naive              tomorrow equals today. The floor.
  seasonal_naive     next Tuesday equals last Tuesday. Usually the real bar on any
                     series with hard weekly seasonality, and it is the model most
                     "sophisticated" attempts silently fail to beat.
  moving_average     mean of the trailing 28 days. Ignores the weekly cycle.
  dow_moving_average mean of the last 4 same-weekday values. Seasonal naive with the
                     noise averaged down.
  pickup             on-the-books at this horizon, plus the pickup that comparable
                     dates historically still received from this horizon on. The
                     only model here that uses information the others cannot see --
                     it knows what is already sold.

The pickup model is the hospitality-specific one and it is included because it is
the one a revenue manager would actually recognise. It should dominate at short
horizons, where the book is nearly full and there is little left to guess, and decay
towards the seasonal baseline at long horizons, where almost nothing is on the books
yet. Whether it does is an empirical question this module answers rather than
assumes.


ROLLING ORIGIN, AND WHY NOT A SINGLE HOLDOUT

Every forecast is made from an origin date using only data at or before that origin,
and scored against what actually happened. Origins step forward through the test
period, so each model is evaluated many times across different weeks rather than on
one lucky fortnight. Any model that peeks past its origin will look excellent and be
worthless; `tests/test_forecast.py` asserts that none of them can.


ON MAPE

Reported because it is the metric operators ask for, with the zero-denominator guard
that most implementations omit. RMSE is the one to read when the cost of being badly
wrong is worse than the cost of being slightly wrong, which for staffing it is.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pandas as pd

from staypulse import db

# Longest horizon worth forecasting here. Median lead time is 7 days and the median
# stay date is 8% sold at 30 days out, so beyond about a month the book carries
# almost no information and every model collapses onto the seasonal baseline.
MAX_HORIZON = 30

# Horizons reported in the summary table. Chosen to span operational planning
# (a week), staffing (a fortnight) and commercial review (a month).
REPORTED_HORIZONS = (1, 7, 14, 30)

# Trailing same-weekday observations used by the seasonal models.
DOW_WINDOW = 4

# Trailing days used by the plain moving average. Four weeks, so it contains a whole
# number of weekly cycles and cannot be biased by which weekdays fall inside it.
MA_WINDOW = 28

# Comparable same-weekday dates used to estimate remaining pickup.
PICKUP_WINDOW = 8

# Trailing days of REALISED inventory used to convert a room-night forecast into an
# occupancy percentage.
#
# THIS WINDOW ENDS AT THE ORIGIN, AND THAT IS THE WHOLE POINT. It read the last 28
# rows of the entire series until 2026-08-17, which meant a forecast made in October
# 2025 was divided by the capacity the portfolio had in August 2026. Sellable
# inventory here is not a constant: units open on dated schedules and the portfolio
# went from ~29.4 to ~38.9 sellable unit-nights per day in March 2026, so the bug
# understated capacity by a quarter for every origin before the expansion and
# overstated the resulting occupancy percentage by the same factor.
#
# Future capacity is not knowable at the origin even in principle. Out-of-order
# nights are drawn per unit-night in the generator, so any date's sellable count is
# settled only once that date has passed. Occupancy percentage is therefore derived
# from inventory the origin had actually seen, and the room-night forecast -- which
# is the primary output and unaffected by this -- is reported alongside it.
CAPACITY_WINDOW = 28


@dataclass
class Accuracy:
    model: str
    horizon: int
    n: int
    mae: float
    rmse: float
    mape: float | None
    bias: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "horizon_days": self.horizon,
            "observations": self.n,
            "mae_nights": round(self.mae, 2),
            "rmse_nights": round(self.rmse, 2),
            "mape_pct": None if self.mape is None else round(self.mape, 2),
            "bias_nights": round(self.bias, 2),
        }


# ---------------------------------------------------------------------------
def daily_actuals() -> pd.DataFrame:
    """Occupied room-nights per stay date, portfolio total. The truth series."""
    rows = db.fetch_all("""
        SELECT stay_date,
               count(*) FILTER (WHERE is_occupied)  AS occupied,
               count(*) FILTER (WHERE is_sellable)  AS sellable
        FROM mart.fact_unit_night
        GROUP BY 1 ORDER BY 1
    """)
    df = pd.DataFrame(rows)
    df["stay_date"] = pd.to_datetime(df["stay_date"])
    return df.set_index("stay_date").astype({"occupied": int, "sellable": int})


def otb_matrix(max_horizon: int = MAX_HORIZON,
               first: dt.date | None = None,
               last: dt.date | None = None) -> pd.DataFrame:
    """(stay_date, days_out) -> nights on the books at that horizon.

    Computed once. Every pickup-model forecast is then a lookup rather than another
    reconstruction, which is what keeps the backtest tractable.

    `first`/`last` bound the stay dates materialised. A rolling backtest wants the
    whole matrix and leaves them unset; a single forecast from one origin needs
    only its targets and the comparable dates behind it, and building the other
    eighteen months is most of the cost of that call.
    """
    rows = db.fetch_all(
        """
        WITH cal AS (
            SELECT DISTINCT stay_date FROM mart.fact_unit_night
            WHERE (CAST(:first AS date) IS NULL OR stay_date >= CAST(:first AS date))
              AND (CAST(:last  AS date) IS NULL OR stay_date <= CAST(:last  AS date))
        ),
             h   AS (SELECT generate_series(0, :maxh) AS days_out)
        SELECT c.stay_date, h.days_out, count(n.booking_key) AS nights_on_books
        FROM cal c
        CROSS JOIN h
        LEFT JOIN mart.v_booking_night n
               ON  n.stay_date   = c.stay_date
               AND n.entered_on <= c.stay_date - h.days_out
               AND (n.left_on IS NULL OR n.left_on > c.stay_date - h.days_out)
        GROUP BY 1, 2
        """,
        maxh=max_horizon,
        first=first,
        last=last,
    )
    df = pd.DataFrame(rows)
    df["stay_date"] = pd.to_datetime(df["stay_date"])
    return df.pivot(index="stay_date", columns="days_out", values="nights_on_books")


# ---------------------------------------------------------------------------
# Models. Each takes history strictly up to and including `origin` and returns a
# prediction for `target`. None of them may read `actuals` past the origin.
# ---------------------------------------------------------------------------
def _naive(hist: pd.Series, origin: pd.Timestamp, target: pd.Timestamp, **_: Any) -> float:
    return float(hist.iloc[-1])


def _seasonal_naive(hist: pd.Series, origin: pd.Timestamp, target: pd.Timestamp,
                    **_: Any) -> float:
    """Most recent same-weekday value at or before the origin."""
    same_dow = hist[hist.index.dayofweek == target.dayofweek]
    return float(same_dow.iloc[-1]) if len(same_dow) else float(hist.mean())


def _moving_average(hist: pd.Series, origin: pd.Timestamp, target: pd.Timestamp,
                    **_: Any) -> float:
    return float(hist.tail(MA_WINDOW).mean())


def _dow_moving_average(hist: pd.Series, origin: pd.Timestamp, target: pd.Timestamp,
                        **_: Any) -> float:
    same_dow = hist[hist.index.dayofweek == target.dayofweek]
    if not len(same_dow):
        return float(hist.mean())
    return float(same_dow.tail(DOW_WINDOW).mean())


def _pickup(hist: pd.Series, origin: pd.Timestamp, target: pd.Timestamp,
            otb: pd.DataFrame | None = None, **_: Any) -> float:
    """On the books now, plus the pickup comparable dates still received from here.

    The remaining-pickup term is estimated per weekday and per horizon from the last
    PICKUP_WINDOW comparable stay dates that had already completed by the origin.
    Falls back to the day-of-week seasonal mean when the book has no history to lean
    on, so it degrades to a sane model rather than to zero.
    """
    if otb is None or target not in otb.index:
        return _dow_moving_average(hist, origin, target)

    days_out = int((target - origin).days)
    if days_out not in otb.columns:
        return _dow_moving_average(hist, origin, target)

    on_books = otb.at[target, days_out]
    if pd.isna(on_books):
        return _dow_moving_average(hist, origin, target)

    # Comparable completed dates: same weekday, already realised by the origin.
    comparable = hist[hist.index.dayofweek == target.dayofweek].tail(PICKUP_WINDOW)
    if not len(comparable):
        return float(on_books)

    remaining = []
    for past in comparable.index:
        if past in otb.index and days_out in otb.columns:
            past_otb = otb.at[past, days_out]
            if not pd.isna(past_otb):
                remaining.append(float(comparable[past]) - float(past_otb))
    if not remaining:
        return float(on_books)

    return float(on_books) + float(np.median(remaining))


def _seasonal_holiday(hist: pd.Series, origin: pd.Timestamp, target: pd.Timestamp,
                      calendar: dict[str, Any] | None = None, **_: Any) -> float:
    """Day-of-week baseline scaled by a measured holiday multiplier.

    The multiplier comes from `signals.calendar`, estimated ONLY from holidays that
    had already completed before the backtest began -- so a forecast for Diwali can
    never be scaled by Diwali's own realised effect.

    Falls back in three steps: the specific holiday's multiplier at that offset,
    then the pooled cross-holiday multiplier at that offset, then no adjustment at
    all. A date with no holiday nearby is therefore identical to
    `dow_moving_average`, which is the intended behaviour -- this model claims to
    help near holidays and nowhere else.
    """
    base = _dow_moving_average(hist, origin, target)
    if not calendar:
        return base

    context = calendar.get("context", {}).get(target.date())
    if not context:
        return base

    holiday, offset = context
    specific = calendar.get("specific", {}).get((holiday, offset))
    if specific is not None:
        return base * specific

    # NO POOLED FALLBACK. This was tried and it made the model the worst of the
    # six, so it is worth recording why rather than just deleting it.
    #
    # Pooling a multiplier across all holidays assumes holidays are interchangeable.
    # They are not. Measured on this portfolio, Christmas runs -20.4pp and New Year
    # -11.5pp, while Id-ul-Fitr runs +10.9pp and Independence Day +4.9pp. Averaging
    # those produced pooled multipliers of 1.02 to 1.19 -- above 1, i.e. push the
    # forecast UP -- which were then applied to Christmas and New Year, the two
    # dates that collapse hardest. MAE on holiday-adjacent dates went from 4.19 to
    # 5.11 and bias flipped from -0.84 to +0.98.
    #
    # A holiday with no prior occurrence of its OWN gets no adjustment. The model
    # then degrades to its baseline, which is the correct behaviour for a model
    # that does not know the answer.
    return base


MODELS: dict[str, Callable[..., float]] = {
    "naive": _naive,
    "seasonal_naive": _seasonal_naive,
    "moving_average": _moving_average,
    "dow_moving_average": _dow_moving_average,
    "pickup": _pickup,
    "seasonal_holiday": _seasonal_holiday,
}


# ---------------------------------------------------------------------------
def calendar_context(before: dt.date) -> dict[str, Any]:
    """Holiday multipliers and per-date context for the `seasonal_holiday` model.

    NO-LEAKAGE DESIGN. The multipliers are estimated once, from data strictly
    before `before` -- normally the earliest origin in the backtest. Estimating
    them per origin would be marginally sharper and far slower; estimating them
    once at the START of the test period is strictly more conservative, because
    every origin then uses a multiplier fitted on less information than it could
    legitimately have had.

    That direction matters: a leak makes a model look better than it is, and this
    errs the other way.
    """
    from staypulse.signals import calendar as cal

    rows = db.fetch_all("""
        SELECT full_date, nearest_holiday, days_to_holiday
        FROM mart.dim_date
        WHERE is_holiday_adjacent AND nearest_holiday IS NOT NULL
    """)
    return {
        "estimated_before": before.isoformat(),
        "specific": cal.holiday_multiplier(before),
        "pooled": cal.generic_multiplier(before),
        "context": {
            r["full_date"]: (str(r["nearest_holiday"]), int(r["days_to_holiday"]))
            for r in rows
        },
    }


def backtest(
    test_days: int = 120,
    origin_step: int = 3,
    max_horizon: int = MAX_HORIZON,
) -> pd.DataFrame:
    """Rolling-origin backtest of every model.

    Returns one row per (model, origin, target) with the prediction and the truth.
    """
    actuals = daily_actuals()
    series = actuals["occupied"].astype(float)
    otb = otb_matrix(max_horizon)

    last = series.index.max()
    first_origin = last - pd.Timedelta(days=test_days)
    origins = [
        d for d in series.index
        if first_origin <= d <= last - pd.Timedelta(days=1)
    ][::origin_step]

    calendar = calendar_context(first_origin.date()) if origins else None

    records: list[dict[str, Any]] = []
    for origin in origins:
        # The only line that enforces the no-peeking rule. Everything downstream
        # sees `hist` and nothing else.
        hist = series.loc[:origin]
        if len(hist) < MA_WINDOW + 1:
            continue

        for h in range(1, max_horizon + 1):
            target = origin + pd.Timedelta(days=h)
            if target not in series.index:
                continue
            truth = float(series.loc[target])
            for name, fn in MODELS.items():
                records.append({
                    "model": name,
                    "origin": origin,
                    "target": target,
                    "horizon": h,
                    "prediction": fn(hist, origin, target, otb=otb,
                                     calendar=calendar),
                    "actual": truth,
                })

    df = pd.DataFrame(records)
    df["error"] = df["prediction"] - df["actual"]

    # Mark which targets are holiday-adjacent, so accuracy can be scored where the
    # holiday model actually claims to help.
    adjacent = {
        r["full_date"] for r in db.fetch_all(
            "SELECT full_date FROM mart.dim_date WHERE is_holiday_adjacent"
        )
    }
    df["holiday_adjacent"] = df["target"].dt.date.isin(adjacent)
    return df


def score(results: pd.DataFrame, horizons: tuple[int, ...] = REPORTED_HORIZONS
          ) -> list[Accuracy]:
    """MAE, RMSE, MAPE and bias per model per horizon."""
    out: list[Accuracy] = []
    for h in horizons:
        subset = results[results["horizon"] == h]
        for model in MODELS:
            rows = subset[subset["model"] == model]
            if rows.empty:
                continue
            err = rows["error"].to_numpy(dtype=float)
            actual = rows["actual"].to_numpy(dtype=float)
            # MAPE is undefined at zero and explodes near it. Guarded rather than
            # quietly producing a large number that looks like a model failure.
            safe = actual > 0
            mape = (
                float(np.mean(np.abs(err[safe] / actual[safe])) * 100)
                if safe.sum() >= 0.9 * len(actual) and safe.any()
                else None
            )
            out.append(Accuracy(
                model=model,
                horizon=h,
                n=len(rows),
                mae=float(np.mean(np.abs(err))),
                rmse=float(np.sqrt(np.mean(err ** 2))),
                mape=mape,
                bias=float(np.mean(err)),
            ))
    return out


def holiday_evaluation(test_days: int = 260) -> dict[str, Any]:
    """Score every model on holiday-adjacent dates specifically.

    WHY THIS EXISTS AS A SEPARATE EVALUATION

    The standard 120-day backtest window contains ZERO festival windows -- the
    three that fall inside the dataset are all earlier than mid-April 2026. Scored
    there, `seasonal_holiday` is identical to `dow_moving_average` by construction,
    because it applies no adjustment to a date with no holiday nearby.

    Reporting only that would be misleading in both directions: it would hide any
    real benefit, and it would hide any real harm. So the window is widened to
    reach the holidays, and accuracy is reported BOTH overall and on
    holiday-adjacent targets alone -- the only dates this model claims to improve.
    """
    results = backtest(test_days=test_days, origin_step=3)
    adjacent = results[results["holiday_adjacent"]]
    ordinary = results[~results["holiday_adjacent"]]

    def _score(df: pd.DataFrame) -> list[dict[str, Any]]:
        out = []
        for model in MODELS:
            rows = df[df["model"] == model]
            if rows.empty:
                continue
            err = rows["error"].to_numpy(dtype=float)
            out.append({
                "model": model,
                "observations": len(rows),
                "mae_nights": round(float(np.mean(np.abs(err))), 2),
                "rmse_nights": round(float(np.sqrt(np.mean(err ** 2))), 2),
                "bias_nights": round(float(np.mean(err)), 2),
            })
        return sorted(out, key=lambda d: d["mae_nights"])

    on_holidays = _score(adjacent)
    on_ordinary = _score(ordinary)

    return {
        "test_window_days": test_days,
        "holiday_adjacent_forecasts": int(len(adjacent)),
        "ordinary_forecasts": int(len(ordinary)),
        "accuracy_on_holiday_dates": on_holidays,
        "accuracy_on_ordinary_dates": on_ordinary,
        "best_on_holiday_dates": on_holidays[0]["model"] if on_holidays else None,
        "note": (
            "The 120-day window used for the headline backtest contains no festival "
            "window, so this evaluation widens it to reach them. A model that only "
            "adjusts holiday-adjacent dates cannot be judged on dates without one."
        ),
    }


def winners(scores: list[Accuracy]) -> dict[int, str]:
    """Lowest-MAE model per horizon. Reported whichever way it falls."""
    best: dict[int, tuple[str, float]] = {}
    for s in scores:
        if s.horizon not in best or s.mae < best[s.horizon][1]:
            best[s.horizon] = (s.model, s.mae)
    return {h: m for h, (m, _) in best.items()}


def forward(as_of: dt.date | None = None, horizon: int = MAX_HORIZON,
            model: str = "pickup") -> list[dict[str, Any]]:
    """Forecast forward from `as_of` with a named model.

    Defaults to the pickup model. Use `backtest` + `score` first if you want to know
    whether that default is justified on current data -- it is checked, not assumed.
    """
    actuals = daily_actuals()
    series = actuals["occupied"].astype(float)

    origin = pd.Timestamp(as_of) if as_of else series.index.max()
    # The pickup model reads the book for its targets and for the last
    # PICKUP_WINDOW comparable same-weekday dates behind the origin. Eight weeks
    # of lookback covers that with room to spare; the rest of the matrix is never
    # touched by a single-origin forecast.
    otb = otb_matrix(
        horizon,
        first=(origin - pd.Timedelta(days=7 * PICKUP_WINDOW + 14)).date(),
        last=(origin + pd.Timedelta(days=horizon)).date(),
    )
    hist = series.loc[:origin]
    fn = MODELS[model]

    # Capacity from inventory the origin had already seen. See CAPACITY_WINDOW.
    known_capacity = actuals["sellable"].loc[:origin].tail(CAPACITY_WINDOW)
    sellable_by_dow = known_capacity.groupby(known_capacity.index.dayofweek).mean()

    rows: list[dict[str, Any]] = []
    for h in range(1, horizon + 1):
        target = origin + pd.Timedelta(days=h)
        pred = fn(hist, origin, target, otb=otb)
        capacity = float(sellable_by_dow.get(target.dayofweek, np.nan))
        rows.append({
            "stay_date": target.date().isoformat(),
            "horizon_days": h,
            "predicted_room_nights": round(pred, 1),
            "predicted_occupancy_pct": (
                None if not capacity or np.isnan(capacity)
                else round(100.0 * pred / capacity, 1)
            ),
            "actual_room_nights": (
                float(series.loc[target]) if target in series.index else None
            ),
        })
    return rows


def summary(test_days: int = 120) -> dict[str, Any]:
    """Backtest, score, and say plainly which model won."""
    results = backtest(test_days=test_days)
    scores = score(results)
    best = winners(scores)
    return {
        "target": "daily occupied room-nights, portfolio total",
        "backtest": {
            "test_window_days": test_days,
            "origins": int(results["origin"].nunique()),
            "forecasts_evaluated": int(len(results)),
            "horizons": list(REPORTED_HORIZONS),
        },
        "accuracy": [s.as_dict() for s in scores],
        "best_by_horizon": best,
        "note": (
            "Rolling-origin backtest: every forecast uses only data at or before its "
            "origin. Seasonal naive is included because it is the bar a weekly-"
            "seasonal series sets, and a model that cannot beat it is not adding "
            "anything."
        ),
    }


# ---------------------------------------------------------------------------
# Backtesting lab (F-801): the same backtest, cut along the dimensions that
# change the answer.
#
# A single headline MAE hides where a model actually fails. The pickup model
# should be strong at three days and weak at thirty; a corporate portfolio should
# be harder to forecast at the weekend than midweek. Those are claims, and the
# point of slicing is that they become checkable instead of plausible.
# ---------------------------------------------------------------------------

# Observations required before a slice is scored. A cell with nine forecasts in
# it produces an MAE that reorders the model table on noise, and a lab that
# reports it is worse than one that omits it, because it invites a conclusion.
MIN_SLICE_OBSERVATIONS = 30

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday",
                 "Friday", "Saturday", "Sunday")


def _slice_scores(df: pd.DataFrame, min_observations: int) -> list[dict[str, Any]]:
    """MAE, RMSE and bias per model over one slice of the backtest."""
    out: list[dict[str, Any]] = []
    for model in MODELS:
        rows = df[df["model"] == model]
        if len(rows) < min_observations:
            continue
        err = rows["error"].to_numpy(dtype=float)
        out.append({
            "model": model,
            "observations": int(len(rows)),
            "mae_nights": round(float(np.mean(np.abs(err))), 3),
            "rmse_nights": round(float(np.sqrt(np.mean(err ** 2))), 3),
            "bias_nights": round(float(np.mean(err)), 3),
        })
    return sorted(out, key=lambda d: d["mae_nights"])


def slice_accuracy(results: pd.DataFrame,
                   min_observations: int = MIN_SLICE_OBSERVATIONS
                   ) -> dict[str, Any]:
    """Model accuracy cut by horizon, month, weekday and holiday adjacency.

    WHAT IS NOT SLICED HERE, AND WHY IT IS NOT AN OVERSIGHT

    There is no cut by property or by channel. The forecast target is portfolio
    total occupied room-nights -- one series -- so there is no per-property
    prediction to score, and slicing the ACTUALS by property while the forecast
    stays portfolio-wide would produce a number that looks like per-property
    accuracy and is not.

    Making that cut real needs a per-property forecast target: `daily_actuals`
    grouped by `property_key`, the pickup model's on-the-books matrix likewise,
    and a separate backtest per property. That is a different feature, and it
    would be forecasting a series of roughly ten room-nights a day per property,
    where the models behave differently enough that the portfolio results would
    not carry over. Named rather than approximated, per the standing rule.
    """
    frame = results.copy()
    frame["month"] = frame["target"].dt.to_period("M").astype(str)
    frame["weekday"] = frame["target"].dt.dayofweek

    def _grouped(column: str, label: Any = None) -> list[dict[str, Any]]:
        blocks = []
        for key, group in frame.groupby(column):
            scores = _slice_scores(group, min_observations)
            if not scores:
                continue
            blocks.append({
                "slice": label(key) if label else key,
                "forecasts": int(len(group)),
                "best_model": scores[0]["model"],
                "best_mae_nights": scores[0]["mae_nights"],
                "models": scores,
            })
        return blocks

    return {
        "min_observations_per_cell": min_observations,
        "by_horizon": [
            block for block in _grouped("horizon", label=lambda h: int(h))
            if int(block["slice"]) in REPORTED_HORIZONS
        ],
        "by_month": _grouped("month"),
        "by_weekday": _grouped("weekday", label=lambda d: WEEKDAY_NAMES[int(d)]),
        "by_holiday_adjacency": _grouped(
            "holiday_adjacent",
            label=lambda flag: "holiday_adjacent" if flag else "ordinary",
        ),
        "not_sliced": {
            "by_property": (
                "Requires a per-property forecast target. The backtest forecasts "
                "portfolio-total occupied room-nights, so no per-property "
                "prediction exists to score. See slice_accuracy.__doc__."
            ),
            "by_channel": (
                "Same reason, and channel is a booking attribute rather than an "
                "inventory one -- it does not partition unit-nights."
            ),
        },
    }


def lab(test_days: int = 365, origin_step: int = 3) -> dict[str, Any]:
    """The Backtesting Lab artifact: one backtest, reported from every angle.

    A wider window than the headline backtest, because the interesting slices --
    month, holiday adjacency -- need to reach dates the 120-day window never
    touches.
    """
    results = backtest(test_days=test_days, origin_step=origin_step)
    scores = score(results)
    sliced = slice_accuracy(results)

    disagreement = {
        str(block["slice"]): block["best_model"]
        for block in sliced["by_horizon"]
    }
    return {
        "target": "daily occupied room-nights, portfolio total",
        "window": {
            "test_days": test_days,
            "origins": int(results["origin"].nunique()),
            "forecasts_evaluated": int(len(results)),
            "models": list(MODELS),
        },
        "headline_accuracy": [s.as_dict() for s in scores],
        "best_by_horizon": disagreement,
        "slices": sliced,
        "note": (
            "One rolling-origin backtest, cut four ways. Every forecast in it uses "
            "only data at or before its own origin. Cells with fewer than "
            f"{MIN_SLICE_OBSERVATIONS} forecasts are omitted rather than reported, "
            "because an MAE over nine observations reorders the table on noise."
        ),
    }
