"""Scenario engine: what the arithmetic says if something changed.

A SCENARIO IS NOT A FORECAST, AND THE DISTINCTION IS THE WHOLE FEATURE

A forecast answers "what is going to happen", carries a model, and can be scored
against reality -- which is what `analytics.forecast` and `analytics.intervals`
do, with a coverage test to prove the intervals mean something.

A scenario answers "what would the books say if occupancy were five points
higher". It carries no model, predicts nothing, and cannot be right or wrong. It
is arithmetic on an identity, and its entire value is that it is exact.

Confusing the two is the most common way a what-if tool becomes dishonest: a
number produced by holding ADR fixed and moving occupancy gets presented as a
projection, and a reader assumes someone believes it will happen. Every result
here is labelled `scenario`, states what it held constant, and never claims the
change is achievable. A test scans the output for forecast vocabulary.


WHAT THIS REFUSES TO DO

It will not tell you how to make occupancy rise five points, or what it would
cost, or whether the revenue is capturable. Those questions need price
elasticity and a demand response, and this warehouse contains neither -- the same
gap that keeps `opportunity_signals` from naming a price and keeps the
overbooking simulator from naming a level.

So the honest output is: here is the arithmetic consequence, here is exactly what
was held fixed, and here is the assumption you are making if you act on it.


THE INTERACTION TERM

RevPAR = Occupancy x ADR is multiplicative, so a scenario moving both levers has
an interaction term that has to go somewhere. Assigning it to one lever flatters
that lever; dropping it makes the parts stop summing to the whole.

`analytics.rootcause` already settled this for observed movements, using the
symmetric Shapley split:

    occupancy contribution = d(Occ) x mean(ADR_before, ADR_after)
    rate contribution      = d(ADR) x mean(Occ_before, Occ_after)

This module uses the SAME convention, because a scenario decomposition that
attributed the interaction differently from the root-cause decomposition would
let the two disagree about the same movement. The sum is asserted exact in tests.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from staypulse import db

# Commission is charged GST at 18%, so the cost of an OTA night is the commission
# plus the tax on it. `services.revenue_channels` already nets channel economics
# this way; the same multiplier is used here so the two cannot disagree.
GST_ON_COMMISSION = 1.18

# Lever values swept in the published sensitivity tables.
OCCUPANCY_SWEEP_PP: tuple[float, ...] = (-10.0, -5.0, -2.0, 2.0, 5.0, 10.0)
ADR_SWEEP_PCT: tuple[float, ...] = (-10.0, -5.0, -2.0, 2.0, 5.0, 10.0)


@dataclass
class Position:
    """A settled trading position. The baseline a scenario departs from."""

    rooms_available: int
    rooms_sold: int
    revenue_inr: float
    commission_inr: float

    @property
    def occupancy(self) -> float:
        return self.rooms_sold / self.rooms_available if self.rooms_available else 0.0

    @property
    def adr(self) -> float:
        return self.revenue_inr / self.rooms_sold if self.rooms_sold else 0.0

    @property
    def revpar(self) -> float:
        return self.revenue_inr / self.rooms_available if self.rooms_available else 0.0

    @property
    def net_revenue_inr(self) -> float:
        """Room revenue after commission and the GST charged on commission."""
        return self.revenue_inr - self.commission_inr * GST_ON_COMMISSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "rooms_available": self.rooms_available,
            "rooms_sold": self.rooms_sold,
            "occupancy_pct": round(100 * self.occupancy, 3),
            "adr_inr": round(self.adr, 2),
            "revpar_inr": round(self.revpar, 2),
            "room_revenue_inr": round(self.revenue_inr, 2),
            "commission_inr": round(self.commission_inr, 2),
            "net_revenue_inr": round(self.net_revenue_inr, 2),
        }


@dataclass
class Scenario:
    """The arithmetic consequence of a stated change. Never a prediction."""

    baseline: Position
    result: Position
    levers: dict[str, Any]
    assumptions: list[str] = field(default_factory=list)

    @property
    def revpar_change(self) -> float:
        return self.result.revpar - self.baseline.revpar

    def decomposition(self) -> dict[str, Any]:
        """Split the RevPAR movement between occupancy and rate.

        Symmetric (Shapley) split, matching `rootcause` exactly. The two
        contributions sum to the total movement with no residual, which the test
        suite asserts rather than this docstring claiming.
        """
        d_occ = self.result.occupancy - self.baseline.occupancy
        d_adr = self.result.adr - self.baseline.adr
        mean_adr = (self.baseline.adr + self.result.adr) / 2.0
        mean_occ = (self.baseline.occupancy + self.result.occupancy) / 2.0

        occupancy_contribution = d_occ * mean_adr
        rate_contribution = d_adr * mean_occ
        total = occupancy_contribution + rate_contribution

        return {
            "method": (
                "symmetric (Shapley) split, identical to analytics.rootcause: "
                "occupancy contribution = d(Occ) x mean(ADR), rate contribution "
                "= d(ADR) x mean(Occ). The two sum to the movement exactly."
            ),
            "occupancy_contribution_inr": round(occupancy_contribution, 4),
            "rate_contribution_inr": round(rate_contribution, 4),
            "total_revpar_change_inr": round(total, 4),
            "residual_inr": round(self.revpar_change - total, 10),
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "result_type": "scenario",
            "is_forecast": False,
            "what_this_is": (
                "The arithmetic consequence of the stated change, holding "
                "everything else fixed. It predicts nothing and cannot be right "
                "or wrong. It is not a projection and nobody has claimed the "
                "change will happen or that it is achievable."
            ),
            "levers": self.levers,
            "baseline": self.baseline.as_dict(),
            "scenario": self.result.as_dict(),
            "change": {
                "occupancy_pp": round(
                    100 * (self.result.occupancy - self.baseline.occupancy), 3),
                "adr_inr": round(self.result.adr - self.baseline.adr, 2),
                "revpar_inr": round(self.revpar_change, 2),
                "room_revenue_inr": round(
                    self.result.revenue_inr - self.baseline.revenue_inr, 2),
                "net_revenue_inr": round(
                    self.result.net_revenue_inr - self.baseline.net_revenue_inr, 2),
            },
            "decomposition": self.decomposition(),
            "assumptions_held_constant": self.assumptions,
        }


# ---------------------------------------------------------------------------
def baseline(start: dt.date | None = None,
             end: dt.date | None = None) -> Position:
    """The settled position over a window, from the semantic layer."""
    row = db.fetch_all(
        """
        SELECT count(*) FILTER (WHERE is_sellable)          AS rooms_available,
               count(*) FILTER (WHERE is_occupied)          AS rooms_sold,
               coalesce(sum(room_revenue_net_inr), 0)       AS revenue,
               coalesce(sum(commission_inr), 0)             AS commission
        FROM mart.fact_unit_night
        WHERE (CAST(:start AS date) IS NULL OR stay_date >= CAST(:start AS date))
          AND (CAST(:end   AS date) IS NULL OR stay_date <= CAST(:end   AS date))
        """,
        start=start,
        end=end,
    )[0]
    return Position(
        rooms_available=int(row["rooms_available"]),
        rooms_sold=int(row["rooms_sold"]),
        revenue_inr=float(row["revenue"]),
        commission_inr=float(row["commission"]),
    )


def apply_levers(
    position: Position,
    occupancy_pp: float = 0.0,
    adr_pct: float = 0.0,
    capacity_units_pct: float = 0.0,
) -> Scenario:
    """Move occupancy, rate and capacity, and report what the identity gives.

    OCCUPANCY is moved in percentage POINTS, not percent. "Occupancy up 5" is
    ambiguous in ordinary speech and the two readings differ by a factor of
    fifteen at this portfolio's occupancy, so the unit is in the parameter name.

    CAPACITY holds rooms SOLD constant rather than occupancy, capped at the new
    capacity. Taking a unit out of service does not create demand, and at 76%
    occupancy there is slack to absorb it; the cap is what stops the arithmetic
    selling rooms that no longer exist. The opposite convention -- holding
    occupancy fixed -- would quietly assume demand scales with inventory, which
    is a commercial claim rather than arithmetic.
    """
    assumptions: list[str] = []

    rooms_available = position.rooms_available
    if capacity_units_pct:
        rooms_available = max(
            0, round(position.rooms_available * (1 + capacity_units_pct / 100.0))
        )
        assumptions.append(
            "Rooms sold are held constant when capacity changes, capped at the "
            "new capacity: removing inventory does not remove demand, and adding "
            "it does not create demand."
        )

    occupancy = position.occupancy + occupancy_pp / 100.0
    occupancy = min(max(occupancy, 0.0), 1.0)
    if occupancy_pp:
        assumptions.append(
            "ADR is held constant while occupancy moves. This warehouse contains "
            "no price elasticity, so the engine cannot say what rate change would "
            "be needed to shift occupancy, nor what selling more nights would do "
            "to the achieved rate."
        )

    if capacity_units_pct and not occupancy_pp:
        rooms_sold = min(position.rooms_sold, rooms_available)
    else:
        rooms_sold = round(occupancy * rooms_available)

    adr = position.adr * (1 + adr_pct / 100.0)
    if adr_pct:
        assumptions.append(
            "Occupancy is held constant while ADR moves. In a real market a rate "
            "rise costs volume; with no elasticity in this warehouse that "
            "trade-off cannot be quantified, so it is excluded rather than "
            "guessed."
        )

    # Commission scales with the revenue it is charged on, at the observed
    # blended rate. Nothing here re-prices a channel.
    commission_rate = (
        position.commission_inr / position.revenue_inr
        if position.revenue_inr else 0.0
    )
    revenue = adr * rooms_sold

    if not assumptions:
        assumptions.append("No lever was moved; this is the baseline restated.")

    return Scenario(
        baseline=position,
        result=Position(
            rooms_available=rooms_available,
            rooms_sold=int(rooms_sold),
            revenue_inr=revenue,
            commission_inr=revenue * commission_rate,
        ),
        levers={
            "occupancy_pp": occupancy_pp,
            "adr_pct": adr_pct,
            "capacity_units_pct": capacity_units_pct,
        },
        assumptions=assumptions,
    )


# ---------------------------------------------------------------------------
def channel_economics() -> list[dict[str, Any]]:
    """Measured ADR and commission per occupied night, by channel."""
    rows = db.fetch_all(
        """
        SELECT c.channel_code,
               count(*)                                              AS nights,
               sum(e.room_revenue_net_inr)                           AS revenue,
               sum(e.commission_inr)                                 AS commission
        FROM mart.v_unit_night_enriched e
        JOIN mart.dim_channel c ON c.channel_key = e.channel_key
        WHERE e.is_occupied
        GROUP BY 1 ORDER BY nights DESC
        """
    )
    return [
        {
            "channel": row["channel_code"],
            "nights": int(row["nights"]),
            "adr_inr": round(float(row["revenue"]) / int(row["nights"]), 2),
            "commission_per_night_inr": round(
                float(row["commission"]) / int(row["nights"]), 2),
            "net_per_night_inr": round(
                (float(row["revenue"]) - float(row["commission"]) * GST_ON_COMMISSION)
                / int(row["nights"]), 2),
        }
        for row in rows
    ]


def shift_channel_mix(from_channel: str, to_channel: str,
                      share_pct: float) -> dict[str, Any]:
    """Move a share of one channel's nights to another, and price the difference.

    THE ONE LEVER HERE WITH REAL ECONOMICS BEHIND IT. Commission per night is
    measured, not assumed: OTA nights carry 685-900 INR while corporate, direct
    and walk-in carry nothing, so moving a night between them has a computable
    effect on net revenue rather than a hypothetical one.

    THE ASSUMPTION THAT MAKES IT A SCENARIO AND NOT A PLAN. It assumes the demand
    transfers -- that a guest who booked through an OTA would have booked direct
    if the OTA had not been there. Nothing in this warehouse supports that, and
    for some of these channels it is plainly false: a walk-in is a walk-in
    because they walked in. The arithmetic is exact; its premise is not
    evidence.
    """
    economics = {row["channel"]: row for row in channel_economics()}
    if from_channel not in economics or to_channel not in economics:
        raise KeyError(
            f"unknown channel; known channels are {sorted(economics)}"
        )

    source, target = economics[from_channel], economics[to_channel]
    nights_moved = round(source["nights"] * share_pct / 100.0)

    revenue_change = nights_moved * (target["adr_inr"] - source["adr_inr"])
    commission_change = nights_moved * (
        target["commission_per_night_inr"] - source["commission_per_night_inr"]
    )
    net_change = nights_moved * (
        target["net_per_night_inr"] - source["net_per_night_inr"]
    )

    return {
        "result_type": "scenario",
        "is_forecast": False,
        "lever": {
            "from_channel": from_channel,
            "to_channel": to_channel,
            "share_pct": share_pct,
            "nights_moved": nights_moved,
        },
        "from_economics": source,
        "to_economics": target,
        "change": {
            "room_revenue_inr": round(revenue_change, 2),
            "commission_inr": round(commission_change, 2),
            "net_revenue_inr": round(net_change, 2),
            "net_per_night_inr": round(
                target["net_per_night_inr"] - source["net_per_night_inr"], 2),
        },
        "assumptions_held_constant": [
            "Demand transfers in full: every night moved would still have been "
            "sold through the receiving channel. Nothing in this warehouse "
            "supports that, and for walk-in and corporate it is plainly false.",
            "Rate per night in each channel is the measured average and does not "
            "change with volume.",
            "No acquisition cost. Commission is recorded; the marketing spend "
            "needed to move a booking direct is not, so the saving shown here is "
            "gross of whatever it would cost to achieve.",
        ],
        "note": (
            "Commission is netted at "
            f"{GST_ON_COMMISSION:.2f}x to include the GST charged on it, matching "
            "the channel economics endpoint."
        ),
    }


# ---------------------------------------------------------------------------
def sensitivity(position: Position | None = None) -> dict[str, Any]:
    """How RevPAR responds across a sweep of each lever, one at a time."""
    position = position or baseline()
    return {
        "result_type": "scenario",
        "is_forecast": False,
        "baseline": position.as_dict(),
        "occupancy_pp": [
            {
                "lever_pp": value,
                "revpar_inr": round(
                    apply_levers(position, occupancy_pp=value).result.revpar, 2),
                "revpar_change_inr": round(
                    apply_levers(position, occupancy_pp=value).revpar_change, 2),
            }
            for value in OCCUPANCY_SWEEP_PP
        ],
        "adr_pct": [
            {
                "lever_pct": value,
                "revpar_inr": round(
                    apply_levers(position, adr_pct=value).result.revpar, 2),
                "revpar_change_inr": round(
                    apply_levers(position, adr_pct=value).revpar_change, 2),
            }
            for value in ADR_SWEEP_PCT
        ],
        "note": (
            "One lever at a time, each holding the other constant. Combining "
            "them is multiplicative rather than additive, which `apply_levers` "
            "handles and a naive reading of this table would not."
        ),
    }


def summary() -> dict[str, Any]:
    """The published artifact."""
    position = baseline()
    return {
        "result_type": "scenario",
        "is_forecast": False,
        "separation_note": (
            "A scenario is not a forecast. `analytics.forecast` predicts what "
            "will happen and is scored against reality; this computes what the "
            "identity gives if something changed, predicts nothing, and cannot "
            "be right or wrong. Nothing here should be presented as a projection."
        ),
        "baseline": position.as_dict(),
        "identity": (
            "RevPAR = ADR x Occupancy, and revenue = ADR x rooms sold. Every "
            "figure below follows from those two and nothing else."
        ),
        "examples": [
            apply_levers(position, occupancy_pp=5.0).as_dict(),
            apply_levers(position, adr_pct=5.0).as_dict(),
            apply_levers(position, occupancy_pp=5.0, adr_pct=5.0).as_dict(),
        ],
        "sensitivity": sensitivity(position),
        "channel_mix_example": shift_channel_mix("MMT", "DIRECT", 25.0),
        "what_this_cannot_do": [
            "Say how to achieve any of these changes.",
            "Say what a rate change would cost in volume, or what selling more "
            "nights would do to the achieved rate. That needs price elasticity, "
            "which this warehouse does not contain.",
            "Claim any of the revenue shown is capturable.",
        ],
    }
