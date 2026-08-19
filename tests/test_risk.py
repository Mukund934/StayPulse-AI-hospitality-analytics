"""Cancellation risk and overbooking tests.

Two different kinds of claim are under test here.

The cancellation model makes a claim about PREDICTION, and the ways that goes
wrong quietly are well known: a feature derived from the outcome, a random split
that lets the model see later bookings, scaling statistics computed over the test
set, and AUC reported without a base rate so a useless model looks strong. Each
has a test.

The overbooking simulator makes a claim about ARITHMETIC, and its failure mode is
the opposite -- producing a confident recommendation from a cost nobody supplied.
The tests assert what it refuses to do.

Run:  python -m pytest tests/test_risk.py -v
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import cancellation as cx  # noqa: E402
from staypulse.analytics import overbooking as ob  # noqa: E402


@pytest.fixture(scope="module")
def raw() -> pd.DataFrame:
    return cx.dataset()


@pytest.fixture(scope="module")
def built(raw: pd.DataFrame):
    features, names = cx.build_features(raw)
    return features, names


@pytest.fixture(scope="module")
def fitted(built):
    features, names = built
    train, test, split_date = cx.temporal_split(features)
    model = cx.fit(train, names)
    evaluation = cx.evaluate(model, train, test, split_date)
    return model, evaluation, train, test


class TestNoOutcomeLeakage:
    """A feature derived from the outcome scores beautifully and is unusable."""

    def test_design_matrix_holds_no_outcome_column(self, built):
        _, names = built
        assert names, "no features built; the loop below would be vacuous"
        banned = ("cancel", "no_show", "status", "outcome")
        for name in names:
            assert not any(token in name.lower() for token in banned), (
                f"feature {name!r} is derived from the outcome"
            )

    def test_every_feature_is_knowable_when_the_booking_is_made(self, built):
        """Each feature must come from a column that exists at booking time."""
        _, names = built
        allowed_roots = {
            "lead_time", "nights", "adults", "price_per_night", "has_discount",
            "is_weekend_arrival", "channel_", "property_", "stay_",
        }
        for name in names:
            assert any(name.startswith(root) for root in allowed_roots), (
                f"{name!r} is not obviously a booking-time feature"
            )

    def test_the_split_is_temporal_not_random(self, built):
        features, _ = built
        train, test, split_date = cx.temporal_split(features)
        assert len(train) > 0 and len(test) > 0
        assert train["booking_date"].max() < test["booking_date"].min(), (
            "a training booking was made after a test booking; the split is not "
            "temporal and the model can learn from the future"
        )
        assert test["booking_date"].min() >= split_date

    def test_scaling_statistics_come_from_training_rows_only(self, built, fitted):
        """Standardising over train and test together leaks the test
        distribution into the fitted model, and never shows up as an error."""
        features, names = built
        model, _, train, test = fitted
        train_means = train[names].to_numpy(dtype=float).mean(axis=0)
        assert np.allclose(model.means, train_means), (
            "model scaling does not match the training means"
        )
        full_means = features[names].to_numpy(dtype=float).mean(axis=0)
        assert not np.allclose(model.means, full_means), (
            "model scaling matches the FULL dataset means, so the test rows "
            "were used to fit the scaler"
        )


class TestModelPerformance:
    """Performance claims, each with the context that makes them meaningful."""

    def test_the_model_beats_a_coin_toss(self, fitted):
        _, evaluation, _, _ = fitted
        assert evaluation.n_test > 200, "test set too small to judge"
        assert evaluation.auc > 0.60, (
            f"AUC {evaluation.auc:.3f}; the generator plants a channel and "
            "lead-time mechanism, so a model that cannot beat 0.60 is broken "
            "rather than merely unlucky"
        )

    def test_precision_is_reported_against_the_base_rate(self, fitted):
        """Precision alone is meaningless. The lift is the claim."""
        _, evaluation, _, _ = fitted
        payload = evaluation.as_dict()
        assert payload["base_rate_pct"] > 0
        assert payload["classification_at_threshold"]["lift_over_base_rate"] > 1.2, (
            "the model does not beat the base rate by a usable margin"
        )

    def test_accuracy_is_deliberately_not_reported(self, fitted):
        """On a 12% base rate, 'never cancels' is ~88% accurate and useless."""
        _, evaluation, _, _ = fitted
        payload = evaluation.as_dict()
        assert "accuracy" not in payload["classification_at_threshold"]
        assert "accuracy" in payload["classification_at_threshold"]["note"].lower()

    def test_threshold_is_the_base_rate_not_a_tuned_value(self, fitted):
        _, evaluation, train, _ = fitted
        assert abs(evaluation.threshold - float(train["cancelled"].mean())) < 1e-9

    def test_calibration_error_is_population_weighted(self, fitted):
        """Regression test for a metric that was wrong.

        An unweighted mean over equal-width bins let a bin holding one booking
        count as much as one holding 344, reporting 9.13pp when the
        population-weighted figure was 2.03pp. Same family as the pooled-rate
        error recorded in PART L-14.
        """
        _, evaluation, _, _ = fitted
        bins = evaluation.calibration
        assert len(bins) >= 4, "too few calibration bins to test the weighting"

        unweighted = float(np.mean([abs(b["gap_pp"]) for b in bins]))
        weighted = 100.0 * evaluation.calibration_error
        total = sum(b["bookings"] for b in bins)
        expected = sum(abs(b["gap_pp"]) * b["bookings"] for b in bins) / total
        assert abs(weighted - expected) < 0.01, "weighting is not by bin population"

        counts = [b["bookings"] for b in bins]
        if max(counts) > 5 * min(counts):
            assert abs(unweighted - weighted) > 0.5, (
                "bin populations are very uneven yet the weighted and unweighted "
                "errors agree, so the weighting is not doing anything"
            )

    def test_the_model_is_calibrated_where_the_data_is(self, fitted):
        _, evaluation, _, _ = fitted
        assert 100.0 * evaluation.calibration_error < 5.0, (
            f"weighted calibration error "
            f"{100 * evaluation.calibration_error:.2f}pp is too large for the "
            "probabilities to be acted on"
        )


class TestGroundTruthRecovery:
    """The generator's mechanism is known, so recovery is checkable."""

    def test_lead_time_effect_points_the_right_way(self, fitted):
        model, _, _, _ = fitted
        report = cx.validate_against_planted(model)
        assert report["lead_time"]["expected"] == "increasing"
        assert report["lead_time"]["recovered"] is True, (
            "the planted mechanism multiplies cancellation by "
            "(1 + 0.55*tanh((lead-10)/14)), which increases with lead time"
        )

    def test_channel_ordering_matches_the_planted_rates(self, fitted):
        model, _, _, _ = fitted
        report = cx.validate_against_planted(model)
        ranking = report["channel_ranking"]
        assert len(ranking["planted"]) == 8
        assert ranking["spearman"] > 0.70, (
            f"channel ordering correlates {ranking['spearman']} with the planted "
            f"rates; recovered {ranking['recovered']} against planted "
            f"{ranking['planted']}"
        )

    def test_the_lowest_risk_channels_are_recovered(self, fitted):
        """CORP (0.055) and DIRECT (0.085) are planted lowest by some margin."""
        model, _, _, _ = fitted
        report = cx.validate_against_planted(model)
        recovered = report["channel_ranking"]["recovered"]
        assert set(recovered[-3:]) & {"CORP", "DIRECT"}, (
            f"corporate and direct should rank among the least likely to "
            f"cancel; got {recovered}"
        )

    def test_temporal_drift_is_measured_and_published(self, raw):
        """The reason the split is temporal, stated as a number."""
        drift = cx.temporal_drift(raw)
        assert len(drift["quarters"]) >= 4
        assert drift["change_pp"] < 0, (
            "cancellation rate should decline across the record; if it no longer "
            "does, the argument for the temporal split needs restating"
        )


class TestNoShowIsUnlearnable:
    """Demonstrated, not asserted."""

    def test_no_show_model_lands_near_a_coin_toss(self, raw):
        report = cx.noshow_is_unlearnable(raw)
        assert report["eligible_bookings"] > 1000
        assert abs(report["auc"] - 0.5) < 0.10, (
            f"no-show AUC {report['auc']}; the generator draws it as a flat "
            "1.4% independent of every feature, so a model that appears to "
            "predict it is fitting noise"
        )
        assert report["verdict"] == "unlearnable"

    def test_the_observed_rate_matches_the_planted_constant(self, raw):
        report = cx.noshow_is_unlearnable(raw)
        assert abs(report["no_show_rate_pct"] - report["generator_constant_pct"]) < 1.0


class TestAucImplementation:
    """The metric itself, since it is hand-rolled."""

    def test_perfect_separation_scores_one(self):
        y = np.array([0, 0, 1, 1], dtype=float)
        assert cx.roc_auc(y, np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)

    def test_inverted_separation_scores_zero(self):
        y = np.array([0, 0, 1, 1], dtype=float)
        assert cx.roc_auc(y, np.array([0.9, 0.8, 0.2, 0.1])) == pytest.approx(0.0)

    def test_all_ties_score_one_half(self):
        """A model outputting one constant has no discrimination. Without tie
        handling this scores 1.0 and the metric is a lie."""
        y = np.array([0, 1, 0, 1], dtype=float)
        assert cx.roc_auc(y, np.full(4, 0.3)) == pytest.approx(0.5)

    def test_matches_a_hand_computed_case(self):
        y = np.array([0, 1, 0, 1], dtype=float)
        scores = np.array([0.1, 0.4, 0.35, 0.8])
        # Pairs (neg, pos): (0.1,0.4)+, (0.1,0.8)+, (0.35,0.4)+, (0.35,0.8)+ = 4/4
        assert cx.roc_auc(y, scores) == pytest.approx(1.0)


class TestArrivalDistribution:
    """The simulator's core, checked against a closed form."""

    def test_equal_probabilities_reproduce_the_binomial(self):
        n, p = 20, 0.85
        pmf = ob.arrival_distribution(np.full(n, p))
        exact = np.array([math.comb(n, k) * p**k * (1 - p)**(n - k)
                          for k in range(n + 1)])
        assert np.abs(pmf - exact).max() < 1e-12

    def test_the_distribution_is_a_distribution(self):
        pmf = ob.arrival_distribution(np.array([0.2, 0.5, 0.9, 0.75]))
        assert pmf.sum() == pytest.approx(1.0)
        assert (pmf >= 0).all()

    def test_it_is_deterministic(self):
        survival = np.array([0.3, 0.6, 0.85])
        assert np.array_equal(ob.arrival_distribution(survival),
                              ob.arrival_distribution(survival))

    def test_heterogeneity_narrows_the_distribution(self):
        """A Poisson-binomial is tighter than the binomial with the same mean.

        This matters because it means per-booking probabilities justify more
        aggressive overbooking for reasons that are partly mathematical rather
        than predictive, which the module publishes rather than claims as
        model quality.
        """
        mean = 0.8
        flat = ob.arrival_distribution(np.full(10, mean))
        spread = ob.arrival_distribution(
            np.array([0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.92, 0.95, 0.98])
        )
        assert abs(ob._sd(spread)) < abs(ob._sd(flat))


class TestOverbookingRefusesToInventACost:
    """The number this module must not produce."""

    def test_the_published_summary_names_no_recommended_level(self):
        summary = ob.summary()
        assert "recommended_overbook" not in summary
        assert "recommended_overbook" not in summary["example"]
        assert summary["what_is_missing"]["cost_of_walking_a_guest"]

    def test_recommend_requires_a_cost_ratio(self):
        """No default. A default would be an invented cost."""
        import inspect
        signature = inspect.signature(ob.recommend)
        assert signature.parameters["cost_ratio"].default is inspect.Parameter.empty

    def test_a_recommendation_carries_its_caveat(self):
        outcomes = ob.simulate(29, np.full(28, 0.846))
        result = ob.recommend(outcomes, cost_ratio=10.0)
        assert result["recommended_overbook"] is not None
        assert "cost ratio supplied" in result["caveat"]

    def test_the_recommendation_moves_with_the_cost_ratio(self):
        """If it did not, the ratio would be decorative."""
        outcomes = ob.simulate(29, np.full(28, 0.846))
        cheap = ob.recommend(outcomes, 1.0)["recommended_overbook"]
        dear = ob.recommend(outcomes, 50.0)["recommended_overbook"]
        assert cheap > dear, (
            "a more expensive walk must justify less overbooking; got "
            f"{cheap} at ratio 1 and {dear} at ratio 50"
        )

    def test_a_boundary_optimum_is_flagged_as_one(self):
        """On an undersold date every level is walk-free, so the optimum sits at
        the edge of the search. Reporting that as a recommendation would dress
        'we stopped looking' up as an answer."""
        outcomes = ob.simulate(60, np.full(10, 0.85), max_overbook=3)
        result = ob.recommend(outcomes, cost_ratio=5.0)
        assert result["recommendation_at_search_boundary"] is True
        assert result["boundary_note"]


@pytest.fixture(scope="module")
def outcomes():
    """A book that genuinely reaches capacity, so the trade-off is live."""
    return ob.simulate(29, np.full(28, 0.846), max_overbook=8)


class TestOverbookingArithmetic:
    """Properties that must hold whatever the data says."""

    def test_walk_risk_rises_with_the_level(self, outcomes):
        risks = [level.p_any_walk for level in outcomes]
        assert risks == sorted(risks)
        assert risks[-1] > risks[0], "walk risk never rose; the model is inert"

    def test_empty_rooms_fall_with_the_level(self, outcomes):
        empty = [level.expected_empty for level in outcomes]
        assert empty == sorted(empty, reverse=True)

    def test_breakeven_ratio_declines(self, outcomes):
        """Each extra room overbooked buys less and risks more, so the ratio at
        which it pays must fall."""
        ratios = [level.breakeven_cost_ratio for level in outcomes
                  if level.breakeven_cost_ratio is not None]
        assert len(ratios) >= 3, "too few breakeven points to test the trend"
        assert ratios == sorted(ratios, reverse=True)

    def test_expected_arrivals_never_exceed_the_book(self, outcomes):
        for level in outcomes:
            assert level.expected_arrivals <= level.accepted

    def test_zero_capacity_produces_nothing_rather_than_dividing_by_it(self):
        assert ob.simulate(0, np.full(5, 0.8)) == []


class TestWashMeasurement:
    def test_measured_wash_matches_the_funnel(self):
        wash = ob.measured_wash_rate()
        assert wash["bookings"] > 5000
        assert 10.0 < wash["wash_rate_pct"] < 20.0
        assert wash["survival_rate_pct"] == pytest.approx(
            100 - wash["wash_rate_pct"], abs=0.01)

    def test_channel_wash_rates_are_populated_and_ordered(self):
        rows = ob.measured_wash_rate()["by_channel"]
        assert len(rows) >= 6, "channel breakdown is empty or truncated"
        rates = [row["wash_rate_pct"] for row in rows]
        assert rates == sorted(rates, reverse=True)

    def test_the_example_date_is_one_where_overbooking_binds(self):
        """Most dates here are undersold; on those the simulator shows nothing.
        The published example must be a date where the trade-off is real."""
        tightest = ob.tightest_stay_date()
        assert tightest["on_books"] / tightest["capacity"] > 0.8
