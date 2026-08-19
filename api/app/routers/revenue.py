"""Revenue management: on the books, pickup, pace, forecasting and root cause.

These endpoints are the forward-looking half of the API. Everything under
/api/kpis and /api/revenue/trends describes nights that have already happened;
everything here describes nights that have not.

TWO THINGS ARE DELIBERATE AND WORTH READING BEFORE USING THEM.

The as-of date defaults to a point where a full forward book exists in the dataset
rather than to the wall clock. This warehouse holds no reservations for arrivals
after its inventory horizon, so anchoring to today's real date would return an empty
book the moment the calendar moved past it, and the endpoint would look broken when
it was merely honest.

The forecast endpoints do not compute a backtest on the request path. Scoring five
models over forty origins takes about twelve seconds, which is not a page load.
Accuracy is served from the stored evaluation; /forecast/accuracy says when it was
produced.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, HTTPException, Query

from api.app import services

router = APIRouter(prefix="/api/revenue-management", tags=["revenue management"])


def _resolve_as_of(as_of: str | None) -> dt.date:
    """Parse an as-of date, defaulting to the last date with a full forward book."""
    if as_of is None:
        return services.default_as_of()
    try:
        parsed = dt.date.fromisoformat(as_of)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="as_of must be an ISO date, for example 2026-07-12.",
        ) from None
    bounds = services.data_bounds()
    if not (bounds["first"] <= parsed <= bounds["last"]):
        raise HTTPException(
            status_code=422,
            detail=(
                f"as_of must fall inside the dataset, "
                f"{bounds['first'].isoformat()} to {bounds['last'].isoformat()}."
            ),
        )
    return parsed


@router.get("/overview", summary="Forward position: on the books, pickup and pace")
def overview(as_of: str | None = Query(None, description="ISO date; defaults to the "
                                                         "last full forward book")) -> dict:
    return services.rm_overview(_resolve_as_of(as_of))


@router.get("/on-the-books", summary="The book as it stood on a given date")
def on_the_books(
    as_of: str | None = Query(None),
    horizon_days: int = Query(30, ge=1, le=35),
) -> dict:
    return services.rm_on_the_books(_resolve_as_of(as_of), horizon_days)


@router.get("/pickup", summary="Nights added and cancelled per activity date")
def pickup(
    as_of: str | None = Query(None),
    lookback_days: int = Query(14, ge=1, le=90),
) -> dict:
    return {
        "note": ("additions and cancellations are reported separately. A day that "
                 "booked 20 nights and lost 18 is a different story from one that "
                 "booked 2 and lost 0, and net pickup cannot tell them apart."),
        "pickup": services.rm_pickup(_resolve_as_of(as_of), lookback_days),
    }


@router.get("/pace", summary="Every future stay date scored against its own curve")
def pace(as_of: str | None = Query(None)) -> dict:
    return services.rm_pace(_resolve_as_of(as_of))


@router.get("/signals", summary="Evidence-backed forward signals")
def signals(as_of: str | None = Query(None), limit: int = Query(12, ge=1, le=50)) -> dict:
    return {
        "note": ("no signal recommends a price. There is no competitor rate feed and "
                 "no elasticity in this warehouse, so a rate recommendation would be "
                 "an opinion wearing a number."),
        "signals": services.rm_signals(_resolve_as_of(as_of), limit),
    }


@router.get("/booking-curve", summary="Share of the book normally sold by N days out")
def booking_curve() -> dict:
    return services.rm_booking_curve()


@router.get("/lead-time", summary="Lead-time distribution by channel")
def lead_time() -> dict:
    return {
        "note": ("percentiles rather than a mean: these distributions are heavily "
                 "skewed and a 12-day mean can describe a channel where half the "
                 "bookings are same-day."),
        "channels": services.rm_lead_time(),
    }


@router.get("/wash", summary="Cancellation and no-show funnel by stay-month cohort")
def wash() -> dict:
    return services.rm_wash()


@router.get("/grain-reconciliation",
            summary="How the demand grain ties to the inventory grain")
def grain_reconciliation() -> dict:
    return services.rm_grain_reconciliation()


@router.get("/forecast", summary="Forward occupancy forecast")
def forecast(
    horizon_days: int = Query(14, ge=1, le=30),
    model: str = Query("pickup",
                       pattern="^(pickup|seasonal_naive|naive|moving_average|dow_moving_average)$"),
) -> dict:
    return services.rm_forecast(horizon_days, model)


@router.get("/forecast/accuracy", summary="Rolling-origin backtest of every model")
def forecast_accuracy() -> dict:
    return services.rm_forecast_accuracy()


@router.get("/holiday-effect",
            summary="Measured public-holiday demand effect, by holiday and offset")
def holiday_effect() -> dict:
    return services.rm_holiday_effect()


@router.get("/holiday-effect/validation",
            summary="Measured effect against the generator's planted windows")
def holiday_validation() -> dict:
    return services.rm_holiday_validation()


@router.get("/forecast/holiday-evaluation",
            summary="Forecast accuracy on holiday-adjacent dates specifically")
def holiday_forecast_evaluation() -> dict:
    return services.rm_holiday_forecast_evaluation()


@router.get("/replay",
            summary="What StayPulse knew at a past date, and what it would have said")
def replay(
    as_of: str | None = Query(None, description="ISO date to replay"),
    horizon_days: int = Query(35, ge=1, le=35),
    with_outcome: bool = Query(True, description="Include what actually happened"),
) -> dict:
    """Reconstruct a past decision point without hindsight.

    The `decision` block is built only from what was knowable on the as-of date;
    each input names the rule that bounds it in `information_set`. The `outcome`
    block is the future, and it is produced by a separate call that takes the
    decision as its input rather than the other way round.

    Takes a few seconds: this reconstructs the book, the pace benchmark and a
    forecast from scratch at the requested date.
    """
    return services.rm_replay(_resolve_as_of(as_of), horizon_days, with_outcome)


@router.get("/replay/summary", summary="One replay, condensed to knew / said / happened")
def replay_summary(
    as_of: str | None = Query(None),
    horizon_days: int = Query(35, ge=1, le=35),
) -> dict:
    return services.rm_replay_summary(_resolve_as_of(as_of), horizon_days)


@router.get("/replay/evaluation",
            summary="Pooled replay accuracy across many historical origins")
def replay_evaluation() -> dict:
    return services.rm_replay_evaluation()


@router.get("/why", summary="Why did RevPAR change? Deterministic decomposition")
def why(
    days: int = Query(30, ge=7, le=120,
                      description="Length of the current window. The baseline is the "
                                  "immediately preceding window of the same length."),
    end: str | None = Query(None, description="ISO end date; defaults to the horizon"),
) -> dict:
    end_date = _resolve_as_of(end) if end else services.data_bounds()["last"]
    return services.rm_why(end_date, days)


@router.get("/forecast/intervals", summary="Forward forecast with prediction intervals")
def forecast_intervals(
    horizon_days: int = Query(14, ge=1, le=30),
    model: str = Query("pickup",
                       pattern="^(pickup|seasonal_naive|naive|moving_average|dow_moving_average)$"),
    level: float = Query(0.8, ge=0.5, le=0.95),
) -> dict:
    """A point forecast with an empirical interval around it.

    Calibrated on the model's own backtest residuals, using only residuals whose
    target had already been realised by the as-of date. A horizon without enough
    observed residuals returns null bounds rather than a fabricated number.
    """
    return services.rm_forecast_intervals(horizon_days, model, level)


@router.get("/forecast/intervals/coverage",
            summary="Did the intervals actually contain the truth?")
def forecast_interval_coverage() -> dict:
    return services.rm_interval_coverage()


@router.get("/backtest-lab",
            summary="Forecast accuracy sliced by horizon, month, weekday and holiday")
def backtest_lab() -> dict:
    return services.rm_backtest_lab()


@router.get("/alerts", summary="Every open alert from all four feeds, one queue")
def alert_center(as_of: str | None = Query(None)) -> dict:
    """Anomalies, data-quality failures, SLA breaches and pace need-dates.

    There is deliberately no severity score shared across sources: a robust z, a
    percentage of failing rows and a room-night shortfall are incommensurable.
    Each alert reports its own feed's measure with units named. The queue is
    ordered by actionability -- what you can still do about it -- which is the
    one axis that genuinely is comparable.
    """
    return services.rm_alert_center(_resolve_as_of(as_of))


@router.get("/alerts/summary", summary="Alert counts by source and actionability")
def alert_summary(as_of: str | None = Query(None)) -> dict:
    return services.rm_alert_summary(_resolve_as_of(as_of))


@router.get("/opportunities", summary="Stay dates filling ahead of their own curve")
def opportunity_radar(as_of: str | None = Query(None)) -> dict:
    """The upside half of pace analysis.

    Names no price: there is no competitor rate feed and no price elasticity in
    this warehouse, so a rate recommendation would be an opinion with a number
    attached.
    """
    return services.rm_opportunity_radar(_resolve_as_of(as_of))


@router.get("/cancellation-risk",
            summary="Cancellation model: performance, calibration and ground truth")
def cancellation_risk() -> dict:
    """Out-of-sample performance against a temporal split.

    Accuracy is not reported: on this base rate a model predicting 'never
    cancels' scores about 88% and is useless. Precision against the base rate,
    recall and the calibration curve are.
    """
    return services.rm_cancellation_model()


@router.get("/overbooking", summary="Overbooking outcomes for one stay date")
def overbooking(
    stay_date: str | None = Query(
        None, description="ISO stay date to simulate. Defaults to the date whose "
                          "book came closest to capacity, because on an undersold "
                          "date every overbooking level is walk-free."),
    as_of: str | None = Query(None),
    cost_ratio: float | None = Query(
        None, gt=0, le=200,
        description="Cost of walking a guest in units of one empty room. "
                    "Required for a recommendation; this warehouse prices "
                    "neither side, so there is deliberately no default."),
) -> dict:
    """Outcome distribution at each overbooking level.

    No level is recommended unless a cost ratio is supplied, because the optimum
    depends on the cost of walking a guest relative to an empty room and nothing
    in this warehouse prices either.
    """
    if stay_date is None:
        return services.rm_overbooking_default(cost_ratio)
    try:
        stay = dt.date.fromisoformat(stay_date)
    except ValueError:
        raise HTTPException(
            status_code=422, detail="stay_date must be an ISO date."
        ) from None
    return services.rm_overbooking(_resolve_as_of(as_of), stay, cost_ratio)


@router.get("/overbooking/wash", summary="Measured wash rate, overall and by channel")
def overbooking_wash() -> dict:
    return services.rm_overbooking_wash()
