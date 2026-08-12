"""StayPulse test suite.

Grouped by what each class actually protects:

  TestMetricInvariants     algebraic identities that must hold or the numbers are wrong
  TestTimezone             UTC/IST correctness, the defect most likely to ship silently
  TestGrain                the half-open interval and the unit-night grain
  TestReferentialIntegrity relationships the mart depends on
  TestReconciliation       source counts vs analytical counts
  TestDataQuality          the quality framework catches what is planted
  TestAnomalyDetection     the detector discriminates rather than twitching
  TestAIValidation         the evidence gate BLOCKS a fabricated quote
  TestReproducibility      the same seed produces the same dataset
  TestSemanticLayer        the registry cannot publish an undefined metric

Run:  python -m pytest tests/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.ai import baseline  # noqa: E402
from staypulse.ai.client import _norm  # noqa: E402
from staypulse.analytics import anomaly as an  # noqa: E402
from staypulse.generate import spec  # noqa: E402
from staypulse.generate.builder import Generator, dataset_fingerprint  # noqa: E402
from staypulse.quality import rules as dq_rules  # noqa: E402
from staypulse.quality import runner as dq_runner  # noqa: E402


# ===========================================================================
class TestMetricInvariants:
    def test_revpar_equals_adr_times_occupancy(self):
        """The identity only holds if all three share one table and denominator.

        This is the single most valuable assertion in the project: it fails the
        moment someone computes occupancy from bookings and ADR from unit-nights.
        """
        r = db.fetch_all("""
            SELECT count(*) FILTER (WHERE is_sellable) av,
                   count(*) FILTER (WHERE is_occupied) sold,
                   sum(room_revenue_net_inr) rev
            FROM mart.fact_unit_night
        """)[0]
        av, sold, rev = int(r["av"]), int(r["sold"]), float(r["rev"])
        occ, adr, revpar = sold / av, rev / sold, rev / av
        assert abs(revpar - adr * occ) < 1e-6, (
            f"RevPAR {revpar} != ADR {adr} x Occ {occ}")

    def test_operational_occupancy_exceeds_benchmark(self):
        """Removing out-of-order units from availability must raise occupancy."""
        r = db.fetch_all("""
            SELECT sum(rooms_sold) sold, sum(rooms_available) av,
                   sum(unit_nights_physical) phys
            FROM mart.v_daily_kpi
        """)[0]
        op = int(r["sold"]) / int(r["av"])
        bm = int(r["sold"]) / int(r["phys"])
        assert op > bm

    def test_gross_equals_net_plus_gst(self):
        r = db.fetch_all("""
            SELECT sum(room_revenue_net_inr) net, sum(gst_inr) gst,
                   sum(gross_incl_gst_inr) gross
            FROM mart.v_unit_night_enriched WHERE is_occupied
        """)[0]
        assert abs(float(r["gross"]) - (float(r["net"]) + float(r["gst"]))) < 1.0

    def test_view_agrees_with_base_table(self):
        """The semantic layer must not drift from the facts it reads."""
        v = db.fetch_all("SELECT sum(rooms_sold) s, sum(room_revenue_net_inr) r "
                         "FROM mart.v_daily_kpi")[0]
        b = db.fetch_all("SELECT count(*) FILTER (WHERE is_occupied) s, "
                         "sum(room_revenue_net_inr) r FROM mart.fact_unit_night")[0]
        assert int(v["s"]) == int(b["s"])
        assert abs(float(v["r"]) - float(b["r"])) < 1.0

    def test_occupancy_never_exceeds_capacity(self):
        bad = db.scalar("""
            SELECT count(*) FROM (
                SELECT property_key, stay_date,
                       count(*) FILTER (WHERE is_sellable) av,
                       count(*) FILTER (WHERE is_occupied) sold
                FROM mart.fact_unit_night GROUP BY 1,2) t
            WHERE sold > av
        """)
        assert bad == 0

    @pytest.mark.parametrize("metric", [
        "occupancy_pct", "adr_inr", "revpar_inr", "cancellation_rate_pct",
        "sla_breach_rate_pct", "repeat_guest_rate_pct", "alos_nights",
    ])
    def test_metric_is_registered(self, metric):
        assert db.scalar(
            "SELECT count(*) FROM meta.metric_definition WHERE metric_key = :m",
            m=metric) == 1


# ===========================================================================
class TestTimezone:
    def test_business_date_shifts_at_1830_utc(self):
        """18:30 UTC is midnight IST. That boundary is the whole defect class."""
        before = db.scalar(
            "SELECT meta.business_date(timestamptz '2026-03-10 18:29:00+00')")
        after = db.scalar(
            "SELECT meta.business_date(timestamptz '2026-03-10 18:31:00+00')")
        assert before.isoformat() == "2026-03-10"
        assert after.isoformat() == "2026-03-11"

    def test_business_date_is_immutable(self):
        """IMMUTABLE, so it can be indexed. STABLE would silently forbid that."""
        vol = db.scalar("""
            SELECT p.provolatile FROM pg_proc p
            JOIN pg_namespace n ON n.oid = p.pronamespace
            WHERE n.nspname='meta' AND p.proname='business_date'
        """)
        assert vol == "i", f"expected IMMUTABLE, got provolatile={vol!r}"

    def test_service_request_dates_use_ist(self):
        bad = db.scalar("""
            SELECT count(*) FROM mart.fact_service_request
            WHERE request_date <> meta.business_date(created_at)
        """)
        assert bad == 0

    def test_planted_business_date_drift_is_present_and_bounded(self):
        f2 = next(f for f in spec.PLANTED_FINDINGS
                  if f.key == "F2_NIGHT_AUDIT_CUTOFF")
        r = db.fetch_all("""
            SELECT count(*) FILTER (
                       WHERE meta.business_date(booked_at) BETWEEN :s AND :e) inside,
                   count(*) FILTER (
                       WHERE meta.business_date(booked_at) NOT BETWEEN :s AND :e) outside
            FROM mart.fact_booking
            WHERE booking_date <> meta.business_date(booked_at)
        """, s=f2.window[0], e=f2.window[1])[0]
        assert int(r["inside"]) > 0, "planted drift did not survive generation"
        assert int(r["outside"]) == 0, "drift leaked outside its window"


# ===========================================================================
class TestGrain:
    def test_departure_night_is_not_charged(self):
        """Half-open interval. Violating it inflates room-nights by ~1/ALOS."""
        bad = db.scalar("""
            SELECT count(*) FROM mart.fact_unit_night un
            JOIN mart.fact_booking b ON b.booking_key = un.booking_key
            WHERE b.stay_type='nightly' AND b.nights > 0
              AND un.stay_date >= b.check_out_date
        """)
        assert bad == 0

    def test_unit_night_grain_is_unique(self):
        dupes = db.scalar("""
            SELECT count(*) FROM (
                SELECT unit_key, stay_date FROM mart.fact_unit_night
                GROUP BY 1,2 HAVING count(*) > 1) t
        """)
        assert dupes == 0

    def test_cancelled_bookings_hold_no_inventory(self):
        bad = db.scalar("""
            SELECT count(*) FROM mart.fact_unit_night un
            JOIN mart.fact_booking b ON b.booking_key = un.booking_key
            WHERE un.is_occupied AND b.status IN ('cancelled','no_show')
        """)
        assert bad == 0

    def test_vacant_nights_carry_no_revenue(self):
        assert db.scalar("""
            SELECT count(*) FROM mart.fact_unit_night
            WHERE NOT is_occupied AND room_revenue_net_inr <> 0
        """) == 0


# ===========================================================================
class TestReferentialIntegrity:
    def test_occupied_night_always_has_a_booking(self):
        assert db.scalar("SELECT count(*) FROM mart.fact_unit_night "
                         "WHERE is_occupied AND booking_key IS NULL") == 0

    def test_out_of_order_is_never_sellable(self):
        assert db.scalar("SELECT count(*) FROM mart.fact_unit_night "
                         "WHERE is_out_of_order AND is_sellable") == 0

    def test_every_unit_belongs_to_a_property(self):
        assert db.scalar("""
            SELECT count(*) FROM mart.dim_unit u
            LEFT JOIN mart.dim_property p ON p.property_key = u.property_key
            WHERE p.property_key IS NULL
        """) == 0

    def test_aspects_only_reference_real_reviews(self):
        assert db.scalar("""
            SELECT count(*) FROM mart.fact_review_aspect a
            LEFT JOIN mart.fact_review r ON r.review_key = a.review_key
            WHERE r.review_key IS NULL
        """) == 0


# ===========================================================================
class TestReconciliation:
    def test_room_nights_reconcile_across_grains(self):
        """Bookings imply N nights; the unit-night table must materialise ~N.

        A gap is expected and bounded: out-of-order units suppress occupancy and
        deliberately-planted duplicate bookings claim the same night twice.
        """
        r = db.fetch_all("""
            SELECT (SELECT sum(check_out_date - check_in_date)
                    FROM mart.fact_booking
                    WHERE status NOT IN ('cancelled','no_show')
                      AND stay_type='nightly')                       AS implied,
                   (SELECT count(*) FROM mart.fact_unit_night un
                    JOIN mart.fact_booking b ON b.booking_key = un.booking_key
                    WHERE un.is_occupied AND b.stay_type='nightly')  AS materialised
        """)[0]
        implied, mat = int(r["implied"]), int(r["materialised"])
        drift = (implied - mat) / implied
        assert 0 <= drift < 0.12, f"{drift:.1%} drift is beyond OOO + planted duplicates"

    def test_payments_mostly_resolve_to_bookings(self):
        r = db.fetch_all("""
            SELECT count(*) total, count(*) FILTER (WHERE booking_key IS NULL) orphan
            FROM mart.fact_payment
        """)[0]
        rate = int(r["orphan"]) / int(r["total"])
        # Orphans are planted on purpose; they must exist but stay rare.
        assert 0 < rate < 0.05


# ===========================================================================
class TestDataQuality:
    def test_no_rule_errors(self):
        """A rule that throws is a broken check and must not read as healthy."""
        report = dq_runner.run_all(persist_results=False)
        errored = [r.rule.rule_id for r in report["results"] if r.error]
        assert not errored, f"rules errored: {errored}"

    def test_every_planted_defect_class_is_caught(self):
        report = dq_runner.run_all(persist_results=False)
        missed = [d["defect_class"] for d in report["defect_recall"]
                  if not d["detected"]]
        assert not missed, f"undetected defect classes: {missed}"

    def test_quality_score_is_bounded(self):
        report = dq_runner.run_all(persist_results=False)
        assert 0 <= report["quality_score"] <= 100

    def test_every_rule_declares_its_expectation(self):
        undocumented = [r.rule_id for r in dq_rules.RULES
                        if not r.description or not r.expectation]
        assert not undocumented


# ===========================================================================
class TestAnomalyDetection:
    def test_flags_an_injected_step_change(self):
        rng = np.random.default_rng(7)
        days = pd.date_range("2026-01-01", periods=120, freq="D")
        # Weekly seasonality plus noise, then a hard step on one day.
        vals = 100 + 25 * (days.dayofweek < 5) + rng.normal(0, 3, len(days))
        vals[100] = 20
        df = pd.DataFrame({"stay_date": days, "m": vals})
        hits = an.detect(df, metric="m", min_abs_change=10.0)
        assert any(h.date == days[100].date().isoformat() for h in hits)

    def test_does_not_flag_pure_weekly_seasonality(self):
        """The failure mode of a naive rolling mean: alarming every weekend."""
        days = pd.date_range("2026-01-01", periods=120, freq="D")
        vals = np.where(days.dayofweek < 5, 100.0, 60.0)   # noiseless, strong cycle
        df = pd.DataFrame({"stay_date": days, "m": vals})
        hits = an.detect(df, metric="m", min_abs_change=5.0)
        assert not hits, f"seasonality misread as anomalies: {[h.date for h in hits]}"

    def test_materiality_gate_suppresses_trivial_deviations(self):
        rng = np.random.default_rng(3)
        days = pd.date_range("2026-01-01", periods=120, freq="D")
        vals = 100 + rng.normal(0, 0.05, len(days))
        vals[100] = 101.0     # statistically enormous, operationally nothing
        df = pd.DataFrame({"stay_date": days, "m": vals})
        assert an.detect(df, metric="m", min_abs_change=0.0)
        assert not an.detect(df, metric="m", min_abs_change=5.0)

    def test_false_alert_budget_is_monotonic_in_threshold(self):
        loose = an.false_alert_budget(6, 4, z_threshold=2.0)
        tight = an.false_alert_budget(6, 4, z_threshold=3.5)
        assert (loose["expected_false_alerts_per_day"]
                > tight["expected_false_alerts_per_day"])

    def test_planted_f1_degradation_is_detectable(self):
        f1 = next(f for f in spec.PLANTED_FINDINGS
                  if f.key == "F1_KOR_SLA_DEGRADATION")
        r = db.fetch_all("""
            SELECT round(avg(resolution_minutes) FILTER (WHERE request_date >= :s)::numeric, 1) aft,
                   round(avg(resolution_minutes) FILTER (WHERE request_date <  :s)::numeric, 1) bef
            FROM mart.v_service_kpi
            WHERE property_code='BLR-KOR' AND owning_team='housekeeping'
              AND day_part_ist='evening' AND resolution_minutes IS NOT NULL
        """, s=f1.window[0])[0]
        assert float(r["aft"]) / float(r["bef"]) > 1.4

    def test_decoy_does_not_look_like_a_rate_problem(self):
        """D1: mix moved, rate held, RevPAR held. Must not read as a price cut."""
        r = db.fetch_all("""
            WITH win AS (SELECT sum(room_revenue_net_inr) rv, sum(rooms_sold) s,
                                sum(rooms_available) a
                         FROM mart.v_daily_kpi WHERE stay_date BETWEEN :s AND :e),
                 base AS (SELECT sum(room_revenue_net_inr) rv, sum(rooms_sold) s,
                                 sum(rooms_available) a
                          FROM mart.v_daily_kpi
                          WHERE stay_date BETWEEN (CAST(:s AS date) - INTERVAL '61 days')
                                              AND (CAST(:s AS date) - INTERVAL '1 day'))
            SELECT (SELECT rv/s FROM win) - (SELECT rv/s FROM base) adr_d,
                   (SELECT rv/a FROM win) - (SELECT rv/a FROM base) revpar_d
        """, s=spec.DECOY.window[0], e=spec.DECOY.window[1])[0]
        assert abs(float(r["adr_d"])) < 350.0
        assert float(r["revpar_d"]) > -200.0


# ===========================================================================
class TestAIValidation:
    def test_evidence_validator_blocks_a_fabricated_quote(self):
        """The gate never fired on the real corpus, so prove it can fire.

        A validator that has never rejected anything is indistinguishable from a
        validator that cannot reject anything. This forces the failure.
        """
        source = "The apartment was spotless but the AC took a day to fix."
        assert _norm("the AC took a day to fix") in _norm(source)
        assert _norm("the AC was replaced within the hour") not in _norm(source)

    def test_validator_tolerates_whitespace_and_case(self):
        source = "Housekeeping   took nearly TWO hours to respond."
        assert _norm("housekeeping took nearly two hours") in _norm(source)

    def test_all_published_aspects_are_evidence_verified(self):
        assert db.scalar("SELECT count(*) FROM mart.fact_review_aspect "
                         "WHERE NOT evidence_verified") == 0

    def test_published_evidence_really_is_a_substring(self):
        """End-to-end: re-verify every stored span against its source text."""
        rows = db.fetch_all("""
            SELECT a.evidence_span, r.review_text
            FROM mart.fact_review_aspect a
            JOIN mart.fact_review r ON r.review_key = a.review_key
            WHERE a.evidence_verified
        """)
        assert rows, "no aspects extracted yet"
        bad = [r for r in rows
               if _norm(r["evidence_span"]) not in _norm(r["review_text"])]
        assert not bad, f"{len(bad)} published spans are not in their source"

    def test_aspect_categories_are_within_the_taxonomy(self):
        from staypulse.ai.client import CATEGORIES
        rows = db.fetch_all(
            "SELECT DISTINCT category FROM mart.fact_review_aspect")
        unknown = [r["category"] for r in rows if r["category"] not in CATEGORIES]
        assert not unknown, f"off-taxonomy categories: {unknown}"

    def test_baseline_finds_aspects_and_polarity(self):
        rows = baseline.classify(
            "The room was spotless but housekeeping took nearly two hours.")
        cats = {r["category"]: r["polarity"] for r in rows}
        assert cats.get("cleanliness") == "positive"
        assert cats.get("housekeeping_response") == "negative"

    def test_baseline_returns_nothing_for_empty_input(self):
        assert baseline.classify("") == []


# ===========================================================================
class TestReproducibility:
    def test_same_seed_same_dataset(self):
        a = dataset_fingerprint(Generator(seed=4242).generate(n_guests=500))
        b = dataset_fingerprint(Generator(seed=4242).generate(n_guests=500))
        assert a == b

    def test_different_seed_different_dataset(self):
        a = dataset_fingerprint(Generator(seed=1).generate(n_guests=500))
        b = dataset_fingerprint(Generator(seed=2).generate(n_guests=500))
        assert a != b

    def test_generator_records_ground_truth(self):
        data = Generator(seed=99).generate(n_guests=500)
        assert not data.review_truth.empty
        assert set(data.review_truth.columns) >= {
            "review_id", "category", "polarity", "severity", "actionable_by"}


# ===========================================================================
class TestSemanticLayer:
    def test_every_metric_declares_a_date_basis(self):
        assert db.scalar("SELECT count(*) FROM meta.metric_definition "
                         "WHERE date_basis IS NULL") == 0

    def test_date_basis_is_check_constrained(self):
        """The constraint is the control. Without it the field is a comment."""
        from sqlalchemy import text
        with pytest.raises(Exception):
            with db.connect() as conn:
                conn.execute(text("""
                    INSERT INTO meta.metric_definition
                        (metric_key, display_name, business_definition, formula_text,
                         sql_expression, grain, date_basis, unit, source_tables, owner_team)
                    VALUES ('t_bad','t','t','t','t','t','whenever_feels_right','count',
                            ARRAY['t'],'t')
                """))

    def test_no_duplicate_metric_names(self):
        assert db.scalar("""
            SELECT count(*) FROM (
                SELECT display_name FROM meta.metric_definition
                GROUP BY 1 HAVING count(*) > 1) t
        """) == 0

    def test_every_metric_documents_caveats(self):
        assert db.scalar("SELECT count(*) FROM meta.metric_definition "
                         "WHERE caveats IS NULL OR caveats = ''") == 0

    def test_gst_resolves_across_the_rate_change(self):
        """5% below / 18% above the threshold, after 22 Sep 2025."""
        assert float(db.scalar(
            "SELECT meta.gst_pct(DATE '2026-01-15', 5000)")) == 5.0
        assert float(db.scalar(
            "SELECT meta.gst_pct(DATE '2026-01-15', 7600)")) == 18.0
        # Before the change the lower band was 12%.
        assert float(db.scalar(
            "SELECT meta.gst_pct(DATE '2025-05-15', 5000)")) == 12.0
