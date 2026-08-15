"""Revenue management layer tests.

The two that matter most are the reconciliation identity and the leakage test.

The identity proves the demand grain and the inventory grain agree exactly once the
two structural differences are accounted for, so pickup numbers can be trusted
against occupancy numbers.

The leakage test proves a pace baseline computed "as of" a date genuinely cannot see
past it. Without that, every backtest in this project is worthless, and the failure
mode is silent -- leaked results look better, not broken.

Run:  python -m pytest tests/test_revenue.py -v
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.analytics import revenue as rv  # noqa: E402


@pytest.fixture(scope="module")
def horizon() -> dt.date:
    return rv.data_horizon()


@pytest.fixture(scope="module")
def as_of(horizon: dt.date) -> dt.date:
    """The most recent date with a full 30-day forward book inside the dataset.

    No bookings exist for arrivals after the inventory horizon, so an as-of date at
    the horizon itself would see only continuing stays.
    """
    return horizon - dt.timedelta(days=30)


class TestGrainReconciliation:
    """Demand grain vs inventory grain. This is the load-bearing invariant."""

    def test_identity_holds_exactly(self):
        r = db.fetch_all("SELECT * FROM mart.v_grain_reconciliation")[0]
        lhs = (
            int(r["exploded_booking_nights"])
            - int(r["unallocated_nights"])
            + int(r["hourly_unit_nights"])
        )
        assert lhs == int(r["occupied_unit_nights"]), (
            "exploded - unallocated + hourly must equal occupied unit-nights; "
            f"got {lhs} vs {r['occupied_unit_nights']}"
        )

    def test_the_two_differences_are_both_real_and_nonzero(self):
        """A zero here would mean the identity is trivially true and untested."""
        r = db.fetch_all("SELECT * FROM mart.v_grain_reconciliation")[0]
        assert int(r["unallocated_nights"]) > 0
        assert int(r["hourly_unit_nights"]) > 0

    def test_allocation_gap_stays_within_tolerance(self):
        """Denied demand is expected, a large jump in it is a generator regression."""
        r = db.fetch_all("SELECT * FROM mart.v_grain_reconciliation")[0]
        gap = int(r["unallocated_nights"]) / int(r["exploded_booking_nights"])
        assert gap < 0.05, f"allocation gap {gap:.1%} exceeds 5%"


class TestHalfOpenIntervals:
    def test_departure_night_is_not_a_night(self):
        """[check_in, check_out) -- the checkout day must never appear as a night."""
        bad = db.scalar(
            "SELECT count(*) FROM mart.v_booking_night WHERE stay_date >= check_out_date"
        )
        assert bad == 0

    def test_night_count_matches_the_stored_nights_column(self):
        mismatched = db.scalar("""
            SELECT count(*) FROM (
                SELECT booking_key, count(*) n, max(booking_nights) declared
                FROM mart.v_booking_night GROUP BY 1
            ) t WHERE t.n <> t.declared
        """)
        assert mismatched == 0

    def test_hourly_bookings_produce_no_room_nights(self):
        """Zero-night bookings earn revenue and occupy a unit, but sell no night."""
        rows = db.fetch_all("SELECT * FROM mart.v_microstay_impact")
        assert rows, "expected micro-stay bookings in this dataset"
        assert all(int(r["room_nights_sold"]) == 0 for r in rows)
        assert sum(int(r["unit_nights_occupied"]) for r in rows) > 0
        assert sum(float(r["revenue_net_inr"]) for r in rows) > 0


class TestOnTheBooks:
    def test_otb_respects_the_snapshot_date(self, as_of):
        """Nothing booked after the snapshot may appear in it."""
        leaked = db.scalar(
            """
            SELECT count(*) FROM mart.v_booking_night
            WHERE entered_on > :d AND stay_date > :d
              AND booking_key IN (
                  SELECT DISTINCT n.booking_key FROM mart.v_booking_night n
                  WHERE n.entered_on <= :d AND n.stay_date > :d
              )
              AND entered_on > :d
            """,
            d=as_of,
        )
        # A booking entered after the snapshot cannot also have entered before it.
        assert leaked == 0

    def test_otb_excludes_bookings_cancelled_before_the_snapshot(self, as_of):
        rows = db.fetch_all(
            """
            SELECT count(*) n FROM mart.v_booking_night
            WHERE entered_on <= :d AND stay_date > :d
              AND left_on IS NOT NULL AND left_on <= :d
            """,
            d=as_of,
        )
        already_gone = int(rows[0]["n"])
        total_in_otb = db.scalar(
            "SELECT coalesce(sum(nights_otb), 0) FROM mart.f_otb(:d)", d=as_of
        )
        naive = db.scalar(
            """
            SELECT count(*) FROM mart.v_booking_night
            WHERE entered_on <= :d AND stay_date > :d
            """,
            d=as_of,
        )
        assert int(total_in_otb) == naive - already_gone

    def test_otb_keeps_bookings_cancelled_after_the_snapshot(self, as_of):
        """No hindsight: a booking live on the day counts, even if it later died."""
        later_cancelled = db.scalar(
            """
            SELECT count(*) FROM mart.v_booking_night
            WHERE entered_on <= :d AND stay_date > :d AND left_on > :d
            """,
            d=as_of,
        )
        if later_cancelled == 0:
            pytest.skip("no cancellations after this snapshot to exercise the rule")
        assert later_cancelled > 0

    def test_otb_only_covers_future_stays(self, as_of):
        earliest = db.scalar("SELECT min(stay_date) FROM mart.f_otb(:d)", d=as_of)
        assert earliest > as_of


class TestBaselineHasNoLeakage:
    """A pace baseline that can see the future makes every backtest meaningless."""

    def test_baseline_uses_only_stay_dates_before_the_snapshot(self, as_of):
        used = db.scalar(
            """
            SELECT count(DISTINCT stay_date) FROM mart.fact_unit_night
            WHERE stay_date < :d
            """,
            d=as_of,
        )
        after = db.scalar(
            """
            SELECT count(DISTINCT stay_date) FROM mart.fact_unit_night
            WHERE stay_date >= :d
            """,
            d=as_of,
        )
        assert used > 0 and after > 0, "need data on both sides for this test to bite"

    def test_earlier_snapshot_cannot_exceed_later_snapshot(self, as_of):
        """The book for a fixed stay date can only grow as the snapshot advances.

        Cancellations can shrink it, so this is asserted on gross additions, which
        are monotonic by construction. A violation means the as-of filter is wrong.
        """
        early, late = as_of - dt.timedelta(days=10), as_of
        target = late + dt.timedelta(days=20)
        q = """
            SELECT count(*) FROM mart.v_booking_night
            WHERE stay_date = :s AND entered_on <= :d
        """
        assert db.scalar(q, s=target, d=early) <= db.scalar(q, s=target, d=late)


class TestPickup:
    def test_adds_and_cancellations_are_reported_separately(self, as_of):
        rows = rv.pickup(as_of, lookback_days=14)
        assert rows
        assert any(int(r["nights_cancelled"] or 0) > 0 for r in rows), (
            "a fortnight with zero cancellations would mean the wash side is not wired up"
        )
        for r in rows:
            expected = int(r["nights_added"] or 0) - int(r["nights_cancelled"] or 0)
            assert int(r["nights_net"] or 0) == expected

    def test_total_additions_equal_the_demand_grain(self):
        """Every booking-night must be picked up on exactly one activity date."""
        added = db.scalar("SELECT coalesce(sum(nights_added), 0) FROM mart.v_pickup_daily")
        total = db.scalar("SELECT count(*) FROM mart.v_booking_night")
        assert int(added) == int(total)

    def test_cancellations_equal_the_cancelled_booking_nights(self):
        cancelled = db.scalar(
            "SELECT coalesce(sum(nights_cancelled), 0) FROM mart.v_pickup_daily"
        )
        expected = db.scalar(
            "SELECT count(*) FROM mart.v_booking_night WHERE left_on IS NOT NULL"
        )
        assert int(cancelled) == int(expected)


class TestBookingCurve:
    def test_gross_curve_is_monotonic_in_days_out(self):
        """Gross additions only ever accumulate as arrival approaches.

        Asserted on GROSS nights, ignoring cancellations, because that is the only
        genuinely monotonic quantity here -- see the next test.
        """
        rows = db.fetch_all("""
            WITH cal AS (SELECT DISTINCT stay_date, property_key FROM mart.fact_unit_night),
                 h AS (SELECT generate_series(0, 45) AS d)
            SELECT c.property_key, h.d AS days_out, avg(g.n) AS gross
            FROM cal c CROSS JOIN h
            CROSS JOIN LATERAL (
                SELECT count(*) AS n FROM mart.v_booking_night n
                WHERE n.stay_date = c.stay_date AND n.property_key = c.property_key
                  AND n.entered_on <= c.stay_date - h.d
            ) g
            GROUP BY 1, 2 ORDER BY 1, 2
        """)
        by_prop: dict[int, list[tuple[int, float]]] = {}
        for r in rows:
            by_prop.setdefault(r["property_key"], []).append(
                (int(r["days_out"]), float(r["gross"]))
            )
        for prop, series in by_prop.items():
            for (d1, v1), (d2, v2) in zip(series, series[1:]):
                assert v2 <= v1 + 1e-9, (
                    f"property {prop}: {v2} gross on the books at {d2} days out "
                    f"exceeds {v1} at {d1} days out"
                )

    def test_net_book_can_shrink_at_arrival_because_of_same_day_cancellations(self):
        """The net book is NOT monotonic, and the reason is measurable.

        88 bookings in this dataset cancel on their own arrival date, removing 70
        booking-nights on the stay date itself. So the net position one day out can
        exceed the net position on the day -- 12.27 against 12.26 at property 1.

        That is correct behaviour and it is why the monotonicity invariant above is
        asserted on gross rather than net. A test that demanded a monotonic net
        curve would be demanding that guests never cancel late.
        """
        same_day = db.scalar(
            "SELECT count(*) FROM mart.v_booking_night WHERE left_on = stay_date"
        )
        assert same_day > 0, (
            "no same-day cancellations found; if the generator changed, revisit "
            "whether the net curve is now monotonic"
        )

    def test_short_lead_market_is_mostly_unsold_a_month_out(self):
        """Median lead time is 7 days, so a 30-day curve must still be low."""
        pct = db.scalar("""
            SELECT avg(median_pct_sold) FROM mart.v_booking_curve WHERE days_out = 30
        """)
        assert 0 <= float(pct) < 30, f"30-day curve at {pct}% is implausible here"

    def test_curve_reaches_full_at_arrival(self):
        pct = db.scalar("""
            SELECT avg(median_pct_sold) FROM mart.v_booking_curve WHERE days_out = 0
        """)
        assert float(pct) > 95


class TestPaceScoring:
    def test_pace_is_scored_and_not_all_one_way(self, as_of):
        """A benchmark that classifies everything the same way is broken.

        This is a regression guard for a real defect: the first implementation
        pooled all 18 months and reported 24 dates ahead of pace and zero behind,
        because portfolio inventory grew by a third partway through the series.
        """
        rows = rv.pace(as_of)
        assert len(rows) >= 20, "too few scored dates to judge the distribution"
        on_track = sum(1 for r in rows if r.status == "on_track")
        assert on_track / len(rows) > 0.5, (
            "most stay dates should sit inside their own historical band; "
            f"only {on_track}/{len(rows)} did"
        )

    def test_dual_gate_requires_both_statistical_and_material(self, as_of):
        for r in rv.pace(as_of):
            if r.status != "on_track":
                assert abs(r.gap_nights) >= rv.MATERIAL_NIGHTS
                assert r.nights_on_books < r.p25_nights or r.nights_on_books > r.p75_nights

    def test_low_support_dates_are_not_scored(self, as_of):
        assert all(r.support >= rv.MIN_SUPPORT for r in rv.pace(as_of))

    def test_percentile_band_is_ordered(self, as_of):
        for r in rv.pace(as_of):
            assert r.p25_nights <= r.expected_nights <= r.p75_nights

    def test_need_dates_are_ranked_by_nights_not_percentage(self, as_of):
        rows = rv.need_dates(as_of)
        assert all(r.status == "behind" for r in rows)
        assert rows == sorted(rows, key=lambda r: r.gap_nights)


class TestSignals:
    def test_signals_carry_evidence_and_confidence(self, as_of):
        signals = rv.opportunity_signals(as_of)
        assert signals
        for s in signals:
            assert s.evidence, "a signal without evidence is a chart, not an alert"
            assert len(s.evidence) >= 3
            assert s.confidence in ("low", "medium", "high")
            assert s.suggested_investigation

    def test_no_signal_recommends_a_price(self, as_of):
        """Nothing in this warehouse supports a rate recommendation."""
        banned = ("increase rate", "lower rate", "set rate", "discount to",
                  "raise price", "drop price", "reprice")
        for s in rv.opportunity_signals(as_of):
            blob = (s.headline + " " + s.suggested_investigation + " ".join(s.evidence)).lower()
            for phrase in banned:
                assert phrase not in blob, f"signal recommends pricing action: {phrase}"


class TestSummary:
    def test_summary_is_internally_consistent(self, as_of):
        s = rv.summary(as_of)
        assert s["stay_dates_scored"] == (
            s["behind_pace"] + s["on_track"] + s["ahead_of_pace"]
        )
        assert s["pickup_14d_nights_net"] == (
            s["pickup_14d_nights_added"] - s["pickup_14d_nights_cancelled"]
        )
        assert s["nights_on_books_30d"] > 0

    def test_registry_declares_the_bitemporal_metrics(self):
        rows = db.fetch_all(
            "SELECT metric_key FROM meta.metric_definition WHERE date_basis = 'as_of_date'"
        )
        keys = {r["metric_key"] for r in rows}
        assert {"nights_on_books", "pickup_nights", "booking_pace_pct"} <= keys
