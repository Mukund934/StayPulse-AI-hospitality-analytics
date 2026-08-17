"""Decision replay: what StayPulse could have known at a past moment, and what
it would have said.

WHAT THIS IS FOR

Every forward-looking number in this warehouse -- on the books, pickup, pace, the
forecast -- is claimed to be computable without hindsight. That claim is easy to
make and easy to get wrong, and its failure mode is silent: a leaked result looks
*better*, not broken. This module takes a past date T, rebuilds the whole
analytical picture as it stood then, and reports what the system would have said,
so the claim can be inspected rather than believed.

Then, separately, it shows what actually happened.


THE ONE STRUCTURAL IDEA

`reconstruct(T)` and `outcome(...)` are two functions, not one function with a
flag. The reconstruction never receives the outcome -- not as an argument, not as
a field it ignores, not as a column it filters out later. A single function that
computed both and then hid half would be one careless edit away from scoring
itself against its own answer, and nothing in the output would look different.

That separation is what the leakage tests hang off. `reconstruct` is pure with
respect to the future, so inserting bookings dated after T must leave its output
bit-for-bit identical, and `fingerprint` reduces that to one comparable string.


THREE KINDS OF KNOWABLE, AND WHY THE DISTINCTION MATTERS

Not everything about the future is unknowable, and pretending otherwise would be
its own distortion. Each source in INFORMATION_SET carries one of three bases:

  REALISED   Settled facts about nights that have already happened: occupancy,
             ADR, the inventory that turned out to be sellable. Bounded by
             stay_date <= T.

  AS_BOOKED  The book as it stood at T -- reservations entered by then and not
             yet cancelled by then. This describes future stay dates, which is
             the point, but it uses only what the reservation system held at T.
             Bounded by entered_on <= T AND (left_on IS NULL OR left_on > T).

  EX_ANTE    Genuinely knowable in advance, because it is published in advance.
             The public-holiday calendar is the only member here: Diwali 2026 was
             a known date in 2025. Treating it as unknowable would understate the
             system, and treating anything else as ex-ante would overstate it.

The measured *effect* of a holiday is not ex-ante. The date is published; the
demand response has to be estimated from holidays that have already occurred, and
that estimate is bounded by T like everything else.


WHAT IS DELIBERATELY NOT HERE

No counterfactual. This replays what the system would have said, not what would
have happened had someone acted on it. There is no price elasticity in this
warehouse, so "revenue we would have captured" is unanswerable and any number
attached to it would be invented. F-1103 is the place for that conversation, and
it stays labelled a simulation when it arrives.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from staypulse import db
from staypulse.analytics import forecast as fc
from staypulse.analytics import revenue as rv

# Forward window the replay reconstructs. Matches the pace horizon, which is
# capped at the point this market stops carrying signal.
DEFAULT_HORIZON = rv.MAX_USEFUL_HORIZON

# Trailing pickup window shown in the reconstruction. Two weeks is the operational
# review period and matches what the /pickup endpoint defaults to.
PICKUP_LOOKBACK = 14

# Trailing realised nights summarised as context for the decision.
REALISED_LOOKBACK = 28

# Model the replay forecasts with. `pickup` is the default everywhere else in the
# project because the stored backtest picks it at short horizons; the replay does
# not re-derive that choice, it inherits it, and records which model it used.
DEFAULT_MODEL = "pickup"

# --- temporal bases --------------------------------------------------------
REALISED = "realised"
AS_BOOKED = "as_booked"
EX_ANTE = "ex_ante"


@dataclass(frozen=True)
class Source:
    """One input to a replayed decision, with the rule that bounds it in time."""

    name: str
    basis: str
    rule: str
    contributes: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.name,
            "basis": self.basis,
            "temporal_rule": self.rule,
            "contributes": self.contributes,
        }


# The manifest. Every query this module runs is answerable from exactly one of
# these, and each entry states the predicate that keeps it behind T.
INFORMATION_SET: tuple[Source, ...] = (
    Source(
        name="mart.f_otb(T)",
        basis=AS_BOOKED,
        rule="entered_on <= T AND (left_on IS NULL OR left_on > T) AND stay_date > T",
        contributes="nights and revenue on the books for each future stay date",
    ),
    Source(
        name="mart.v_pickup_daily",
        basis=AS_BOOKED,
        rule="activity_date <= T",
        contributes="nights added and cancelled per day over the trailing window",
    ),
    Source(
        name="mart.v_booking_night (pace benchmark)",
        basis=REALISED,
        rule="stay_date < T, sampled at the same days-out horizon",
        contributes="the same-weekday booking curve each future date is scored against",
    ),
    Source(
        name="mart.fact_unit_night",
        basis=REALISED,
        rule="stay_date <= T",
        contributes="occupied room-nights history, and the sellable inventory the "
                    "occupancy percentage is divided by",
    ),
    Source(
        name="mart.dim_date (holiday calendar)",
        basis=EX_ANTE,
        rule="none -- statutory and lunar calendars are published years ahead",
        contributes="which future stay dates are public holidays or adjacent to one",
    ),
    Source(
        name="staypulse.signals.calendar (measured effect)",
        basis=REALISED,
        rule="estimated only from holidays whose dates fall strictly before T",
        contributes="how much a holiday moved occupancy, where one has already occurred",
    ),
)


@dataclass
class DecisionState:
    """Everything knowable at `as_of`, and what the system would have said.

    Contains no post-`as_of` fact of any kind. `outcome()` is a separate call
    precisely so that nothing here can quietly become an input to itself.
    """

    as_of: dt.date
    horizon_days: int
    model: str
    book: dict[str, Any]
    pickup: dict[str, Any]
    realised: dict[str, Any]
    pace: dict[str, Any]
    calendar: list[dict[str, Any]]
    holiday_evidence: list[dict[str, Any]]
    forecast: list[dict[str, Any]]
    signals: list[dict[str, Any]]
    information_set: list[dict[str, Any]] = field(
        default_factory=lambda: [s.as_dict() for s in INFORMATION_SET]
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "horizon_days": self.horizon_days,
            "forecast_model": self.model,
            "information_set": self.information_set,
            "book": self.book,
            "pickup": self.pickup,
            "realised": self.realised,
            "pace": self.pace,
            "calendar": self.calendar,
            "holiday_evidence": self.holiday_evidence,
            "forecast": self.forecast,
            "signals": self.signals,
        }

    @property
    def fingerprint(self) -> str:
        """Stable digest of the whole reconstruction.

        The leakage tests reduce to one equality on this string. A hash is used
        rather than a field-by-field comparison so that a field added later is
        covered automatically instead of being silently exempt.
        """
        canonical = json.dumps(self.as_dict(), sort_keys=True, default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class Outcome:
    """What actually happened. Never an input to a `DecisionState`."""

    as_of: dt.date
    forecast_accuracy: list[dict[str, Any]]
    pace_calls: dict[str, Any]
    coverage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "forecast_accuracy": self.forecast_accuracy,
            "pace_calls": self.pace_calls,
            "coverage": self.coverage,
        }


# ---------------------------------------------------------------------------
# Reconstruction. Nothing below this line may read a fact dated after `as_of`,
# except the public-holiday calendar, which is published in advance.
# ---------------------------------------------------------------------------
def _book(as_of: dt.date, horizon_days: int) -> dict[str, Any]:
    """Position on the books at `as_of`, totals and per stay date."""
    rows = rv.on_the_books(as_of, horizon_days)
    by_date: dict[dt.date, dict[str, float]] = {}
    for r in rows:
        entry = by_date.setdefault(
            r["stay_date"], {"nights": 0, "revenue": 0.0, "days_out": int(r["days_out"])}
        )
        entry["nights"] += int(r["nights_on_books"])
        entry["revenue"] += float(r["revenue_otb_inr"] or 0)

    return {
        "nights_on_books": sum(int(r["nights_on_books"]) for r in rows),
        "revenue_on_books_inr": round(
            sum(float(r["revenue_otb_inr"] or 0) for r in rows), 2
        ),
        "stay_dates_with_inventory_sold": len(by_date),
        "by_stay_date": [
            {
                "stay_date": d.isoformat(),
                "days_out": v["days_out"],
                "nights_on_books": int(v["nights"]),
                "revenue_on_books_inr": round(v["revenue"], 2),
            }
            for d, v in sorted(by_date.items())
        ],
    }


def _pickup(as_of: dt.date) -> dict[str, Any]:
    """Trailing booking activity as it stood at `as_of`."""
    rows = rv.pickup(as_of, lookback_days=PICKUP_LOOKBACK)
    added = sum(int(r["nights_added"] or 0) for r in rows)
    cancelled = sum(int(r["nights_cancelled"] or 0) for r in rows)
    return {
        "lookback_days": PICKUP_LOOKBACK,
        "nights_added": added,
        "nights_cancelled": cancelled,
        "nights_net": added - cancelled,
        "revenue_added_inr": round(
            sum(float(r["revenue_added_inr"] or 0) for r in rows), 2
        ),
        "active_days": len(rows),
    }


def _realised(as_of: dt.date) -> dict[str, Any]:
    """Settled performance over the trailing window. Nothing here is forward."""
    row = db.fetch_all(
        """
        SELECT count(*) FILTER (WHERE is_occupied)             AS occupied,
               count(*) FILTER (WHERE is_sellable)             AS sellable,
               count(DISTINCT stay_date)                       AS days,
               round(sum(room_revenue_net_inr), 2)             AS revenue
        FROM mart.fact_unit_night
        WHERE stay_date BETWEEN CAST(:as_of AS date) - :lb AND :as_of
        """,
        as_of=as_of,
        lb=REALISED_LOOKBACK,
    )[0]
    occupied = int(row["occupied"] or 0)
    sellable = int(row["sellable"] or 0)
    return {
        "lookback_days": REALISED_LOOKBACK,
        "days_observed": int(row["days"] or 0),
        "occupied_room_nights": occupied,
        "sellable_room_nights": sellable,
        "occupancy_pct": round(100.0 * occupied / sellable, 2) if sellable else None,
        "revenue_inr": float(row["revenue"] or 0),
    }


def _final_book_benchmark(as_of: dt.date) -> dict[tuple[int, int], tuple[float, int]]:
    """(property, weekday) -> median FINAL book of comparable completed dates.

    WHY THIS EXISTS, AND WHY IT IS PART OF THE DECISION RATHER THAN THE OUTCOME.

    Pace answers "is this date where it normally is BY NOW". Judging that call
    against what the date finally carried needs a second number -- what comparable
    dates finally carried -- and the first version of this module did not have it.
    It compared the final book against `expected_nights`, which is the median book
    at the same days-out horizon, so almost every date cleared it simply because
    bookings kept arriving after the snapshot. The measured base rate was 7.3%,
    which is not a base rate, it is a unit error.

    The correct comparator is the median final book of the same benchmark set pace
    already uses: the last BENCHMARK_WINDOW same-weekday dates at the same property
    before T. Those dates have completed by T, so their final book is settled and
    knowable then -- which is why this belongs to the reconstruction and is bounded
    like everything else in it.

    `entered_on <= stay_date` counts a night only if it was on the books when the
    date arrived, so a reservation keyed to a past date but entered afterwards
    cannot inflate a historical final book.
    """
    rows = db.fetch_all(
        """
        WITH calendar AS (
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
        finals AS (
            SELECT r.property_key, r.dow, r.stay_date,
                   count(n.booking_key) AS final_nights
            FROM recent r
            LEFT JOIN mart.v_booking_night n
                   ON  n.stay_date    = r.stay_date
                   AND n.property_key = r.property_key
                   AND n.entered_on  <= r.stay_date
                   AND (n.left_on IS NULL OR n.left_on > r.stay_date)
            GROUP BY 1, 2, 3
        )
        SELECT property_key, dow,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY final_nights) AS median_final,
               count(*) AS support
        FROM finals
        GROUP BY 1, 2
        """,
        as_of=as_of,
        win=rv.BENCHMARK_WINDOW,
    )
    return {
        (int(r["property_key"]), int(r["dow"])): (
            float(r["median_final"]),
            int(r["support"]),
        )
        for r in rows
    }


def _pace(as_of: dt.date, rows: list[rv.PaceRow]) -> dict[str, Any]:
    """Every scorable future date against its own weekday's curve, as of T."""
    finals = _final_book_benchmark(as_of)
    return {
        "scored": len(rows),
        "behind": sum(1 for r in rows if r.status == "behind"),
        "on_track": sum(1 for r in rows if r.status == "on_track"),
        "ahead": sum(1 for r in rows if r.status == "ahead"),
        "stay_dates": [
            {
                "stay_date": r.stay_date.isoformat(),
                "property": r.property_name,
                "property_key": r.property_key,
                "days_out": r.days_out,
                "nights_on_books": r.nights_on_books,
                "expected_nights": r.expected_nights,
                "usual_range": [r.p25_nights, r.p75_nights],
                "gap_nights": r.gap_nights,
                "status": r.status,
                "support": r.support,
                "confidence": r.confidence,
                "expected_final_nights": (
                    None
                    if (r.property_key, r.stay_date.isoweekday()) not in finals
                    else round(finals[(r.property_key, r.stay_date.isoweekday())][0], 2)
                ),
            }
            for r in sorted(rows, key=lambda p: (p.stay_date, p.property_key))
        ],
    }


def _calendar(as_of: dt.date, horizon_days: int) -> list[dict[str, Any]]:
    """Holiday context for the forward window.

    The only forward-dated input in the reconstruction, and the only one that
    needs no temporal bound: a public-holiday calendar is published years ahead,
    so knowing that a date inside the window is Diwali is not hindsight. What it
    is *worth* is a different question, answered by `_holiday_evidence` under the
    usual bound.
    """
    rows = db.fetch_all(
        """
        SELECT full_date, is_public_holiday, holiday_name, holiday_confidence,
               nearest_holiday, days_to_holiday, is_holiday_adjacent,
               is_long_weekend, is_bridge_day
        FROM mart.dim_date
        WHERE full_date > :as_of
          AND full_date <= CAST(:as_of AS date) + :h
          AND (is_public_holiday OR is_holiday_adjacent OR is_bridge_day)
        ORDER BY full_date
        """,
        as_of=as_of,
        h=horizon_days,
    )
    return [
        {
            "stay_date": r["full_date"].isoformat(),
            "days_out": (r["full_date"] - as_of).days,
            "is_public_holiday": bool(r["is_public_holiday"]),
            "holiday_name": r["holiday_name"],
            "holiday_confidence": r["holiday_confidence"],
            "nearest_holiday": r["nearest_holiday"],
            "days_to_holiday": (
                None if r["days_to_holiday"] is None else int(r["days_to_holiday"])
            ),
            "is_holiday_adjacent": bool(r["is_holiday_adjacent"]),
            "is_long_weekend": bool(r["is_long_weekend"]),
            "is_bridge_day": bool(r["is_bridge_day"]),
        }
        for r in rows
    ]


def _holiday_evidence(as_of: dt.date) -> list[dict[str, Any]]:
    """Measured holiday effects available at T -- from holidays already past.

    A holiday appearing in `_calendar` for the first time has no row here, and
    that absence is the honest answer rather than a gap to be filled. It is also
    the finding from F-102: on eighteen months of data most holidays never get a
    prior occurrence, which is why the holiday-aware forecast lost.
    """
    from staypulse.signals import calendar as cal

    return [e.as_dict() for e in cal.holiday_effects(as_of)]


def _forecast(as_of: dt.date, horizon_days: int, model: str) -> list[dict[str, Any]]:
    """Forward forecast from T, with the realised column removed.

    `forecast.forward` returns `actual_room_nights` as a convenience for the live
    endpoint, where the horizon runs past the end of the data and the column is
    always null. At a historical origin it is populated, and it is exactly the
    thing this module must not carry into a decision. Dropped here rather than
    ignored downstream: a field that is present but unused is one refactor away
    from being used.
    """
    rows = fc.forward(as_of=as_of, horizon=horizon_days, model=model)
    return [
        {
            "stay_date": r["stay_date"],
            "horizon_days": r["horizon_days"],
            "predicted_room_nights": r["predicted_room_nights"],
            "predicted_occupancy_pct": r["predicted_occupancy_pct"],
        }
        for r in rows
    ]


def _assert_no_future_dates(state: DecisionState) -> None:
    """Belt and braces: nothing realised may carry a date after `as_of`.

    The reconstruction is bounded by SQL predicates already. This re-checks the
    assembled object, because the predicates live in six places and a future
    edit to any one of them would otherwise fail silently and look like an
    improvement in accuracy.
    """
    horizon_end = state.as_of + dt.timedelta(days=state.horizon_days)

    for row in state.pace["stay_dates"]:
        stay = dt.date.fromisoformat(row["stay_date"])
        if stay <= state.as_of:
            raise AssertionError(
                f"pace scored {stay}, which is not in the future at {state.as_of}"
            )

    for row in state.forecast:
        stay = dt.date.fromisoformat(row["stay_date"])
        if not (state.as_of < stay <= horizon_end):
            raise AssertionError(f"forecast covers {stay}, outside the replay window")

    for row in state.book["by_stay_date"]:
        stay = dt.date.fromisoformat(row["stay_date"])
        if stay <= state.as_of:
            raise AssertionError(f"book carries {stay}, which is already settled")


def reconstruct(
    as_of: dt.date,
    horizon_days: int = DEFAULT_HORIZON,
    model: str = DEFAULT_MODEL,
) -> DecisionState:
    """Rebuild the decision picture as it stood on `as_of`.

    Every element is bounded by the rule recorded against it in INFORMATION_SET.
    No argument to this function can introduce a future fact, which is the
    property the leakage tests exercise.
    """
    horizon_days = min(horizon_days, rv.MAX_USEFUL_HORIZON)

    # One connection and one transaction for the whole reconstruction, so the
    # dozen queries below cannot see a dozen different states of the database.
    with db.session():
        # Scored once and shared. `opportunity_signals` derives from the same rows
        # rather than re-running the benchmark, which is the expensive part.
        scored = rv.pace(as_of, horizon_days)
        state = DecisionState(
            as_of=as_of,
            horizon_days=horizon_days,
            model=model,
            book=_book(as_of, horizon_days),
            pickup=_pickup(as_of),
            realised=_realised(as_of),
            pace=_pace(as_of, scored),
            calendar=_calendar(as_of, horizon_days),
            holiday_evidence=_holiday_evidence(as_of),
            forecast=_forecast(as_of, horizon_days, model),
            signals=[
                s.as_dict() for s in rv.opportunity_signals(as_of, scored=scored)
            ],
        )
    _assert_no_future_dates(state)
    return state


# ---------------------------------------------------------------------------
# Outcome. Everything below this line is post-T by design.
# ---------------------------------------------------------------------------
def _final_book(first: dt.date, last: dt.date) -> dict[tuple[dt.date, int], int]:
    """(stay_date, property) -> booking-nights still live on the stay date itself.

    THE GRAIN HERE IS DELIBERATE. Pace scores booking-nights from the demand
    grain, so the thing it must be judged against is the same grain at its final
    state: reservations that survived to the arrival date. Comparing against
    `fact_unit_night.is_occupied` would look equivalent and is not -- the two
    grains differ by unallocated nights and hourly stays, which is the whole
    point of the reconciliation identity, and scoring a demand-grain call against
    an inventory-grain outcome would attribute that structural gap to the call.

    No-shows count as on the books: the room was held and the date was sold as
    far as the book was concerned. That is the quantity pace was measuring.
    """
    rows = db.fetch_all(
        """
        SELECT stay_date, property_key, count(*) AS final_nights
        FROM mart.v_booking_night
        WHERE stay_date BETWEEN :a AND :b
          AND entered_on <= stay_date
          AND (left_on IS NULL OR left_on > stay_date)
        GROUP BY 1, 2
        """,
        a=first,
        b=last,
    )
    return {(r["stay_date"], int(r["property_key"])): int(r["final_nights"]) for r in rows}


def _forecast_accuracy(state: DecisionState) -> list[dict[str, Any]]:
    """Predicted against realised occupied room-nights, per horizon.

    Scored on the inventory grain because that is what the forecast targets --
    `daily_actuals` counts occupied unit-nights. It is a different grain from the
    pace scoring below, on purpose, and each is matched to what it predicted.
    """
    rows = db.fetch_all(
        """
        SELECT stay_date, count(*) FILTER (WHERE is_occupied) AS occupied
        FROM mart.fact_unit_night
        WHERE stay_date BETWEEN CAST(:a AS date) + 1 AND CAST(:a AS date) + :h
        GROUP BY 1
        """,
        a=state.as_of,
        h=state.horizon_days,
    )
    actual = {r["stay_date"]: int(r["occupied"]) for r in rows}

    out: list[dict[str, Any]] = []
    for row in state.forecast:
        stay = dt.date.fromisoformat(row["stay_date"])
        if stay not in actual:
            continue
        predicted = float(row["predicted_room_nights"])
        truth = actual[stay]
        out.append({
            "stay_date": row["stay_date"],
            "horizon_days": row["horizon_days"],
            "predicted_room_nights": predicted,
            "actual_room_nights": truth,
            "error_room_nights": round(predicted - truth, 2),
            "abs_error_room_nights": round(abs(predicted - truth), 2),
        })
    return out


def _pace_calls(state: DecisionState) -> dict[str, Any]:
    """Did the dates flagged at T actually land where the flag implied?

    A `behind` call says: this date is carrying fewer nights than comparable dates
    normally carry by now. The checkable consequence is that it finishes below
    what comparable dates finally carry -- `expected_final_nights`, the median
    final book of the same benchmark set, computed at T from dates that had
    already completed by then. Not `expected_nights`: that is the median book at
    the same days-out horizon, and a date beating it proves only that bookings
    kept arriving after the snapshot.

    THE BASE RATE IS REPORTED ALONGSIDE, AND IT IS NOT DECORATION. "Seventy per
    cent of behind calls finished below expectation" means nothing if seventy per
    cent of *every* scored date finishes below expectation. Without the base rate
    a flag that fires on everything scores as a good flag.
    """
    dates = state.pace["stay_dates"]
    if not dates:
        return {"scored": 0, "note": "no stay date had enough support to be scored"}

    stays = [dt.date.fromisoformat(r["stay_date"]) for r in dates]
    final = _final_book(min(stays), max(stays))

    resolved: list[dict[str, Any]] = []
    for row in dates:
        key = (dt.date.fromisoformat(row["stay_date"]), int(row["property_key"]))
        if key not in final or row["expected_final_nights"] is None:
            continue
        realised = final[key]
        expected_final = float(row["expected_final_nights"])
        resolved.append({
            "stay_date": row["stay_date"],
            "property": row["property"],
            "status_at_as_of": row["status"],
            "nights_on_books_at_as_of": row["nights_on_books"],
            "expected_nights_by_now": float(row["expected_nights"]),
            "expected_final_nights": expected_final,
            "final_nights": realised,
            "finished_below_expectation": realised < expected_final,
            "nights_picked_up_after_as_of": realised - row["nights_on_books"],
        })

    def _rate(subset: list[dict[str, Any]], below: bool) -> float | None:
        if not subset:
            return None
        hits = sum(1 for r in subset if r["finished_below_expectation"] is below)
        return round(100.0 * hits / len(subset), 1)

    behind = [r for r in resolved if r["status_at_as_of"] == "behind"]
    ahead = [r for r in resolved if r["status_at_as_of"] == "ahead"]

    return {
        "scored": len(resolved),
        "unresolved": len(dates) - len(resolved),
        "base_rate_finished_below_expectation_pct": _rate(resolved, True),
        "behind": {
            "calls": len(behind),
            "finished_below_expectation_pct": _rate(behind, True),
        },
        "ahead": {
            "calls": len(ahead),
            "finished_above_expectation_pct": _rate(ahead, False),
        },
        "stay_dates": resolved,
    }


def _coverage(state: DecisionState) -> dict[str, Any]:
    """How much of the replay window the dataset can actually resolve.

    An as-of date near the inventory horizon produces a decision whose outcome is
    mostly unobservable. Saying so is more useful than reporting an accuracy
    figure computed on the three days that happened to fit.
    """
    horizon_end = state.as_of + dt.timedelta(days=state.horizon_days)
    last = rv.data_horizon()
    resolvable = max(0, (min(horizon_end, last) - state.as_of).days)
    return {
        "window_days": state.horizon_days,
        "resolvable_days": resolvable,
        "data_horizon": last.isoformat(),
        "fully_resolved": resolvable >= state.horizon_days,
    }


def outcome(state: DecisionState) -> Outcome:
    """What happened after `state.as_of`.

    Takes the decision as input, never the other way round. The direction of that
    dependency is the guarantee.
    """
    with db.session():
        return Outcome(
            as_of=state.as_of,
            forecast_accuracy=_forecast_accuracy(state),
            pace_calls=_pace_calls(state),
            coverage=_coverage(state),
        )


# ---------------------------------------------------------------------------
def replay(
    as_of: dt.date,
    horizon_days: int = DEFAULT_HORIZON,
    model: str = DEFAULT_MODEL,
    with_outcome: bool = True,
) -> dict[str, Any]:
    """Reconstruct the decision at `as_of`, and optionally score it."""
    state = reconstruct(as_of, horizon_days, model)
    payload: dict[str, Any] = {
        "decision": state.as_dict(),
        "fingerprint": state.fingerprint,
        "note": (
            "The decision block is reconstructed from the information set listed "
            "inside it and contains no fact dated after the as-of date. The "
            "public-holiday calendar is the one forward-dated input, because it "
            "is published years in advance; the size of a holiday's effect is "
            "estimated only from holidays that had already occurred."
        ),
    }
    if with_outcome:
        payload["outcome"] = outcome(state).as_dict()
    return payload


def summary(
    as_of: dt.date | None = None,
    horizon_days: int = DEFAULT_HORIZON,
) -> dict[str, Any]:
    """Headline view of one replay, without the per-date detail."""
    as_of = as_of or (rv.data_horizon() - dt.timedelta(days=DEFAULT_HORIZON))
    state = reconstruct(as_of, horizon_days)
    result = outcome(state)

    errors = [r["abs_error_room_nights"] for r in result.forecast_accuracy]
    return {
        "as_of": as_of.isoformat(),
        "fingerprint": state.fingerprint,
        "horizon_days": horizon_days,
        "knew": {
            "nights_on_books": state.book["nights_on_books"],
            "revenue_on_books_inr": state.book["revenue_on_books_inr"],
            "pickup_14d_net": state.pickup["nights_net"],
            "trailing_occupancy_pct": state.realised["occupancy_pct"],
            "holidays_in_window": sum(
                1 for c in state.calendar if c["is_public_holiday"]
            ),
            "holidays_with_prior_measurement": len(state.holiday_evidence),
        },
        "said": {
            "dates_behind_pace": state.pace["behind"],
            "dates_ahead_of_pace": state.pace["ahead"],
            "dates_scored": state.pace["scored"],
            "signals_raised": len(state.signals),
        },
        "happened": {
            "forecast_mae_room_nights": (
                round(sum(errors) / len(errors), 2) if errors else None
            ),
            "forecast_days_scored": len(errors),
            "pace_base_rate_below_expectation_pct":
                result.pace_calls.get("base_rate_finished_below_expectation_pct"),
            "behind_calls": result.pace_calls.get("behind", {}),
            "ahead_calls": result.pace_calls.get("ahead", {}),
            "coverage": result.coverage,
        },
    }
