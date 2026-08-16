"""Calendar and holiday intelligence tests.

Three things are pinned here.

NON-CIRCULARITY. The effect is measured from public-holiday DATES only. Nothing in
the measurement path may read the generator's planted festival windows, or the
validation would be assuming its own answer.

NO LEAKAGE. A multiplier estimated "as of" a date may not see past it, or the
forecast evaluation is worthless.

THE NEGATIVE RESULT. Holiday-aware forecasting measurably does NOT work on this
dataset, and that is pinned deliberately. If a future change makes
`seasonal_holiday` beat its baseline, the burden is to prove that came from more
data rather than from fitting the test.

Run:  python -m pytest tests/test_calendar.py -v
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.signals import calendar as cal  # noqa: E402

SOURCE = PROJECT_ROOT / "data" / "reference" / "india_holidays.json"


@pytest.fixture(scope="module")
def source() -> dict:
    return json.loads(SOURCE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def effects() -> list[cal.HolidayEffect]:
    return cal.holiday_effects()


class TestCuratedSource:
    def test_source_is_committed_and_parseable(self, source):
        assert source["holidays"]
        assert len(source["holidays"]) >= 15

    def test_every_entry_is_dated_named_and_scoped(self, source):
        for h in source["holidays"]:
            dt.date.fromisoformat(h["date"])
            assert h["name"] and h["scope"] and h["kind"]
            assert h["confidence"] in ("fixed", "lunar")

    def test_no_duplicate_date_and_name(self, source):
        seen = {(h["date"], h["name"]) for h in source["holidays"]}
        assert len(seen) == len(source["holidays"])

    def test_it_documents_why_there_is_no_api(self, source):
        why = " ".join(source["_about"]["why_not_an_api"]).lower()
        assert "nager" in why and "204" in why

    def test_it_records_that_windows_are_not_encoded_here(self, source):
        """Storing the planted windows would make validation circular."""
        note = source["_about"]["independence_note"].lower()
        assert "dates only" in note or "holiday dates only" in note
        blob = json.dumps(source).lower()
        for banned in ("multiplier", "demand_window", "festival_window"):
            assert banned not in blob


class TestCalendarLoad:
    def test_holidays_are_flagged_in_dim_date(self, source):
        flagged = db.scalar("SELECT count(*) FROM mart.dim_date WHERE is_public_holiday")
        assert flagged == len(source["holidays"])

    def test_provenance_is_recorded(self):
        rows = db.fetch_all(
            "SELECT * FROM meta.calendar_source WHERE source_key = 'india_holidays'"
        )
        assert rows, "calendar provenance missing; run scripts/load_calendar.py"
        r = rows[0]
        assert r["entry_count"] > 0
        assert r["needs_review"] > 0, "lunar dates must be flagged for human review"
        assert "nager" in r["origin"].lower()

    def test_offset_is_zero_on_a_holiday(self):
        bad = db.scalar("""
            SELECT count(*) FROM mart.dim_date
            WHERE is_public_holiday AND days_to_holiday <> 0
        """)
        assert bad == 0

    def test_offset_sign_convention(self):
        """Negative before the holiday, positive after."""
        r = db.fetch_all("""
            SELECT full_date, days_to_holiday, nearest_holiday
            FROM mart.dim_date
            WHERE nearest_holiday = 'Diwali' AND full_date IN
                  (DATE '2025-10-18', DATE '2025-10-22')
            ORDER BY full_date
        """)
        assert len(r) == 2
        assert r[0]["days_to_holiday"] < 0
        assert r[1]["days_to_holiday"] > 0

    def test_adjacency_matches_the_documented_radius(self):
        from scripts.load_calendar import ADJACENCY_RADIUS_DAYS  # noqa: PLC0415
        bad = db.scalar("""
            SELECT count(*) FROM mart.dim_date
            WHERE is_holiday_adjacent
              AND abs(days_to_holiday) > :r
        """, r=ADJACENCY_RADIUS_DAYS)
        assert bad == 0

    def test_bridge_days_are_working_days(self):
        """A bridge day is a weekday trapped between non-working days."""
        bad = db.scalar("""
            SELECT count(*) FROM mart.dim_date
            WHERE is_bridge_day AND (is_weekend OR is_public_holiday)
        """)
        assert bad == 0

    def test_dates_far_from_any_holiday_are_not_adjacent(self):
        row = db.fetch_all("""
            SELECT count(*) n FROM mart.dim_date
            WHERE NOT is_holiday_adjacent AND full_date BETWEEN
                  (SELECT min(stay_date) FROM mart.fact_unit_night)
              AND (SELECT max(stay_date) FROM mart.fact_unit_night)
        """)[0]
        assert int(row["n"]) > 100, "expected plenty of ordinary dates"


class TestMeasurementIsNonCircular:
    def test_measurement_never_imports_the_generator_spec(self):
        """Only validate_against_planted may touch the planted windows."""
        src = (PROJECT_ROOT / "src" / "staypulse" / "signals" / "calendar.py").read_text(
            encoding="utf-8"
        )
        head, _, tail = src.partition("def validate_against_planted")
        assert "generate.spec" not in head, (
            "the measurement path imports the generator spec; the result would be "
            "assuming its own answer"
        )
        assert "generate.spec" in tail, "validation should read the planted windows"

    def test_profile_covers_offsets_either_side(self):
        profile = cal.offset_profile()
        offsets = {p.offset for p in profile}
        assert min(offsets) < 0 < max(offsets)
        assert 0 in offsets

    def test_every_offset_reports_its_sample_size(self):
        for p in cal.offset_profile():
            assert p.observations > 0
            assert p.ci_low_pp <= p.effect_pp <= p.ci_high_pp


class TestPlantedEffectIsRecovered:
    """The generator plants suppressive windows. Did measuring find them?"""

    def test_diwali_effect_is_negative_and_excludes_zero(self, effects):
        diwali = next((e for e in effects if e.name == "Diwali"), None)
        assert diwali is not None, "Diwali should have measurable adjacent dates"
        assert diwali.effect_pp < 0, (
            f"Diwali measured {diwali.effect_pp:+.2f}pp; the generator plants a "
            "x0.62 suppression, so a positive result means the baseline is wrong"
        )
        assert diwali.ci_high_pp < 0, "Diwali effect should be clearly below zero"

    def test_year_end_holidays_are_suppressive(self, effects):
        by_name = {e.name: e for e in effects}
        for name in ("Christmas Day", "New Year's Day"):
            assert name in by_name, f"{name} not measured"
            assert by_name[name].effect_pp < 0, f"{name} should suppress demand here"

    def test_validation_reports_planted_windows_without_using_them(self):
        v = cal.validate_against_planted()
        assert v["planted_windows"]
        assert v["planted_windows_in_data"] == 3, (
            "three of four planted windows fall inside the data; Diwali 2026 is "
            "after the horizon"
        )
        assert v["checks"]["diwali_measured"] is True
        assert v["checks"]["diwali_effect_negative"] is True
        assert "not used in the measurement" in v["method"]

    def test_direction_is_suppressive_overall(self, effects):
        """Corporate aparthotel: business travel stops during festivals."""
        negative = [e for e in effects if e.effect_pp < 0]
        assert len(negative) > len(effects) / 2

    def test_interpretation_declines_to_overclaim(self, effects):
        text = cal.interpretation(effects)
        assert "suppress" in text.lower()
        assert "small" in text.lower() or "indicative" in text.lower()


class TestNoLeakage:
    def test_multiplier_respects_the_as_of_date(self):
        """A multiplier estimated as of T may not see a holiday after T."""
        early = dt.date(2025, 6, 1)
        rows = db.fetch_all("""
            SELECT DISTINCT nearest_holiday h FROM mart.dim_date d
            JOIN mart.fact_unit_night f ON f.stay_date = d.full_date
            WHERE d.is_holiday_adjacent AND d.full_date >= :d
        """, d=early)
        later_only = {r["h"] for r in rows} - {
            r["h"] for r in db.fetch_all("""
                SELECT DISTINCT nearest_holiday h FROM mart.dim_date d
                JOIN mart.fact_unit_night f ON f.stay_date = d.full_date
                WHERE d.is_holiday_adjacent AND d.full_date < :d
            """, d=early)
        }
        estimated = {h for h, _ in cal.holiday_multiplier(early, significant_only=False)}
        assert not (estimated & later_only), (
            f"multiplier leaked holidays only observable after {early}: "
            f"{estimated & later_only}"
        )

    def test_earlier_as_of_uses_no_more_data_than_later(self):
        early = cal.holiday_multiplier(dt.date(2025, 8, 1), significant_only=False)
        late = cal.holiday_multiplier(dt.date(2026, 6, 1), significant_only=False)
        assert len(early) <= len(late)

    def test_profile_as_of_excludes_future_dates(self):
        cutoff = dt.date(2025, 9, 1)
        full = cal.offset_profile()
        limited = cal.offset_profile(cutoff)
        assert sum(p.observations for p in limited) < sum(p.observations for p in full)


class TestForecastIntegrationIsAnHonestFailure:
    """The holiday model does NOT work on this dataset. That is pinned on purpose.

    Three variants were measured on holiday-adjacent dates against a 4.19 baseline:
    pooled fallback 5.11, specific-only 4.94, significance-gated 4.90. Every one is
    worse. The mechanism is documented in the module and in reports/CALENDAR.md.

    If a future change makes this model win, the burden is to show the improvement
    came from more data, not from fitting the evaluation.
    """

    def test_model_is_registered_so_its_loss_is_published(self):
        from staypulse.analytics import forecast as fc
        assert "seasonal_holiday" in fc.MODELS, (
            "the model must stay in the comparison table; hiding a model that lost "
            "is the same failure as hiding a losing horizon"
        )

    def test_model_is_identical_to_its_baseline_away_from_holidays(self):
        """It claims to help only near holidays, so elsewhere it must not move."""
        from staypulse.analytics import forecast as fc
        results = fc.backtest(test_days=90, origin_step=7)
        ordinary = results[~results["holiday_adjacent"]]
        a = ordinary[ordinary["model"] == "seasonal_holiday"]["prediction"].to_numpy()
        b = ordinary[ordinary["model"] == "dow_moving_average"]["prediction"].to_numpy()
        assert len(a) == len(b) > 0
        assert (abs(a - b) < 1e-9).all(), (
            "the holiday model altered a date with no holiday nearby"
        )

    def test_backtest_marks_holiday_adjacent_targets(self):
        from staypulse.analytics import forecast as fc
        results = fc.backtest(test_days=90, origin_step=7)
        assert "holiday_adjacent" in results.columns
        assert results["holiday_adjacent"].dtype == bool

    def test_standard_window_contains_no_festival_window(self):
        """The reason a separate holiday evaluation exists at all."""
        from staypulse.generate.spec import FESTIVAL_WINDOWS
        horizon = db.scalar("SELECT max(stay_date) FROM mart.fact_unit_night")
        start = horizon - dt.timedelta(days=120)
        inside = [n for a, b, _, n in FESTIVAL_WINDOWS if a >= start and b <= horizon]
        assert not inside, (
            f"a festival window entered the standard backtest ({inside}); the "
            "holiday evaluation's rationale needs revisiting"
        )


class TestSummaryContract:
    def test_summary_discloses_source_and_review_burden(self):
        s = cal.summary()
        assert s["source"]["entries"] > 0
        assert s["source"]["entries_needing_human_review"] > 0
        assert "nager" in s["source"]["origin"].lower()

    def test_summary_states_no_window_is_assumed(self):
        s = cal.summary()
        assert "no demand window is assumed" in s["method"].lower()

    def test_summary_carries_profile_and_holidays(self):
        s = cal.summary()
        assert s["holidays"] and s["offset_profile"]
        assert s["interpretation"]
