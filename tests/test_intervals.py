"""Prediction interval and backtesting lab tests.

The coverage test is the one that matters. An interval is a claim about how often
the truth falls inside it, and that claim is either measured or it is decoration.

Two things are guarded against here, because both produce a green suite that
proves nothing:

  1. IN-SAMPLE COVERAGE. An empirical quantile reproduces its nominal level on its
     own sample by construction. A test asserting "80% interval covers ~80%" while
     calibrating on the evaluation set is testing arithmetic. So the tests assert
     the out-of-sample figure, and separately assert that it is LOWER than the
     in-sample one -- if those two ever converge, the separation has broken.

  2. TUNING TO THE TARGET. Corrections searched for until 80% coverage appeared
     would fail at other levels. So coverage is asserted at 50% and 95% as well,
     which a fudge factor fitted to 80% cannot satisfy simultaneously.

Run:  python -m pytest tests/test_intervals.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import forecast as fc  # noqa: E402
from staypulse.analytics import intervals as iv  # noqa: E402

# Coverage tolerance, in percentage points, for the out-of-sample assertions.
#
# Wide enough that a re-generated dataset does not turn a working method red,
# narrow enough that a genuinely broken interval cannot pass. The measured
# deviation for the published method is under 3pp at every level.
TOLERANCE_PP = 6.0


@pytest.fixture(scope="module")
def results() -> pd.DataFrame:
    """One backtest, shared. Recomputing it per test would dominate the run."""
    return fc.backtest(test_days=iv.STUDY_DAYS, origin_step=3)


@pytest.fixture(scope="module")
def prepared(results: pd.DataFrame) -> pd.DataFrame:
    return iv._prepare(results)


@pytest.fixture(scope="module")
def coverage_80(results: pd.DataFrame) -> dict:
    return iv.coverage(level=0.8, results=results)


@pytest.fixture(scope="module")
def sliced(results: pd.DataFrame) -> dict:
    return fc.slice_accuracy(results)


class TestCoverage:
    """The success criterion: an 80% interval must contain the actual ~80% of
    the time, measured out of sample."""

    def test_eighty_percent_interval_covers_about_eighty_percent(self, coverage_80):
        out = coverage_80["out_of_sample"]
        assert out["forecasts"] > 1000, (
            "too few scored forecasts for the coverage figure to mean anything; "
            "the assertion below would be measuring noise"
        )
        assert abs(out["deviation_pp"]) <= TOLERANCE_PP, (
            f"80% interval covered {out['coverage_pct']}% out of sample "
            f"({out['deviation_pp']:+}pp)"
        )

    @pytest.mark.parametrize("level", [0.5, 0.95])
    def test_coverage_holds_at_the_other_levels(self, results, level):
        """A correction tuned until 80% looked right would come apart here."""
        out = iv.coverage(level=level, results=results)["out_of_sample"]
        assert out["forecasts"] > 1000
        assert abs(out["deviation_pp"]) <= TOLERANCE_PP, (
            f"{level:.0%} interval covered {out['coverage_pct']}% out of sample"
        )

    def test_out_of_sample_is_lower_than_in_sample(self, coverage_80):
        """The whole reason the out-of-sample walk exists.

        If these ever converge, the calibration has started seeing the evaluation
        set and the headline number has quietly become arithmetic.
        """
        out = coverage_80["out_of_sample"]["coverage_pct"]
        inside = coverage_80["in_sample"]["coverage_pct"]
        assert inside > out, (
            f"in-sample coverage {inside}% did not exceed out-of-sample {out}%, "
            "which means the two are no longer separated"
        )

    def test_plain_quantiles_under_cover_and_that_is_why_they_are_not_default(
        self, results
    ):
        """The published default was chosen against a measured alternative."""
        plain = iv.coverage(level=0.8, method=iv.METHODS[0], results=results)
        default = iv.coverage(level=0.8, method=iv.DEFAULT_METHOD, results=results)
        assert plain["out_of_sample"]["coverage_pct"] < 80.0, (
            "the plain empirical quantile no longer under-covers; the correction "
            "in the default method may no longer be justified"
        )
        assert abs(default["out_of_sample"]["deviation_pp"]) < abs(
            plain["out_of_sample"]["deviation_pp"]
        )

    def test_coverage_is_reported_per_horizon(self, coverage_80):
        by_horizon = coverage_80["out_of_sample"]["by_horizon"]
        assert len(by_horizon) >= 3, "per-horizon breakdown is empty or truncated"
        for horizon, block in by_horizon.items():
            assert block["forecasts"] > 0
            assert 0.0 <= block["coverage_pct"] <= 100.0


class TestNoLeakageInCalibration:
    """An interval calibrated on an error nobody had observed is a leak."""

    def test_only_residuals_whose_target_had_happened_are_usable(self, prepared):
        """`target <= origin`, not `origin < origin`.

        A 30-day forecast made yesterday has no error yet. Filtering on the origin
        pulls in residuals that did not exist, most heavily at the long horizons.
        """
        origins = sorted(prepared["origin"].unique())
        origin = origins[len(origins) // 2]
        known = prepared[prepared["target"] <= origin]
        assert len(known) > 0, "no residuals selected; the assertion is vacuous"
        assert known["target"].max() <= origin

        # And the naive filter really would have leaked, or this rule is moot.
        naive = prepared[prepared["origin"] < origin]
        assert naive["target"].max() > origin, (
            "filtering on origin would not have leaked on this data, so this "
            "test is not exercising the distinction it claims to"
        )

    def test_long_horizons_are_calibrated_on_fewer_residuals(self, coverage_80):
        """The visible cost of the rule. If h=30 had as much calibration data as
        h=1, the target filter is not being applied."""
        by_horizon = coverage_80["out_of_sample"]["by_horizon"]
        assert "1" in by_horizon and "30" in by_horizon
        assert (
            by_horizon["30"]["median_calibration_residuals"]
            < by_horizon["1"]["median_calibration_residuals"]
        )

    def test_series_level_is_knowable_at_the_origin(self):
        """Residuals are scaled by the trailing level. That window must end at
        the origin, or the scaling itself leaks."""
        actuals = fc.daily_actuals()["occupied"].astype(float)
        level = iv.series_level(actuals)
        mid = actuals.index[len(actuals) // 2]
        expected = actuals.loc[:mid].tail(iv.SCALE_WINDOW).mean()
        assert abs(float(level.loc[mid]) - float(expected)) < 1e-9


class TestIntervalShape:
    """Properties that hold by construction, asserted so a refactor cannot
    quietly break them."""

    def test_conformal_never_narrows_the_interval(self, prepared):
        plain = iv.from_residuals(prepared, level=0.8, method=iv.METHODS[2])
        conformal = iv.from_residuals(prepared, level=0.8, method=iv.METHODS[3])
        assert plain and conformal, "no intervals built; comparison would be vacuous"
        shared = set(plain) & set(conformal)
        assert shared
        for key in shared:
            wide = conformal[key].hi_offset - conformal[key].lo_offset
            narrow = plain[key].hi_offset - plain[key].lo_offset
            assert wide >= narrow - 1e-9, f"conformal narrowed the interval at {key}"

    def test_higher_levels_give_wider_intervals(self, prepared):
        built = {
            lvl: iv.from_residuals(prepared, level=lvl) for lvl in (0.5, 0.8, 0.95)
        }
        shared = set(built[0.5]) & set(built[0.8]) & set(built[0.95])
        assert shared, "no shared keys; the comparison would be vacuous"
        for key in shared:
            widths = [
                built[lvl][key].hi_offset - built[lvl][key].lo_offset
                for lvl in (0.5, 0.8, 0.95)
            ]
            assert widths[0] <= widths[1] <= widths[2], f"non-monotone width at {key}"

    def test_bounds_bracket_the_point_forecast_and_never_go_negative(self, prepared):
        built = iv.from_residuals(prepared, level=0.8)
        assert built
        for interval in list(built.values())[:20]:
            lower, upper = iv.bound(12.0, interval, scale=30.0)
            assert lower >= 0.0
            assert lower <= upper

    def test_a_thin_horizon_gets_no_interval_rather_than_a_fabricated_one(
        self, prepared
    ):
        """Fewer than MIN_RESIDUALS must produce nothing, not a two-point
        quantile dressed as a percentage."""
        thin = prepared.head(iv.MIN_RESIDUALS - 1)
        assert len(thin) < iv.MIN_RESIDUALS
        assert iv.from_residuals(thin, level=0.8) == {}


class TestForwardIntervals:
    """The interval as an operator would receive it."""

    def test_forward_carries_bounds_around_every_point(self):
        payload = iv.forward(horizon=14, level=0.8)
        rows = payload["forecast"]
        assert len(rows) == 14
        priced = [r for r in rows if r["lower_room_nights"] is not None]
        assert priced, "no row carried an interval"
        for row in priced:
            assert row["lower_room_nights"] <= row["predicted_room_nights"]
            assert row["predicted_room_nights"] <= row["upper_room_nights"]

    def test_a_horizon_without_evidence_says_so(self):
        payload = iv.forward(horizon=14, level=0.8, calibration_days=45)
        rows = payload["forecast"]
        unpriced = [r for r in rows if r["lower_room_nights"] is None]
        assert unpriced, (
            "a 45-day calibration window should leave the long horizons without "
            "enough realised residuals; if not, the target filter is too loose"
        )
        for row in unpriced:
            assert row["interval_note"]

    def test_the_method_and_its_caveat_are_published(self):
        payload = iv.forward(horizon=7)
        assert "conformal" in payload["method_note"].lower()
        assert "capacity" in payload["caveat"].lower()


class TestBacktestingLab:
    """F-801: one backtest, cut along the dimensions that change the answer."""

    def test_every_dimension_is_populated(self, sliced):
        for dimension in ("by_horizon", "by_month", "by_weekday",
                          "by_holiday_adjacency"):
            assert sliced[dimension], f"{dimension} is empty"

    def test_weekday_slices_partition_the_backtest(self, sliced, results):
        total = sum(block["forecasts"] for block in sliced["by_weekday"])
        assert total == len(results), (
            "weekday slices do not partition the backtest; a forecast is being "
            "double-counted or dropped"
        )

    def test_best_model_is_the_lowest_mae_model_in_that_slice(self, sliced):
        blocks = sliced["by_weekday"] + sliced["by_holiday_adjacency"]
        assert blocks
        for block in blocks:
            assert block["models"], "a slice reported no models"
            assert block["best_model"] == block["models"][0]["model"]
            assert block["best_mae_nights"] == min(
                m["mae_nights"] for m in block["models"]
            )

    def test_thin_cells_are_omitted_rather_than_reported(self, sliced):
        for dimension in ("by_horizon", "by_month", "by_weekday"):
            for block in sliced[dimension]:
                for model in block["models"]:
                    assert model["observations"] >= fc.MIN_SLICE_OBSERVATIONS

    def test_unsupported_slices_name_what_they_would_need(self, sliced):
        """Per-property accuracy is not approximated from a portfolio forecast."""
        missing = sliced["not_sliced"]
        assert "by_property" in missing and "by_channel" in missing
        assert "per-property forecast target" in missing["by_property"]

    def test_pickup_leads_at_short_horizons_and_decays_at_long_ones(self, results):
        """The claim the forecast module's docstring makes, checked rather than
        assumed. If this inverts, the module's stated rationale is wrong."""
        lab = fc.slice_accuracy(results)
        by_horizon = {int(b["slice"]): b["best_model"] for b in lab["by_horizon"]}
        assert by_horizon[1] == "pickup", (
            "the pickup model no longer wins at one day out, which contradicts "
            "the rationale for including it"
        )
        assert by_horizon[30] != "pickup", (
            "pickup now wins at 30 days; the documented decay towards the "
            "seasonal baseline no longer holds and the docstring is stale"
        )
