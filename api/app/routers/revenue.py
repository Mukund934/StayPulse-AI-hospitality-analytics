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


@router.get("/why", summary="Why did RevPAR change? Deterministic decomposition")
def why(
    days: int = Query(30, ge=7, le=120,
                      description="Length of the current window. The baseline is the "
                                  "immediately preceding window of the same length."),
    end: str | None = Query(None, description="ISO end date; defaults to the horizon"),
) -> dict:
    end_date = _resolve_as_of(end) if end else services.data_bounds()["last"]
    return services.rm_why(end_date, days)
