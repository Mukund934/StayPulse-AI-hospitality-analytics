"""Scenario engine tests.

The engine makes no predictive claim, so there is no accuracy to measure. What
there is instead is arithmetic that must be exact, and a labelling discipline
that must hold — because the way a what-if tool goes wrong is not by computing
the wrong number, it is by letting that number be read as a projection.

So the tests here fall into three groups:

  EXACTNESS      The identity must hold in the result, and the decomposition must
                 sum to the movement with a residual of zero. Not "small" — zero.
  COMPOSITION    Two levers together are multiplicative, not additive. A naive
                 implementation adds them and is wrong by the interaction term.
  HONESTY        Every result is labelled a scenario, states what it held fixed,
                 and contains no forecast vocabulary.

Run:  python -m pytest tests/test_scenario.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse.analytics import scenario as sc  # noqa: E402


@pytest.fixture(scope="module")
def position() -> sc.Position:
    return sc.baseline()


class TestBaselineIsReal:
    def test_the_baseline_comes_from_the_warehouse(self, position):
        assert position.rooms_available > 10_000
        assert 0 < position.rooms_sold < position.rooms_available
        assert position.revenue_inr > 0

    def test_the_identity_holds_in_the_baseline(self, position):
        assert position.adr * position.occupancy == pytest.approx(
            position.revpar, rel=1e-12)

    def test_net_revenue_deducts_commission_and_its_tax(self, position):
        expected = (position.revenue_inr
                    - position.commission_inr * sc.GST_ON_COMMISSION)
        assert position.net_revenue_inr == pytest.approx(expected, rel=1e-12)
        assert position.net_revenue_inr < position.revenue_inr


class TestExactness:
    """The whole value of a scenario is that it is exact."""

    @pytest.mark.parametrize("levers", [
        {"occupancy_pp": 5.0},
        {"adr_pct": 5.0},
        {"occupancy_pp": -3.0, "adr_pct": 7.0},
        {"capacity_units_pct": -5.0},
    ])
    def test_the_identity_holds_in_every_result(self, position, levers):
        result = sc.apply_levers(position, **levers).result
        assert result.adr * result.occupancy == pytest.approx(
            result.revpar, rel=1e-9)

    @pytest.mark.parametrize("levers", [
        {"occupancy_pp": 5.0},
        {"adr_pct": 5.0},
        {"occupancy_pp": 5.0, "adr_pct": 5.0},
        {"occupancy_pp": -8.0, "adr_pct": -4.0},
    ])
    def test_the_decomposition_leaves_no_residual(self, position, levers):
        """Not 'small'. Zero. The Shapley split is exact by construction and a
        residual means the arithmetic has drifted."""
        scenario = sc.apply_levers(position, **levers)
        decomposition = scenario.decomposition()
        assert decomposition["residual_inr"] == pytest.approx(0.0, abs=1e-9)
        assert (decomposition["occupancy_contribution_inr"]
                + decomposition["rate_contribution_inr"]) == pytest.approx(
            scenario.revpar_change, abs=1e-3)

    def test_a_null_scenario_changes_nothing(self, position):
        """Applying no lever must be a no-op, not a rounding drift."""
        scenario = sc.apply_levers(position)
        assert scenario.revpar_change == pytest.approx(0.0, abs=1e-9)
        assert scenario.result.rooms_sold == position.rooms_sold
        assert scenario.result.rooms_available == position.rooms_available


class TestComposition:
    """Two levers are multiplicative. Adding them is the classic error."""

    def test_combined_levers_are_not_the_sum_of_separate_ones(self, position):
        occupancy_only = sc.apply_levers(position, occupancy_pp=5.0).revpar_change
        rate_only = sc.apply_levers(position, adr_pct=5.0).revpar_change
        both = sc.apply_levers(position, occupancy_pp=5.0, adr_pct=5.0).revpar_change

        assert both > occupancy_only + rate_only, (
            "the interaction term has gone missing; RevPAR = ADR x Occupancy is "
            "multiplicative, so moving both levers upward gains more than the "
            "sum of the parts"
        )
        interaction = both - (occupancy_only + rate_only)
        assert interaction > 1.0, f"interaction term implausibly small: {interaction}"

    def test_the_interaction_is_split_evenly_not_dumped_on_one_lever(self, position):
        """Shapley: each contribution uses the MEAN of before and after, so both
        grow relative to their solo values rather than one absorbing the lot."""
        solo_occupancy = sc.apply_levers(position, occupancy_pp=5.0).revpar_change
        solo_rate = sc.apply_levers(position, adr_pct=5.0).revpar_change

        both = sc.apply_levers(position, occupancy_pp=5.0, adr_pct=5.0)
        decomposition = both.decomposition()

        assert decomposition["occupancy_contribution_inr"] > solo_occupancy
        assert decomposition["rate_contribution_inr"] > solo_rate

    def test_the_split_matches_the_root_cause_convention(self, position):
        """A scenario decomposition that attributed the interaction differently
        from `rootcause` would let the two disagree about the same movement."""
        scenario = sc.apply_levers(position, occupancy_pp=4.0, adr_pct=6.0)
        before, after = scenario.baseline, scenario.result

        expected_occupancy = (
            (after.occupancy - before.occupancy) * (before.adr + after.adr) / 2.0
        )
        expected_rate = (
            (after.adr - before.adr) * (before.occupancy + after.occupancy) / 2.0
        )
        # Tolerance matches the payload's 4-decimal rounding, not the underlying
        # precision. Exactness is proven by the zero-residual test above; this
        # one checks that the CONVENTION is Shapley and not something else.
        decomposition = scenario.decomposition()
        assert decomposition["occupancy_contribution_inr"] == pytest.approx(
            expected_occupancy, abs=1e-3)
        assert decomposition["rate_contribution_inr"] == pytest.approx(
            expected_rate, abs=1e-3)


class TestDirectionAndBounds:
    def test_raising_occupancy_raises_revpar(self, position):
        assert sc.apply_levers(position, occupancy_pp=5.0).revpar_change > 0

    def test_lowering_rate_lowers_revpar(self, position):
        assert sc.apply_levers(position, adr_pct=-5.0).revpar_change < 0

    def test_occupancy_cannot_exceed_one_hundred_percent(self, position):
        result = sc.apply_levers(position, occupancy_pp=95.0).result
        assert result.occupancy <= 1.0
        assert result.rooms_sold <= result.rooms_available

    def test_occupancy_cannot_go_negative(self, position):
        result = sc.apply_levers(position, occupancy_pp=-95.0).result
        assert result.occupancy >= 0.0
        assert result.rooms_sold >= 0

    def test_removing_capacity_holds_rooms_sold_until_it_binds(self, position):
        """Taking a unit out of service does not remove demand. At 76% occupancy
        there is slack, so sold nights should be unchanged."""
        result = sc.apply_levers(position, capacity_units_pct=-5.0).result
        assert result.rooms_available < position.rooms_available
        assert result.rooms_sold == position.rooms_sold
        assert result.occupancy > position.occupancy

    def test_capacity_removal_is_capped_by_what_remains(self, position):
        """Cut capacity below the book and sold nights must fall with it,
        rather than the arithmetic selling rooms that no longer exist."""
        result = sc.apply_levers(position, capacity_units_pct=-90.0).result
        assert result.rooms_sold <= result.rooms_available


class TestSensitivityTable:
    def test_the_sweep_is_monotone_in_each_lever(self, position):
        table = sc.sensitivity(position)
        for key, lever in (("occupancy_pp", "lever_pp"), ("adr_pct", "lever_pct")):
            rows = sorted(table[key], key=lambda r: r[lever])
            changes = [row["revpar_change_inr"] for row in rows]
            assert changes == sorted(changes), f"{key} sweep is not monotone"

    def test_the_sweep_crosses_zero(self, position):
        table = sc.sensitivity(position)
        changes = [row["revpar_change_inr"] for row in table["occupancy_pp"]]
        assert min(changes) < 0 < max(changes)


class TestChannelMix:
    """The one lever with measured economics behind it."""

    def test_channel_economics_are_measured_not_assumed(self):
        rows = sc.channel_economics()
        assert len(rows) >= 6, "channel table is empty or truncated"
        for row in rows:
            assert row["nights"] > 0
            assert row["adr_inr"] > 0
            assert row["net_per_night_inr"] <= row["adr_inr"]

    def test_ota_channels_carry_commission_and_direct_does_not(self):
        economics = {row["channel"]: row for row in sc.channel_economics()}
        assert economics["MMT"]["commission_per_night_inr"] > 0
        assert economics["DIRECT"]["commission_per_night_inr"] == 0, (
            "the direct channel should carry no commission; if it does, the "
            "mix scenario is pricing something that is not there"
        )

    def test_moving_ota_nights_direct_improves_net_revenue(self):
        result = sc.shift_channel_mix("MMT", "DIRECT", 25.0)
        assert result["lever"]["nights_moved"] > 0
        assert result["change"]["net_revenue_inr"] > 0

    def test_nights_moved_scale_with_the_share(self):
        quarter = sc.shift_channel_mix("MMT", "DIRECT", 25.0)
        half = sc.shift_channel_mix("MMT", "DIRECT", 50.0)
        assert half["lever"]["nights_moved"] == pytest.approx(
            2 * quarter["lever"]["nights_moved"], rel=0.02)

    def test_the_transferability_assumption_is_stated(self):
        """The arithmetic is exact; its premise is not evidence."""
        result = sc.shift_channel_mix("MMT", "DIRECT", 25.0)
        assumptions = " ".join(result["assumptions_held_constant"]).lower()
        assert "demand transfers" in assumptions
        assert "acquisition cost" in assumptions

    def test_an_unknown_channel_is_rejected(self):
        with pytest.raises(KeyError):
            sc.shift_channel_mix("MMT", "NOT-A-CHANNEL", 10.0)


class TestItNeverClaimsToBeAForecast:
    """The failure mode of a what-if tool is not a wrong number. It is a right
    number read as a projection."""

    def test_every_payload_declares_itself_a_scenario(self, position):
        payloads = [
            sc.apply_levers(position, occupancy_pp=5.0).as_dict(),
            sc.sensitivity(position),
            sc.shift_channel_mix("MMT", "DIRECT", 10.0),
            sc.summary(),
        ]
        for payload in payloads:
            assert payload["result_type"] == "scenario"
            assert payload["is_forecast"] is False

    def test_no_payload_uses_forecast_vocabulary(self, position):
        """Words that would invite a reader to treat arithmetic as prediction."""
        banned = ("we forecast", "projected revenue", "will increase",
                  "will grow", "expected uplift", "predicted revenue",
                  "revenue uplift")
        blob = str(sc.summary()).lower()
        assert blob, "empty summary; this scan would be vacuous"
        for phrase in banned:
            assert phrase not in blob, f"summary uses forecast language: {phrase!r}"

    def test_every_moved_lever_states_what_it_held_constant(self, position):
        for levers in ({"occupancy_pp": 5.0}, {"adr_pct": 5.0},
                       {"capacity_units_pct": -5.0}):
            scenario = sc.apply_levers(position, **levers)
            assert scenario.assumptions, f"{levers} stated no assumptions"
            assert all(a.strip() for a in scenario.assumptions)

    def test_the_absence_of_elasticity_is_disclosed(self, position):
        assumptions = " ".join(
            sc.apply_levers(position, adr_pct=5.0).assumptions).lower()
        assert "elasticity" in assumptions, (
            "a rate scenario that does not disclose the missing elasticity "
            "invites the reader to treat the revenue as capturable"
        )

    def test_the_summary_says_what_it_cannot_do(self):
        limits = " ".join(sc.summary()["what_this_cannot_do"]).lower()
        assert "elasticity" in limits
        assert "capturable" in limits

    def test_the_separation_from_forecasting_is_explicit(self):
        note = sc.summary()["separation_note"].lower()
        assert "not a forecast" in note
