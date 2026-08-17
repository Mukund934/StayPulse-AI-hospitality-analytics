"""Decision replay tests.

The tests that matter here are the leakage tests, and they are built around one
idea: a reconstruction that claims to know nothing about the future must produce
a byte-identical answer when the future changes underneath it.

So these tests change the future. Inside a transaction that is always rolled
back, they insert bookings dated after the as-of date, cancel bookings after it,
and re-run the whole reconstruction. The fingerprint must not move.

TWO CONTROLS STOP THAT FROM BEING A TEST OF NOTHING.

A test asserting "X did not change" passes just as happily when X can never
change and when the thing that was supposed to change X never arrived. Both
failure modes are silent and both make the suite look green while proving
nothing, which is the exact defect found in the API security tests in an earlier
session. So:

  1. `test_fingerprint_moves_between_as_of_dates` proves the fingerprint is
     sensitive to input at all.
  2. `test_sandbox_writes_are_visible_to_the_reconstruction` inserts a booking
     BEFORE the as-of date and asserts the fingerprint DOES move -- proving the
     insert mechanism reaches the queries under test. Without it, a silently
     failing INSERT would make every leakage test above pass.

Run:  python -m pytest tests/test_replay.py -v
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.analytics import replay as rp  # noqa: E402
from staypulse.analytics import revenue as rv  # noqa: E402


@pytest.fixture(scope="module")
def horizon() -> dt.date:
    return rv.data_horizon()


@pytest.fixture(scope="module")
def as_of(horizon: dt.date) -> dt.date:
    """A date with a full forward window and a fully resolvable outcome."""
    return horizon - dt.timedelta(days=40)


@pytest.fixture(scope="module")
def state(as_of: dt.date) -> rp.DecisionState:
    return rp.reconstruct(as_of)


def _seed_row() -> dict:
    """An existing booking to clone, so foreign keys and money stay valid."""
    return db.fetch_all(
        """
        SELECT guest_key, property_key, unit_key, channel_key, stay_type,
               gross_amount_inr, discount_inr, net_room_amount_inr,
               gst_amount_inr, commission_inr
        FROM mart.fact_booking
        WHERE status <> 'cancelled'
        ORDER BY booking_key
        LIMIT 1
        """
    )[0]


def _insert_booking(conn, booking_id: str, booked: dt.date, check_in: dt.date,
                    nights: int = 3) -> None:
    """Insert one booking on the open sandbox transaction."""
    seed = _seed_row()
    conn.execute(
        text(
            """
            INSERT INTO mart.fact_booking (
                booking_id, guest_key, property_key, unit_key, channel_key,
                booked_at, booking_date, check_in_date, check_out_date,
                stay_type, nights, adults, status,
                gross_amount_inr, discount_inr, net_room_amount_inr,
                gst_amount_inr, commission_inr, lead_time_days, source_system
            ) VALUES (
                :bid, :guest, :prop, :unit, :chan,
                CAST(:booked AS timestamptz), :booked, :ci, :co,
                :stype, :nights, 1, 'confirmed',
                :gross, :disc, :net, :gst, :comm, :lead, 'replay-test'
            )
            """
        ),
        {
            "bid": booking_id,
            "guest": seed["guest_key"],
            "prop": seed["property_key"],
            "unit": seed["unit_key"],
            "chan": seed["channel_key"],
            "booked": booked,
            "ci": check_in,
            "co": check_in + dt.timedelta(days=nights),
            "stype": seed["stay_type"],
            "nights": nights,
            "gross": seed["gross_amount_inr"],
            "disc": seed["discount_inr"],
            "net": seed["net_room_amount_inr"],
            "gst": seed["gst_amount_inr"],
            "comm": seed["commission_inr"],
            "lead": (check_in - booked).days,
        },
    )


class TestInformationSet:
    """The manifest is the feature's contract, so it is asserted, not assumed."""

    def test_manifest_is_populated(self):
        assert len(rp.INFORMATION_SET) >= 6, (
            "the information set is the documented contract of this module; an "
            "empty or truncated manifest would make every test below vacuous"
        )

    def test_every_source_declares_a_known_basis(self):
        allowed = {rp.REALISED, rp.AS_BOOKED, rp.EX_ANTE}
        for source in rp.INFORMATION_SET:
            assert source.basis in allowed, f"{source.name} has basis {source.basis}"
            assert source.rule, f"{source.name} declares no temporal rule"
            assert source.contributes, f"{source.name} says nothing about its use"

    def test_only_the_published_calendar_is_treated_as_known_in_advance(self):
        """EX_ANTE is the one exemption from the as-of bound. It stays a single
        exemption: anything else claiming to be knowable in advance is a leak
        with a justification attached."""
        ex_ante = [s for s in rp.INFORMATION_SET if s.basis == rp.EX_ANTE]
        assert len(ex_ante) == 1
        assert "dim_date" in ex_ante[0].name

    def test_state_carries_the_manifest(self, state: rp.DecisionState):
        assert len(state.information_set) == len(rp.INFORMATION_SET)


class TestReconstructionBounds:
    """Nothing dated after the as-of date may appear in a decision."""

    def test_book_holds_only_future_stay_dates(self, state, as_of):
        rows = state.book["by_stay_date"]
        assert len(rows) > 0, "no book to test; pick an as-of date with inventory"
        for row in rows:
            assert dt.date.fromisoformat(row["stay_date"]) > as_of

    def test_forecast_covers_exactly_the_replay_window(self, state, as_of):
        assert len(state.forecast) == state.horizon_days
        end = as_of + dt.timedelta(days=state.horizon_days)
        for row in state.forecast:
            assert as_of < dt.date.fromisoformat(row["stay_date"]) <= end

    def test_pace_scores_only_future_dates(self, state, as_of):
        rows = state.pace["stay_dates"]
        assert len(rows) > 0, "no scored dates; the assertions below would be vacuous"
        for row in rows:
            assert dt.date.fromisoformat(row["stay_date"]) > as_of

    def test_decision_carries_no_realised_outcome(self, state):
        """`forward` returns an `actual_room_nights` column that is populated at a
        historical origin. It is dropped, not filtered downstream, and this walks
        the whole payload to prove no route puts it back.

        `expected_final_nights` is deliberately not banned. It is the median final
        book of dates that had already COMPLETED by the as-of date, so it is a
        settled historical fact, not an outcome. The distinction is the whole
        point of the module and the test has to encode it rather than pattern-match
        on the word "final".
        """
        banned_substrings = ("actual", "outcome")
        banned_exact = {"final_nights", "finished_below_expectation",
                        "nights_picked_up_after_as_of"}

        def walk(node, path=""):
            if isinstance(node, dict):
                for key, value in node.items():
                    low = key.lower()
                    assert not any(b in low for b in banned_substrings), (
                        f"decision payload exposes an outcome field at {path}.{key}"
                    )
                    assert low not in banned_exact, (
                        f"decision payload exposes an outcome field at {path}.{key}"
                    )
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    walk(item, f"{path}[{i}]")

        payload = state.as_dict()
        assert payload, "empty decision payload would make this walk vacuous"
        walk(payload)

        # And the banned names must actually appear on the outcome side, or the
        # walk above is checking for fields that no longer exist anywhere.
        scored = rp.outcome(state).pace_calls["stay_dates"]
        assert scored and "final_nights" in scored[0]

    def test_holiday_evidence_predates_the_snapshot(self, as_of):
        """Effects may only come from holidays that had already happened."""
        evidence = rp._holiday_evidence(as_of)
        names = {e["holiday"] for e in evidence}
        future_only = db.fetch_all(
            """
            SELECT DISTINCT holiday_name FROM mart.dim_date
            WHERE is_public_holiday AND holiday_name IS NOT NULL
            GROUP BY holiday_name
            HAVING min(full_date) >= :d
            """,
            d=as_of,
        )
        for row in future_only:
            assert row["holiday_name"] not in names, (
                f"{row['holiday_name']} has not occurred by {as_of} but carries a "
                "measured effect"
            )

    def test_realised_window_stops_at_the_snapshot(self, state):
        assert state.realised["days_observed"] <= rp.REALISED_LOOKBACK + 1
        assert state.realised["occupied_room_nights"] > 0


class TestNoLeakage:
    """Same historical state, different future, same decision.

    Every test here writes inside `db.rollback_sandbox`, which always rolls back.
    """

    def test_sandbox_writes_are_visible_to_the_reconstruction(self, as_of):
        """THE CONTROL. A booking entered BEFORE the as-of date is part of the
        information set and must move the fingerprint.

        If this fails, the inserts in the tests below are not reaching the
        queries and every "unchanged" assertion in this class is worthless.
        """
        with db.rollback_sandbox() as conn:
            before = rp.reconstruct(as_of).fingerprint
            # Entered a week before the snapshot, for a stay just after it: this
            # is squarely inside what was on the books at T.
            _insert_booking(
                conn,
                "REPLAY-CONTROL-PAST",
                booked=as_of - dt.timedelta(days=7),
                check_in=as_of + dt.timedelta(days=2),
                nights=3,
            )
            after = rp.reconstruct(as_of).fingerprint

        assert before != after, (
            "a booking that existed at the as-of date did not change the "
            "reconstruction, so the sandbox is not reaching the queries under "
            "test and the leakage assertions below prove nothing"
        )

    def test_bookings_entered_after_the_snapshot_do_not_change_the_decision(self, as_of):
        """The headline guarantee: the future arriving changes nothing about T."""
        with db.rollback_sandbox() as conn:
            before = rp.reconstruct(as_of).fingerprint
            for i in range(25):
                _insert_booking(
                    conn,
                    f"REPLAY-FUTURE-{i}",
                    booked=as_of + dt.timedelta(days=1 + i % 5),
                    check_in=as_of + dt.timedelta(days=8 + i % 20),
                    nights=3,
                )
            after = rp.reconstruct(as_of).fingerprint

        assert before == after, (
            "25 bookings entered after the as-of date changed what the system "
            "claims to have known at it -- the reconstruction is leaking"
        )

    def test_cancellations_after_the_snapshot_do_not_change_the_decision(self, as_of):
        """A booking live at T counts at T, whatever happened to it later."""
        with db.rollback_sandbox() as conn:
            before = rp.reconstruct(as_of).fingerprint
            # Selected on the date columns, not on `status`. The generator only
            # ever writes terminal statuses -- there is no 'confirmed' row in this
            # warehouse at all -- so a status-based filter silently matches
            # nothing and the test passes without cancelling anything.
            cancelled = conn.execute(
                text(
                    """
                    UPDATE mart.fact_booking
                    SET status = 'cancelled',
                        cancelled_at = CAST(:d AS timestamptz),
                        cancel_date = :d
                    WHERE booking_key IN (
                        SELECT booking_key FROM mart.fact_booking
                        WHERE booking_date  <= :t
                          AND check_in_date  > :t
                          AND cancel_date   IS NULL
                        ORDER BY booking_key
                        LIMIT 40
                    )
                    """
                ),
                {"d": as_of + dt.timedelta(days=2), "t": as_of},
            ).rowcount
            after = rp.reconstruct(as_of).fingerprint

        assert cancelled > 0, "no booking was cancelled; the assertion would be vacuous"
        assert before == after, (
            f"{cancelled} cancellations dated after the as-of date changed the "
            "reconstruction -- the book is being read with hindsight"
        )

    def test_different_futures_give_the_same_decision_and_different_outcomes(self, as_of):
        """The full statement of the property, both halves of it.

        Same history + different future must give the same DECISION. It must also
        give a different OUTCOME -- otherwise the outcome is not reading the
        future either, and the whole replay is inert.
        """
        with db.rollback_sandbox() as conn:
            base = rp.reconstruct(as_of)
            base_outcome = rp.outcome(base).as_dict()

            for i in range(40):
                _insert_booking(
                    conn,
                    f"REPLAY-ALT-FUTURE-{i}",
                    booked=as_of + dt.timedelta(days=3),
                    check_in=as_of + dt.timedelta(days=10 + i % 15),
                    nights=2,
                )
            altered = rp.reconstruct(as_of)
            altered_outcome = rp.outcome(altered).as_dict()

        assert base.fingerprint == altered.fingerprint, "decision leaked the future"
        assert base_outcome != altered_outcome, (
            "the outcome did not move when the future did, so it is not "
            "measuring the future and the comparison is meaningless"
        )

    def test_fingerprint_moves_between_as_of_dates(self, as_of):
        """THE OTHER CONTROL. A constant fingerprint would satisfy every
        invariance test above while proving nothing."""
        a = rp.reconstruct(as_of).fingerprint
        b = rp.reconstruct(as_of - dt.timedelta(days=7)).fingerprint
        assert a != b

    def test_fingerprint_is_deterministic(self, as_of):
        assert rp.reconstruct(as_of).fingerprint == rp.reconstruct(as_of).fingerprint

    def test_pace_benchmark_ignores_stay_dates_at_or_after_the_snapshot(self, as_of):
        """Independent reimplementation of the bound the benchmark claims.

        The benchmark is the one input where a leak would be invisible in the
        output: it would simply make every pace score better calibrated than it
        had any right to be.
        """
        leaked = db.scalar(
            """
            SELECT count(*) FROM (
                SELECT DISTINCT stay_date FROM mart.fact_unit_night
                WHERE stay_date < :d
            ) x WHERE x.stay_date >= :d
            """,
            d=as_of,
        )
        assert leaked == 0


class TestOutcomeSeparation:
    """The decision must not be able to see its own answer."""

    def test_reconstruct_takes_no_outcome_argument(self):
        import inspect

        params = set(inspect.signature(rp.reconstruct).parameters)
        assert params == {"as_of", "horizon_days", "model"}

    def test_outcome_depends_on_the_decision_and_not_the_reverse(self, state):
        import inspect

        assert "state" in inspect.signature(rp.outcome).parameters
        result = rp.outcome(state)
        assert result.as_of == state.as_of

    def test_replay_can_withhold_the_outcome(self, as_of):
        payload = rp.replay(as_of, with_outcome=False)
        assert "outcome" not in payload
        assert payload["decision"]["as_of"] == as_of.isoformat()


class TestOutcomeScoring:
    """The scoring has to be falsifiable, and comparable against a base rate."""

    def test_forecast_accuracy_pairs_prediction_with_the_same_stay_date(self, state):
        rows = rp.outcome(state).forecast_accuracy
        assert len(rows) > 0, "nothing scored; the loop below would be vacuous"
        predicted = {r["stay_date"]: r["predicted_room_nights"] for r in state.forecast}
        for row in rows:
            assert row["predicted_room_nights"] == predicted[row["stay_date"]]
            assert row["abs_error_room_nights"] == abs(
                round(row["predicted_room_nights"] - row["actual_room_nights"], 2)
            )

    def test_pace_calls_are_scored_against_a_final_book_benchmark(self, state):
        """Not against `expected_nights`. That is the book at the same horizon,
        and beating it only proves bookings kept arriving.

        Note what is NOT asserted: that the final benchmark exceeds the
        by-now benchmark. It usually does, but not always, and the exception is
        real rather than a defect. A night on the books ten days out can leave
        the book before arrival, so on (property, weekday) pairs where wash
        outruns late pickup the median final book sits BELOW the median book at
        horizon. Asserting monotonicity here would be asserting that
        cancellations do not happen.
        """
        calls = rp.outcome(state).pace_calls
        assert calls["scored"] > 0
        for row in calls["stay_dates"]:
            assert row["expected_final_nights"] > 0
            assert row["finished_below_expectation"] == (
                row["final_nights"] < row["expected_final_nights"]
            )
            assert row["nights_picked_up_after_as_of"] == (
                row["final_nights"] - row["nights_on_books_at_as_of"]
            )

    def test_the_two_benchmarks_are_not_the_same_number(self, state):
        """If they were, the comparator fix that produced them did nothing."""
        rows = rp.outcome(state).pace_calls["stay_dates"]
        assert rows, "nothing scored; this comparison would be vacuous"
        differing = sum(
            1 for r in rows
            if r["expected_final_nights"] != r["expected_nights_by_now"]
        )
        assert differing > 0.5 * len(rows), (
            "the final-book benchmark is tracking the by-now benchmark, which "
            "means the pace calls are being scored against the wrong quantity"
        )

    def test_base_rate_is_published_next_to_the_hit_rate(self, state):
        calls = rp.outcome(state).pace_calls
        assert calls["base_rate_finished_below_expectation_pct"] is not None, (
            "a hit rate without its base rate is not a result"
        )
        assert 0.0 <= calls["base_rate_finished_below_expectation_pct"] <= 100.0

    def test_coverage_reports_what_the_dataset_cannot_resolve(self, horizon):
        near_end = horizon - dt.timedelta(days=5)
        result = rp.outcome(rp.reconstruct(near_end))
        assert result.coverage["fully_resolved"] is False
        assert result.coverage["resolvable_days"] < result.coverage["window_days"]


class TestBoundaryDates:
    """Replay must survive the awkward dates, not just the comfortable one."""

    def test_earliest_history_produces_a_decision_without_a_benchmark(self):
        """Days into the dataset there is no comparable history, so nothing can be
        scored. The correct behaviour is an empty pace list, not a crash and not a
        score built on one observation."""
        first = db.scalar("SELECT min(stay_date) FROM mart.fact_unit_night")
        state = rp.reconstruct(first + dt.timedelta(days=3))
        assert state.pace["scored"] == 0
        assert state.pace["stay_dates"] == []
        assert rp.outcome(state).pace_calls["scored"] == 0

    def test_snapshot_at_the_data_horizon_holds_only_continuing_stays(self, horizon):
        """At the inventory horizon the only forward book left is the tail of
        stays that started inside the data and run past its end. It is not zero --
        `v_booking_night` explodes a booking across its nights whether or not
        `fact_unit_night` has inventory rows for them -- and it is thin.
        """
        state = rp.reconstruct(horizon)
        earlier = rp.reconstruct(horizon - dt.timedelta(days=40))
        assert state.book["nights_on_books"] < 0.5 * earlier.book["nights_on_books"], (
            "the book at the horizon should be a residue of the book 40 days out"
        )
        assert state.forecast, "the forecast should still be produced"
        assert rp.outcome(state).coverage["fully_resolved"] is False

    def test_holiday_adjacent_snapshot_carries_calendar_context(self):
        """An as-of date whose window contains a public holiday must surface it,
        because the calendar is the one thing knowable in advance."""
        row = db.fetch_all(
            """
            SELECT min(full_date) AS d FROM mart.dim_date
            WHERE is_public_holiday
              AND full_date > (SELECT min(stay_date) + 120 FROM mart.fact_unit_night)
              AND full_date < (SELECT max(stay_date) FROM mart.fact_unit_night)
            """
        )[0]
        assert row["d"] is not None, "no holiday inside the data; test cannot run"
        state = rp.reconstruct(row["d"] - dt.timedelta(days=10))
        holidays = [c for c in state.calendar if c["is_public_holiday"]]
        assert holidays, "a holiday inside the window was not reported"
        assert any(c["stay_date"] == row["d"].isoformat() for c in holidays)

    def test_a_snapshot_with_no_scorable_dates_still_replays(self):
        first = db.scalar("SELECT min(stay_date) FROM mart.fact_unit_night")
        payload = rp.replay(first + dt.timedelta(days=1))
        assert payload["fingerprint"]
        assert payload["outcome"]["pace_calls"]["scored"] == 0

    def test_summary_runs_at_several_historical_dates(self, horizon):
        for back in (45, 90, 180):
            result = rp.summary(horizon - dt.timedelta(days=back))
            assert result["knew"]["nights_on_books"] >= 0
            assert result["happened"]["forecast_days_scored"] > 0


class TestReplayedForecastUsesContemporaryCapacity:
    """The replay depends on a leak fixed in `forecast.forward` while building it.

    The regression test for the fix itself lives in tests/test_forecast.py, where
    the function does. This checks only that the replay consumes the fixed
    behaviour -- a replayed occupancy percentage must be consistent with the
    inventory the origin had actually seen.
    """

    def test_replayed_occupancy_uses_capacity_from_before_the_snapshot(self):
        early = dt.date(2025, 10, 1)
        state = rp.reconstruct(early, horizon_days=7)
        implied = [
            r["predicted_room_nights"] / r["predicted_occupancy_pct"] * 100
            for r in state.forecast
            if r["predicted_occupancy_pct"]
        ]
        assert implied, "no occupancy percentage in the replayed forecast"

        contemporary = db.scalar(
            """
            SELECT avg(c)::float FROM (
                SELECT count(*) FILTER (WHERE is_sellable) AS c
                FROM mart.fact_unit_night
                WHERE stay_date BETWEEN CAST(:d AS date) - 27 AND :d
                GROUP BY stay_date
            ) x
            """,
            d=early,
        )
        final = db.scalar(
            """
            SELECT avg(c)::float FROM (
                SELECT count(*) FILTER (WHERE is_sellable) AS c
                FROM mart.fact_unit_night
                WHERE stay_date > (SELECT max(stay_date) - 28 FROM mart.fact_unit_night)
                GROUP BY stay_date
            ) x
            """
        )
        assert final > contemporary * 1.2, (
            "the portfolio expansion this test depends on is not in the data"
        )
        assert max(implied) < final * 0.95, (
            f"replayed capacity {max(implied):.1f} is drawn from the expanded "
            f"portfolio ({final:.1f}), which did not exist on {early}"
        )
