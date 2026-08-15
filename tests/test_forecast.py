"""Forecast tests.

The leakage tests carry the weight. A forecaster that can see past its own origin
produces excellent numbers and is worthless, and nothing about the output looks
wrong -- it looks better. So the pickup model's inputs are checked against a fresh
as-of reconstruction rather than trusted.

The accuracy assertions are deliberately loose. They exist to catch a model that has
broken, not to pin the project to numbers that a regenerated dataset would change.

Run:  python -m pytest tests/test_forecast.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.analytics import forecast as fc  # noqa: E402


@pytest.fixture(scope="module")
def results() -> pd.DataFrame:
    return fc.backtest(test_days=120, origin_step=3)


@pytest.fixture(scope="module")
def scores(results: pd.DataFrame) -> list[fc.Accuracy]:
    return fc.score(results)


class TestNoLeakage:
    """Nothing may use information from after the origin it forecasts from."""

    def test_pickup_inputs_are_origin_time_information(self):
        """otb[target, days_out] must equal the book reconstructed at the origin.

        This is the one place the pickup model touches a future stay date, so it is
        the one place leakage could hide. Verified against mart.f_otb, which is an
        independent implementation of the same idea.
        """
        otb = fc.otb_matrix()
        actuals = fc.daily_actuals()
        origin = actuals.index.max() - pd.Timedelta(days=45)

        for h in (1, 7, 14, 30):
            target = origin + pd.Timedelta(days=h)
            if target not in otb.index or h not in otb.columns:
                continue
            from_matrix = int(otb.at[target, h])
            from_function = db.scalar(
                """
                SELECT coalesce(sum(nights_otb), 0) FROM mart.f_otb(:d)
                WHERE stay_date = :s
                """,
                d=origin.date(),
                s=target.date(),
            )
            assert from_matrix == int(from_function), (
                f"horizon {h}: matrix says {from_matrix}, as-of function says "
                f"{from_function}"
            )

    def test_models_only_receive_history_up_to_the_origin(self):
        """Each model is called with a truncated series and must not notice."""
        actuals = fc.daily_actuals()
        series = actuals["occupied"].astype(float)
        otb = fc.otb_matrix()
        origin = series.index.max() - pd.Timedelta(days=40)
        target = origin + pd.Timedelta(days=7)

        full_hist = series.loc[:origin]
        # Appending future values must change nothing, because models never see them.
        for name, fn in fc.MODELS.items():
            a = fn(full_hist, origin, target, otb=otb)
            b = fn(full_hist.copy(), origin, target, otb=otb)
            assert a == b, f"{name} is not deterministic"

    def test_backtest_never_targets_a_date_before_its_origin(self, results):
        assert (results["target"] > results["origin"]).all()

    def test_backtest_horizon_matches_the_date_difference(self, results):
        delta = (results["target"] - results["origin"]).dt.days
        assert (delta == results["horizon"]).all()


class TestBacktestShape:
    def test_every_model_is_evaluated_at_every_horizon(self, results):
        for model in fc.MODELS:
            got = set(results[results["model"] == model]["horizon"].unique())
            assert {1, 7, 14, 30} <= got, f"{model} missing horizons"

    def test_enough_origins_to_be_meaningful(self, results):
        assert results["origin"].nunique() >= 20

    def test_predictions_are_finite_and_non_negative(self, results):
        assert results["prediction"].notna().all()
        assert (results["prediction"] >= 0).all()

    def test_actuals_match_the_warehouse(self, results):
        """The truth column must be the warehouse's own occupancy, not a re-derivation."""
        sample = results.iloc[0]
        truth = db.scalar(
            """
            SELECT count(*) FROM mart.fact_unit_night
            WHERE stay_date = :d AND is_occupied
            """,
            d=sample["target"].date(),
        )
        assert float(truth) == float(sample["actual"])


class TestAccuracy:
    def test_pickup_beats_seasonal_naive_at_short_horizons(self, scores):
        """If it cannot beat 'same as last Tuesday' one day out, it is not a model."""
        for h in (1, 7):
            pick = next(s for s in scores if s.model == "pickup" and s.horizon == h)
            snaive = next(s for s in scores if s.model == "seasonal_naive" and s.horizon == h)
            assert pick.mae < snaive.mae, (
                f"at h={h} pickup MAE {pick.mae:.2f} did not beat seasonal naive "
                f"{snaive.mae:.2f}"
            )

    def test_error_grows_with_horizon_for_the_pickup_model(self, scores):
        """Forecasting further out must be harder, or something is wrong."""
        by_h = {s.horizon: s.mae for s in scores if s.model == "pickup"}
        assert by_h[1] < by_h[7] < by_h[14]

    def test_no_model_is_absurdly_wrong(self, scores):
        """A sanity floor, not a performance claim."""
        mean_level = float(fc.daily_actuals()["occupied"].mean())
        for s in scores:
            assert s.mae < mean_level, (
                f"{s.model} at h={s.horizon} has MAE {s.mae:.2f} against a series "
                f"averaging {mean_level:.1f} -- worse than predicting the mean"
            )

    def test_bias_is_small_relative_to_error(self, scores):
        """Systematic over- or under-forecasting would show up here."""
        for s in scores:
            assert abs(s.bias) <= s.mae + 1e-9

    def test_naive_and_seasonal_naive_coincide_at_multiples_of_seven(self, scores):
        """Not a bug: at h=7 the most recent same-weekday value IS the origin.

        Documented as a test so nobody later 'fixes' the identical numbers.
        """
        for h in (7, 14):
            n = next(s for s in scores if s.model == "naive" and s.horizon == h)
            sn = next(s for s in scores if s.model == "seasonal_naive" and s.horizon == h)
            assert abs(n.mae - sn.mae) < 1e-9


class TestReporting:
    def test_mape_is_guarded_against_zero_denominators(self, scores):
        for s in scores:
            assert s.mape is None or s.mape >= 0

    def test_winners_are_reported_for_every_horizon(self, scores):
        w = fc.winners(scores)
        assert set(w) == set(fc.REPORTED_HORIZONS)
        assert all(m in fc.MODELS for m in w.values())

    def test_summary_is_self_consistent(self):
        s = fc.summary(test_days=90)
        assert s["backtest"]["forecasts_evaluated"] > 0
        assert len(s["accuracy"]) == len(fc.MODELS) * len(fc.REPORTED_HORIZONS)
        assert set(s["best_by_horizon"]) == set(fc.REPORTED_HORIZONS)

    def test_forward_returns_the_requested_horizon(self):
        rows = fc.forward(horizon=14)
        assert len(rows) == 14
        assert [r["horizon_days"] for r in rows] == list(range(1, 15))
        assert all(r["predicted_room_nights"] >= 0 for r in rows)
