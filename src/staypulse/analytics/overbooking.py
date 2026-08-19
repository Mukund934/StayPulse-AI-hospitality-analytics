"""Overbooking simulator: what happens if you sell more rooms than you have.

THE NUMBER THIS MODULE REFUSES TO PRODUCE

Every overbooking treatment ends with an optimal level, and it is always the same
arithmetic: accept one more booking while the expected cost of the extra walk is
below the expected cost of the extra empty room. That needs a COST RATIO --

    cost of walking a guest / cost of an empty room

-- and this warehouse does not contain one. There is no relocation cost, no
compensation field, no goodwill model, nothing that prices a guest turned away
against a room left unsold. `walk_in` here is a booking channel and `relocation`
is a guest segment; neither is the cost of a walk.

So this module does not choose a level. It computes the OUTCOME DISTRIBUTION at
every level -- probability of walking anyone, expected walks, expected empty
rooms -- which is fully determined by the data, and it reports the BREAKEVEN cost
ratio at which each level starts to pay, which is also fully determined by the
data. Supplying the ratio is the operator's job, because only the operator knows
what walking a guest costs their business.

A `cost_ratio` argument is accepted and, when given, a recommendation is
returned. What is never returned is a recommendation with a ratio invented to
produce it.


HOW SHOWS ARE MODELLED

Each booking on the books either arrives or washes. Given per-booking survival
probabilities the number of arrivals is Poisson-binomial, and its distribution is
computed exactly by convolution rather than simulated -- with forty bookings on a
date, an exact answer costs less than a Monte Carlo run and does not wobble
between invocations.

Two sources of survival probability are supported:

  MEASURED   One wash rate for the whole book, from `v_cancellation_funnel`.
             This is what an overbooking policy is normally built on.
  MODELLED   Per-booking probabilities from the F-703 cancellation model, which
             knows that a corporate booking made three days out is far more
             likely to arrive than an OTA booking made two months out.

The second is not automatically better and the difference is reported rather
than assumed. Heterogeneous probabilities give a TIGHTER arrival distribution
than a homogeneous rate with the same mean -- the variance of a Poisson-binomial
is lower than the binomial with equal mean -- so the modelled version will
generally justify slightly more aggressive overbooking. Whether that is real or
an artefact of the model's own calibration is exactly the sort of claim this
project does not make without evidence, so both are published.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import numpy as np

from staypulse import db
from staypulse.analytics import revenue as rv

# Overbooking levels evaluated: rooms sold beyond physical capacity.
MAX_OVERBOOK = 10

# Cost ratios shown in the sensitivity table. Spans the range a hotel might
# plausibly hold -- a walk costing the same as an empty room through a walk
# costing fifty times more -- because the whole point is that the answer moves
# with it and the reader should see how much.
SENSITIVITY_RATIOS: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 20.0, 50.0)


@dataclass
class LevelOutcome:
    """What happens if you accept `overbook` bookings beyond capacity."""

    overbook: int
    accepted: int
    capacity: int
    expected_arrivals: float
    p_any_walk: float
    expected_walks: float
    expected_empty: float
    breakeven_cost_ratio: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "overbook_by": self.overbook,
            "bookings_accepted": self.accepted,
            "physical_capacity": self.capacity,
            "expected_arrivals": round(self.expected_arrivals, 2),
            "probability_of_walking_anyone_pct": round(100 * self.p_any_walk, 2),
            "expected_walks": round(self.expected_walks, 3),
            "expected_empty_rooms": round(self.expected_empty, 3),
            "breakeven_cost_ratio": (
                None if self.breakeven_cost_ratio is None
                else round(self.breakeven_cost_ratio, 2)
            ),
        }


# ---------------------------------------------------------------------------
def arrival_distribution(survival: np.ndarray) -> np.ndarray:
    """Exact Poisson-binomial pmf for the number of arrivals.

    Convolution rather than simulation: with a few dozen bookings this is
    cheaper than a Monte Carlo run and, more importantly, deterministic. A
    simulated overbooking recommendation that changes on re-run is not a
    recommendation.
    """
    pmf = np.zeros(len(survival) + 1)
    pmf[0] = 1.0
    for index, probability in enumerate(survival, start=1):
        # Shift-and-mix: arrivals either gain this booking or do not.
        pmf[1:index + 1] = (
            pmf[1:index + 1] * (1 - probability) + pmf[0:index] * probability
        )
        pmf[0] *= (1 - probability)
    return pmf


def _outcomes(pmf: np.ndarray, capacity: int) -> tuple[float, float, float, float]:
    """Expected arrivals, P(walk), E[walks], E[empty] for a physical capacity."""
    arrivals = np.arange(len(pmf))
    expected_arrivals = float(arrivals @ pmf)
    over = np.maximum(arrivals - capacity, 0)
    under = np.maximum(capacity - arrivals, 0)
    return (
        expected_arrivals,
        float(pmf[arrivals > capacity].sum()),
        float(over @ pmf),
        float(under @ pmf),
    )


def simulate(capacity: int, survival: np.ndarray,
             max_overbook: int = MAX_OVERBOOK) -> list[LevelOutcome]:
    """Outcome distribution at each overbooking level.

    `survival` holds one arrival probability per booking already on the books.
    Each extra accepted booking is given the MEAN survival probability of the
    existing book, because nothing is known about a reservation that has not
    been made yet -- assuming the marginal booking behaves like the average one
    is the weakest assumption available, and it is stated rather than buried.
    """
    if capacity <= 0:
        return []
    mean_survival = float(np.mean(survival)) if len(survival) else 0.0
    outcomes: list[LevelOutcome] = []

    for extra in range(max_overbook + 1):
        book = np.concatenate([survival, np.full(extra, mean_survival)])
        pmf = arrival_distribution(book)
        expected_arrivals, p_walk, walks, empty = _outcomes(pmf, capacity)

        # Breakeven: the cost ratio at which moving from `extra-1` to `extra`
        # stops paying. Below it the extra booking is worth taking; above it the
        # walk risk dominates. Derived from the data alone -- no cost required.
        breakeven: float | None = None
        if outcomes:
            previous = outcomes[-1]
            extra_walks = walks - previous.expected_walks
            rooms_saved = previous.expected_empty - empty
            if extra_walks > 1e-12:
                breakeven = rooms_saved / extra_walks

        outcomes.append(LevelOutcome(
            overbook=extra,
            accepted=len(survival) + extra,
            capacity=capacity,
            expected_arrivals=expected_arrivals,
            p_any_walk=p_walk,
            expected_walks=walks,
            expected_empty=empty,
            breakeven_cost_ratio=breakeven,
        ))
    return outcomes


def recommend(outcomes: list[LevelOutcome], cost_ratio: float) -> dict[str, Any]:
    """The level that minimises expected cost AT A RATIO THE CALLER SUPPLIED.

    Expected cost is `cost_ratio * expected_walks + expected_empty`, in units of
    one empty room. The ratio is an input and never a default: this warehouse
    prices neither side of it.
    """
    if not outcomes:
        return {"cost_ratio": cost_ratio, "recommended_overbook": None}

    costs = [
        (level.overbook, cost_ratio * level.expected_walks + level.expected_empty)
        for level in outcomes
    ]
    best, best_cost = min(costs, key=lambda pair: pair[1])
    # An optimum sitting on the last level searched is not an optimum, it is the
    # edge of the search. Saying so matters: on an undersold date every level up
    # to the cap is walk-free, and reporting the cap as a recommendation would
    # dress "we never looked further" up as an answer.
    at_boundary = best == outcomes[-1].overbook
    baseline = next(cost for overbook, cost in costs if overbook == 0)
    chosen = next(level for level in outcomes if level.overbook == best)

    return {
        "cost_ratio": cost_ratio,
        "cost_ratio_meaning": (
            "cost of walking one guest, expressed in units of one empty room"
        ),
        "recommended_overbook": best,
        "recommendation_at_search_boundary": at_boundary,
        "boundary_note": (
            None if not at_boundary else
            f"The optimum sits at the highest level searched ({best}). The book "
            "does not reach capacity within that range, so this is the edge of "
            "the search rather than a genuine optimum -- raise max_overbook to "
            "find where the trade-off actually turns."
        ),
        "expected_cost_in_empty_room_units": round(best_cost, 3),
        "expected_cost_if_not_overbooking": round(baseline, 3),
        "improvement_vs_no_overbooking": round(baseline - best_cost, 3),
        "expected_walks_at_recommendation": round(chosen.expected_walks, 3),
        "probability_of_walking_anyone_pct": round(100 * chosen.p_any_walk, 2),
        "caveat": (
            "This recommendation is a function of the cost ratio supplied. The "
            "warehouse contains no relocation cost, compensation figure or "
            "goodwill model, so the ratio cannot be derived from it and no "
            "default is offered."
        ),
    }


def sensitivity(outcomes: list[LevelOutcome],
                ratios: tuple[float, ...] = SENSITIVITY_RATIOS) -> list[dict[str, Any]]:
    """How the recommendation moves as the cost ratio moves.

    The honest substitute for an optimum. If the recommended level is flat
    across a wide band of ratios, the choice barely matters and that is worth
    knowing; if it swings, the operator needs to price a walk before acting, and
    that is worth knowing too.
    """
    return [
        {
            "cost_ratio": ratio,
            "recommended_overbook": recommend(outcomes, ratio)["recommended_overbook"],
            "expected_walks": next(
                round(level.expected_walks, 3) for level in outcomes
                if level.overbook == recommend(outcomes, ratio)["recommended_overbook"]
            ),
        }
        for ratio in ratios
    ]


# ---------------------------------------------------------------------------
def measured_wash_rate() -> dict[str, Any]:
    """Portfolio wash rate from the cancellation funnel, and by channel."""
    overall = db.fetch_all(
        """
        SELECT count(*)                                                   AS made,
               count(*) FILTER (WHERE status IN ('cancelled','no_show'))  AS washed
        FROM mart.fact_booking
        """
    )[0]
    by_channel = db.fetch_all(
        """
        SELECT c.channel_code,
               count(*)                                                   AS made,
               count(*) FILTER (WHERE b.status IN ('cancelled','no_show')) AS washed,
               round(100.0 * count(*) FILTER (WHERE b.status IN ('cancelled','no_show'))
                     / count(*), 2)                                        AS wash_pct
        FROM mart.fact_booking b
        JOIN mart.dim_channel c USING (channel_key)
        GROUP BY 1 ORDER BY wash_pct DESC
        """
    )
    rate = float(overall["washed"]) / float(overall["made"])
    return {
        "bookings": int(overall["made"]),
        "washed": int(overall["washed"]),
        "wash_rate_pct": round(100 * rate, 2),
        "survival_rate_pct": round(100 * (1 - rate), 2),
        "by_channel": [
            {
                "channel": row["channel_code"],
                "bookings": int(row["made"]),
                "washed": int(row["washed"]),
                "wash_rate_pct": float(row["wash_pct"]),
            }
            for row in by_channel
        ],
    }


def book_on_hand(as_of: dt.date, stay_date: dt.date) -> dict[str, Any]:
    """The live book and physical capacity for one stay date, as of a date.

    Uses `f_otb`, so the book is the one that stood on `as_of` with no
    hindsight -- the same primitive the decision replay is built on.
    """
    rows = db.fetch_all(
        """
        SELECT coalesce(sum(o.nights_otb), 0) AS on_books
        FROM mart.f_otb(:as_of) o
        WHERE o.stay_date = :stay_date
        """,
        as_of=as_of,
        stay_date=stay_date,
    )
    capacity = db.scalar(
        """
        SELECT count(*) FILTER (WHERE is_sellable)
        FROM mart.fact_unit_night WHERE stay_date = :d
        """,
        d=stay_date,
    )
    return {
        "as_of": as_of,
        "stay_date": stay_date,
        "on_books": int(rows[0]["on_books"]),
        "capacity": int(capacity or 0),
    }


def simulate_stay_date(as_of: dt.date, stay_date: dt.date,
                       max_overbook: int = MAX_OVERBOOK,
                       cost_ratio: float | None = None) -> dict[str, Any]:
    """Overbooking outcomes for one stay date, under a measured wash rate.

    `cost_ratio` is optional and has no default. Supplying it adds a
    recommendation; omitting it returns the outcome table and the breakeven
    column, which is everything the data can support on its own.
    """
    position = book_on_hand(as_of, stay_date)
    wash = measured_wash_rate()
    survival_rate = 1.0 - wash["wash_rate_pct"] / 100.0
    survival = np.full(position["on_books"], survival_rate)
    outcomes = simulate(position["capacity"], survival, max_overbook)

    payload: dict[str, Any] = {
        "as_of": as_of.isoformat(),
        "stay_date": stay_date.isoformat(),
        "on_books": position["on_books"],
        "capacity": position["capacity"],
        "survival_rate_pct": round(100 * survival_rate, 2),
        "source": "measured portfolio wash rate",
        "levels": [level.as_dict() for level in outcomes],
        "sensitivity": sensitivity(outcomes),
        "no_optimum_note": (
            "No single recommended level is given. The optimum depends on the "
            "cost of walking a guest relative to an empty room, and this "
            "warehouse prices neither. The breakeven column states the ratio at "
            "which each level begins to pay; supplying the ratio is the "
            "operator's decision, not this module's."
        ),
    }
    if cost_ratio is not None:
        payload["recommendation"] = recommend(outcomes, cost_ratio)
    return payload


def homogeneous_vs_modelled(capacity: int = 30, bookings: int = 32
                            ) -> dict[str, Any]:
    """Does using per-booking probabilities change the answer?

    Compares one wash rate for the whole book against per-booking probabilities
    with the SAME mean. Any difference is pure variance: a Poisson-binomial is
    tighter than the binomial with equal mean, so heterogeneity concentrates the
    arrival distribution and makes overbooking look safer.

    Published because it is the kind of difference that would otherwise be
    presented as the model "improving" the policy, when part of it is a
    mathematical consequence of heterogeneity rather than better prediction.
    """
    wash = measured_wash_rate()
    mean_survival = 1.0 - wash["wash_rate_pct"] / 100.0

    homogeneous = np.full(bookings, mean_survival)
    # Spread around the same mean using the observed channel wash spread.
    channel_rates = np.array(
        [1.0 - row["wash_rate_pct"] / 100.0 for row in wash["by_channel"]]
    )
    tiled = np.resize(channel_rates, bookings)
    heterogeneous = np.clip(tiled - (tiled.mean() - mean_survival), 0.01, 0.99)

    flat = simulate(capacity, homogeneous, MAX_OVERBOOK)
    spread = simulate(capacity, heterogeneous, MAX_OVERBOOK)

    return {
        "capacity": capacity,
        "bookings_on_hand": bookings,
        "mean_survival_pct": round(100 * mean_survival, 2),
        "homogeneous": {
            "expected_arrivals": flat[0].expected_arrivals,
            "p_walk_at_zero_overbook_pct": round(100 * flat[0].p_any_walk, 2),
            "sd_of_arrivals": round(
                float(_sd(arrival_distribution(homogeneous))), 3),
        },
        "heterogeneous": {
            "expected_arrivals": spread[0].expected_arrivals,
            "p_walk_at_zero_overbook_pct": round(100 * spread[0].p_any_walk, 2),
            "sd_of_arrivals": round(
                float(_sd(arrival_distribution(heterogeneous))), 3),
        },
        "note": (
            "Same mean survival, different spread. A tighter arrival "
            "distribution justifies more aggressive overbooking, and that "
            "narrowing is a property of heterogeneity itself rather than "
            "evidence that the per-booking model predicts well. Its predictive "
            "quality is measured separately, in the cancellation model's "
            "calibration table."
        ),
    }


def _sd(pmf: np.ndarray) -> float:
    arrivals = np.arange(len(pmf))
    mean = arrivals @ pmf
    return float(np.sqrt(((arrivals - mean) ** 2) @ pmf))


def tightest_stay_date(days_out: int = 7) -> dict[str, Any]:
    """The stay date whose book came closest to filling, at `days_out`.

    The published example is chosen rather than fixed. An arbitrary date in this
    portfolio is undersold -- the median date runs well below capacity -- and on
    an undersold date every overbooking level is walk-free, which makes the
    simulator look either trivial or broken. The interesting case is the date
    where the trade-off is real, so that is the one reported.
    """
    row = db.fetch_all(
        """
        WITH cap AS (
            SELECT stay_date, count(*) FILTER (WHERE is_sellable) AS capacity
            FROM mart.fact_unit_night GROUP BY 1
        ),
        otb AS (
            SELECT stay_date, count(*) AS on_books
            FROM mart.v_booking_night
            WHERE entered_on <= stay_date - :d
              AND (left_on IS NULL OR left_on > stay_date - :d)
            GROUP BY 1
        )
        SELECT cap.stay_date, cap.capacity, coalesce(otb.on_books, 0) AS on_books
        FROM cap LEFT JOIN otb USING (stay_date)
        WHERE cap.capacity > 0
        ORDER BY coalesce(otb.on_books, 0)::float / cap.capacity DESC
        LIMIT 1
        """,
        d=days_out,
    )[0]
    return {
        "stay_date": row["stay_date"],
        "capacity": int(row["capacity"]),
        "on_books": int(row["on_books"]),
        "days_out": days_out,
        "as_of": row["stay_date"] - dt.timedelta(days=days_out),
    }


def summary(as_of: dt.date | None = None) -> dict[str, Any]:
    """The published artifact, on the date where overbooking is a live decision."""
    if as_of is None:
        tightest = tightest_stay_date()
        as_of, stay_date = tightest["as_of"], tightest["stay_date"]
    else:
        stay_date = as_of + dt.timedelta(days=7)

    return {
        "wash": measured_wash_rate(),
        "example": simulate_stay_date(as_of, stay_date),
        "example_selection": (
            "The stay date whose book came closest to capacity at 7 days out. "
            "Chosen rather than fixed: most dates in this portfolio are "
            "undersold, and on an undersold date every overbooking level is "
            "walk-free, which shows nothing."
        ),
        "variance_effect": homogeneous_vs_modelled(),
        "what_is_missing": {
            "cost_of_walking_a_guest": (
                "Not present in this warehouse. No relocation cost, "
                "compensation field or goodwill model exists. Without it the "
                "optimal overbooking level is not computable, and any figure "
                "presented as one would be an assumption wearing a decimal "
                "point."
            ),
            "what_would_unblock_it": (
                "A relocation cost per walked guest -- typically the rate paid "
                "at the receiving property plus transport plus any goodwill "
                "credit -- recorded per incident. Then the breakeven column "
                "here becomes a decision rather than a table."
            ),
        },
    }
