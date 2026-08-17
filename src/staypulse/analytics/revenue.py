"""Revenue management analytics: on the books, pickup, pace, need dates.

WHAT THIS ANSWERS THAT THE REST OF THE WAREHOUSE CANNOT

Occupancy, ADR and RevPAR are settled numbers about nights that have already been
sold or lost. Nothing can be done about them. This module answers the forward
question -- for a night that has not happened yet, how much of it is already sold,
and is that ahead of or behind where it normally is by now.


WHERE THE BENCHMARK COMES FROM, AND WHY IT IS NOT A PERCENTAGE

The tempting definition of pace is "share of the final book already sold". It is
circular: for a future stay date the final book is exactly the unknown. Any
implementation that appears to compute it has quietly substituted a forecast for the
truth and then measured itself against its own forecast.

This module compares ABSOLUTE nights on the books against the absolute nights that
comparable prior stay dates carried at the SAME number of days out. Nothing about
the future is assumed, and the comparison is available the moment the horizon is.


WHY THE BENCHMARK IS DAY-OF-WEEK AWARE

This is a corporate aparthotel portfolio: weekday demand dominates and Saturday
behaves nothing like Tuesday. A pace benchmark pooled across all weekdays reports
every Saturday as catastrophically behind and every Tuesday as comfortably ahead,
forever. The same argument, and the same fix, as in `anomaly.py`.


DIVISION OF LABOUR WITH SQL

Business definitions live in the semantic layer -- `mart.v_booking_night`,
`mart.f_otb`, `mart.v_booking_curve` -- so Power BI and the API cannot disagree with
Python about what a booking-night is. What lives here is the STATISTICAL baseline:
grouped medians, minimum-support rules, thresholds. Those are estimates, not
definitions, they need unit tests more than they need governance, and they follow
the precedent already set by the anomaly detector.


WHAT THIS DELIBERATELY DOES NOT DO

It does not set prices and it does not recommend a rate. It surfaces a signal, the
evidence behind it and a confidence, and leaves the decision with a human. A
portfolio project that claims to be a revenue management system is claiming
something it cannot support; this claims to be the analysis that would feed one.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from staypulse import db

# How many recent comparable stay dates form the baseline: the last 8 same-weekday
# dates at the same property, roughly two months of history.
#
# THIS WINDOW IS LOAD-BEARING. The first implementation pooled all 18 months and
# reported 24 stay dates ahead of pace and ZERO behind -- which is not a portfolio
# booking well, it is a broken benchmark. Sellable inventory jumped from ~900 to
# ~1,200 unit-nights per month in March 2026 when the portfolio grew, so an
# all-history median compares a 40-unit portfolio against the period when it had
# about 30. Everything recent then looks ahead of a curve drawn on a smaller hotel.
#
# A trailing window tracks the level of the business instead of averaging over its
# history. Eight is the compromise: long enough for a stable median, short enough
# that a step change in inventory washes out within two months.
BENCHMARK_WINDOW = 8

# A stay date needs at least this many comparable historical observations at the
# same horizon and weekday before its pace is scored at all. Below it the median is
# noise wearing a number's clothes.
MIN_SUPPORT = 6

# HOW "BEHIND" AND "AHEAD" ARE DECIDED, and why not a fixed percentage.
#
# The obvious rule -- flag anything under 70% or over 140% of the median -- was
# tried and is wrong here. Measured on this portfolio, nights on the books for a
# single property nine days out ranges from 3 to 15 across comparable Tuesdays. A
# median of 6 and an observation of 14 is 233% and entirely ordinary. Fixed
# percentage bands on a small, highly dispersed count flag almost every date.
#
# So the band comes from the observed distribution instead, and a flag requires
# BOTH conditions to hold:
#
#   1. STATISTICAL -- outside the p25..p75 range of comparable history.
#   2. MATERIAL    -- at least MATERIAL_NIGHTS away from the median in absolute
#                     room-nights.
#
# Gate 2 is what stops a quiet property generating a stream of technically-unusual
# but operationally irrelevant alerts. This is the same dual-threshold reasoning the
# anomaly detector uses, for the same reason.
MATERIAL_NIGHTS = 4.0

# Below this many nights on the books the ratio is unstable -- going from 1 night to
# 2 is a 100% swing. Suppress rather than publish a meaningless percentage.
MIN_NIGHTS_FOR_PACE = 3

# Horizon beyond which this market simply has no signal. At 30 days out the median
# stay date here is 8% sold, so "behind pace" at 45 days is not information.
MAX_USEFUL_HORIZON = 35


@dataclass
class PaceRow:
    """One future stay date, scored against its own weekday's history."""

    stay_date: dt.date
    property_key: int
    property_name: str
    days_out: int
    nights_on_books: int
    expected_nights: float
    p25_nights: float
    p75_nights: float
    pace_pct: float
    support: int
    revenue_otb_inr: float

    @property
    def gap_nights(self) -> float:
        """Signed distance from the median, in room-nights."""
        return round(self.nights_on_books - self.expected_nights, 1)

    @property
    def status(self) -> str:
        """Dual gate: outside the historical band AND materially far from it."""
        material = abs(self.gap_nights) >= MATERIAL_NIGHTS
        if not material:
            return "on_track"
        if self.nights_on_books < self.p25_nights:
            return "behind"
        if self.nights_on_books > self.p75_nights:
            return "ahead"
        return "on_track"

    @property
    def confidence(self) -> str:
        """How much weight this score carries. See `_confidence`."""
        return _confidence(self)


@dataclass
class Signal:
    """An evidence-backed observation. Never an instruction to change a price."""

    kind: str
    stay_date: dt.date
    property_name: str
    headline: str
    evidence: list[str] = field(default_factory=list)
    confidence: str = "medium"
    suggested_investigation: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "stay_date": self.stay_date.isoformat(),
            "property": self.property_name,
            "headline": self.headline,
            "evidence": self.evidence,
            "confidence": self.confidence,
            "suggested_investigation": self.suggested_investigation,
        }


# ---------------------------------------------------------------------------
def data_horizon() -> dt.date:
    """Last stay date the warehouse holds inventory for.

    Every as-of calculation is anchored here rather than to the wall clock. The
    dataset is synthetic and fixed; using today's real date would silently produce
    an empty book the moment the calendar moved past the horizon.
    """
    return db.scalar("SELECT max(stay_date) FROM mart.fact_unit_night")


def on_the_books(as_of: dt.date, horizon_days: int = 30) -> list[dict[str, Any]]:
    """The book as it stood on `as_of`, by stay date and property."""
    return db.fetch_all(
        """
        SELECT o.stay_date,
               o.property_key,
               p.property_name,
               o.days_out,
               sum(o.nights_otb)                  AS nights_on_books,
               round(sum(o.revenue_otb_inr), 2)   AS revenue_otb_inr
        FROM mart.f_otb(:as_of) o
        JOIN mart.dim_property p USING (property_key)
        WHERE o.stay_date <= CAST(:as_of AS date) + :h
        GROUP BY 1, 2, 3, 4
        ORDER BY 1, 2
        """,
        as_of=as_of,
        h=horizon_days,
    )


def pickup(as_of: dt.date, lookback_days: int = 14) -> list[dict[str, Any]]:
    """Nights added and cancelled per activity date over the trailing window.

    Adds and cancellations are returned separately. A day that booked 20 nights and
    lost 18 is a different operational story from one that booked 2 and lost 0, and
    net pickup alone cannot distinguish them.
    """
    return db.fetch_all(
        """
        SELECT activity_date,
               sum(nights_added)                    AS nights_added,
               sum(nights_cancelled)                AS nights_cancelled,
               sum(nights_net)                      AS nights_net,
               round(sum(revenue_added_inr), 2)     AS revenue_added_inr,
               round(sum(revenue_cancelled_inr), 2) AS revenue_cancelled_inr
        FROM mart.v_pickup_daily
        WHERE activity_date BETWEEN CAST(:as_of AS date) - :lb AND :as_of
        GROUP BY 1
        ORDER BY 1
        """,
        as_of=as_of,
        lb=lookback_days,
    )


def _benchmark(as_of: dt.date) -> dict[tuple[int, int, int], tuple[float, int]]:
    """(property, weekday, days_out) -> (median nights on books, support).

    Built from the last `BENCHMARK_WINDOW` comparable stay dates strictly before
    `as_of`, so a pace score can never borrow information from after the moment it
    claims to be measured at. That is the difference between a backtest and a lie.

    The stay-date universe comes from `fact_unit_night`, not from bookings, so a
    weekday that genuinely had nothing on the books at a given horizon counts as the
    zero it was rather than vanishing from the baseline.
    """
    rows = db.fetch_all(
        """
        WITH horizons AS (SELECT generate_series(0, :maxh) AS days_out),
        calendar AS (
            SELECT DISTINCT stay_date, property_key
            FROM mart.fact_unit_night
            WHERE stay_date < :as_of
        ),
        ranked AS (
            SELECT stay_date, property_key,
                   EXTRACT(ISODOW FROM stay_date)::int AS dow,
                   row_number() OVER (
                       PARTITION BY property_key, EXTRACT(ISODOW FROM stay_date)
                       ORDER BY stay_date DESC
                   ) AS recency
            FROM calendar
        ),
        recent AS (SELECT * FROM ranked WHERE recency <= :win),
        snapshots AS (
            SELECT s.property_key,
                   s.dow,
                   h.days_out,
                   s.stay_date,
                   count(n.booking_key) AS nights_on_books
            FROM recent s
            CROSS JOIN horizons h
            LEFT JOIN mart.v_booking_night n
                   ON  n.stay_date    = s.stay_date
                   AND n.property_key = s.property_key
                   AND n.entered_on  <= s.stay_date - h.days_out
                   AND (n.left_on IS NULL OR n.left_on > s.stay_date - h.days_out)
            GROUP BY 1, 2, 3, 4
        )
        SELECT property_key, dow, days_out,
               percentile_cont(0.50) WITHIN GROUP (ORDER BY nights_on_books) AS median_nights,
               percentile_cont(0.25) WITHIN GROUP (ORDER BY nights_on_books) AS p25_nights,
               percentile_cont(0.75) WITHIN GROUP (ORDER BY nights_on_books) AS p75_nights,
               count(*) AS support
        FROM snapshots
        GROUP BY 1, 2, 3
        """,
        as_of=as_of,
        maxh=MAX_USEFUL_HORIZON,
        win=BENCHMARK_WINDOW,
    )
    return {
        (r["property_key"], r["dow"], r["days_out"]): (
            float(r["median_nights"]),
            float(r["p25_nights"]),
            float(r["p75_nights"]),
            int(r["support"]),
        )
        for r in rows
    }


def pace(as_of: dt.date, horizon_days: int = MAX_USEFUL_HORIZON) -> list[PaceRow]:
    """Score every future stay date against its own weekday's booking curve."""
    horizon_days = min(horizon_days, MAX_USEFUL_HORIZON)
    bench = _benchmark(as_of)
    out: list[PaceRow] = []

    for row in on_the_books(as_of, horizon_days):
        stay_date = row["stay_date"]
        key = (row["property_key"], stay_date.isoweekday(), row["days_out"])
        expected, p25, p75, support = bench.get(key, (0.0, 0.0, 0.0, 0))
        nights = int(row["nights_on_books"])

        if support < MIN_SUPPORT or expected <= 0 or nights < MIN_NIGHTS_FOR_PACE:
            continue

        out.append(
            PaceRow(
                stay_date=stay_date,
                property_key=row["property_key"],
                property_name=row["property_name"],
                days_out=row["days_out"],
                nights_on_books=nights,
                expected_nights=round(expected, 2),
                p25_nights=round(p25, 2),
                p75_nights=round(p75, 2),
                pace_pct=round(100.0 * nights / expected, 1),
                support=support,
                revenue_otb_inr=float(row["revenue_otb_inr"] or 0),
            )
        )
    return out


def need_dates(as_of: dt.date, horizon_days: int = MAX_USEFUL_HORIZON,
               scored: list[PaceRow] | None = None) -> list[PaceRow]:
    """Future stay dates running behind their own weekday's curve, worst first.

    Ranked by absolute room-night shortfall rather than by percentage: eight nights
    missing from a large property matters more than a 50% gap on a date carrying
    four nights, and a percentage ranking inverts that.

    `scored` lets a caller that has already run `pace` for this as-of date reuse
    the result. The benchmark query behind it is the most expensive in the module
    and it was previously being run three times to answer one request.
    """
    rows = pace(as_of, horizon_days) if scored is None else scored
    return sorted((p for p in rows if p.status == "behind"), key=lambda p: p.gap_nights)


def constrained_dates(as_of: dt.date, horizon_days: int = MAX_USEFUL_HORIZON,
                      scored: list[PaceRow] | None = None) -> list[PaceRow]:
    """Future stay dates running ahead of curve -- the other half of the job.

    Pace analysis that only surfaces weak dates is half an instrument. A date filling
    unusually early is the one where inventory is about to run out at a rate that was
    set before anyone knew demand would be strong.
    """
    rows = pace(as_of, horizon_days) if scored is None else scored
    return sorted((p for p in rows if p.status == "ahead"), key=lambda p: -p.gap_nights)


def opportunity_signals(as_of: dt.date, limit: int = 12,
                        scored: list[PaceRow] | None = None) -> list[Signal]:
    """Evidence-backed forward signals.

    Every signal states what was measured, against what baseline, with how much
    historical support. None of them names a price, because nothing in this
    warehouse supports a rate recommendation: there is no competitor rate feed, no
    price elasticity and no booking-level rate history to fit one on.
    """
    signals: list[Signal] = []
    scored = pace(as_of) if scored is None else scored
    behind = need_dates(as_of, scored=scored)
    ahead = constrained_dates(as_of, scored=scored)

    for row in behind[: limit // 2]:
        signals.append(
            Signal(
                kind="soft_demand",
                stay_date=row.stay_date,
                property_name=row.property_name,
                headline=(
                    f"{row.stay_date:%a %d %b} is {abs(row.gap_nights):.0f} room-nights "
                    f"behind its usual position with {row.days_out} days to go"
                ),
                evidence=[
                    f"{row.nights_on_books} nights on the books",
                    f"typically {row.expected_nights:.1f} by this point on a "
                    f"{row.stay_date:%A} (usual range "
                    f"{row.p25_nights:.0f}-{row.p75_nights:.0f})",
                    f"shortfall {abs(row.gap_nights):.1f} nights, "
                    f"{row.pace_pct:.0f}% of the median",
                    f"baseline from the last {row.support} comparable "
                    f"{row.stay_date:%A}s at this property",
                ],
                confidence=_confidence(row),
                suggested_investigation=(
                    "Check whether the gap is demand or mix: compare channel pickup "
                    "for this date against the same weekday, and confirm no rate or "
                    "availability restriction is suppressing it."
                ),
            )
        )

    for row in ahead[: limit - len(signals)]:
        signals.append(
            Signal(
                kind="demand_strength",
                stay_date=row.stay_date,
                property_name=row.property_name,
                headline=(
                    f"{row.stay_date:%a %d %b} is {row.gap_nights:.0f} room-nights "
                    f"ahead of its usual position with {row.days_out} days to go"
                ),
                evidence=[
                    f"{row.nights_on_books} nights on the books",
                    f"typically {row.expected_nights:.1f} by this point on a "
                    f"{row.stay_date:%A} (usual range "
                    f"{row.p25_nights:.0f}-{row.p75_nights:.0f})",
                    f"surplus {row.gap_nights:.1f} nights, "
                    f"{row.pace_pct:.0f}% of the median",
                    f"baseline from the last {row.support} comparable "
                    f"{row.stay_date:%A}s at this property",
                ],
                confidence=_confidence(row),
                suggested_investigation=(
                    "Filling early. Verify remaining inventory and check whether the "
                    "rate on the remaining units was set before this demand appeared."
                ),
            )
        )

    return signals


def _confidence(row: PaceRow) -> str:
    """Confidence in a pace score.

    Driven by how much history backs the baseline and how far out the horizon is.
    Long horizons in a short-lead-time market carry almost no information, and
    saying so is more useful than attaching a number to noise.

    Support is capped at BENCHMARK_WINDOW by construction, so the thresholds are
    expressed against that window rather than against absolute counts.
    """
    full = BENCHMARK_WINDOW
    if row.days_out > 21 or row.support < full - 1:
        return "low"
    if row.days_out > 10 or row.support < full:
        return "medium"
    return "high"


def summary(as_of: dt.date | None = None) -> dict[str, Any]:
    """Portfolio forward position -- the one call the API and briefing both use."""
    as_of = as_of or data_horizon()
    scored = pace(as_of)
    pick = pickup(as_of, lookback_days=14)

    added = sum(int(p["nights_added"] or 0) for p in pick)
    cancelled = sum(int(p["nights_cancelled"] or 0) for p in pick)

    otb_rows = on_the_books(as_of, 30)
    return {
        "as_of": as_of.isoformat(),
        "horizon_days": MAX_USEFUL_HORIZON,
        "nights_on_books_30d": sum(int(r["nights_on_books"]) for r in otb_rows),
        "revenue_on_books_30d_inr": round(
            sum(float(r["revenue_otb_inr"] or 0) for r in otb_rows), 2
        ),
        "pickup_14d_nights_added": added,
        "pickup_14d_nights_cancelled": cancelled,
        "pickup_14d_nights_net": added - cancelled,
        "stay_dates_scored": len(scored),
        "behind_pace": sum(1 for p in scored if p.status == "behind"),
        "on_track": sum(1 for p in scored if p.status == "on_track"),
        "ahead_of_pace": sum(1 for p in scored if p.status == "ahead"),
        "note": (
            "Pace compares absolute nights on the books against the median for the "
            "same property, same weekday and same days-out horizon, built only from "
            "stay dates before the as-of date."
        ),
    }
