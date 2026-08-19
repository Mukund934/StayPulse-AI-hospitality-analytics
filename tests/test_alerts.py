"""Alert Center and Opportunity Radar tests.

The important assertions here are about what the module refuses to do.

An alert queue that merges four incommensurable feeds is under constant pressure
to invent a shared severity score, because a single sortable number is what makes
a queue look finished. This module does not have one, and the tests below fail if
anyone adds it. That is the point of the feature, not a limitation of it.

The second theme is vacuity. A queue can be empty for good reasons, so several
tests assert a populated feed BEFORE looping over it -- otherwise "every alert
declares its units" passes loudest when no alert exists at all.

Run:  python -m pytest tests/test_alerts.py -v
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.analytics import alerts as al  # noqa: E402
from staypulse.analytics import revenue as rv  # noqa: E402


@pytest.fixture(scope="module")
def horizon() -> dt.date:
    return rv.data_horizon()


@pytest.fixture(scope="module")
def centre(horizon: dt.date) -> dict:
    return al.alert_center(horizon)


@pytest.fixture(scope="module")
def radar(horizon: dt.date) -> dict:
    """An as-of date where dates ahead of pace actually exist.

    At the inventory horizon the forward window sits inside a holiday and nothing
    is ahead of curve, so a radar fixture anchored there would make every
    assertion below vacuous.
    """
    return al.opportunity_radar(horizon - dt.timedelta(days=40))


class TestNoInventedSeverity:
    """The queue must not claim a comparability it cannot compute."""

    def test_no_alert_carries_a_cross_source_severity_score(self, centre):
        alerts = centre["alerts"]
        assert alerts, "empty queue; the loop below would prove nothing"
        banned = {"severity", "severity_score", "priority", "score", "rank", "level"}
        for alert in alerts:
            leaked = banned & set(alert)
            assert not leaked, (
                f"{alert['source']} alert exposes {leaked}: a severity comparable "
                "across four incommensurable feeds cannot be computed here"
            )

    def test_every_measure_names_its_own_units_and_disclaims_comparability(self, centre):
        alerts = centre["alerts"]
        assert alerts
        for alert in alerts:
            measure = alert["measure"]
            assert measure["name"], f"{alert['source']} measure has no name"
            assert measure["units"], f"{alert['source']} measure has no units"
            assert measure["comparable_across_sources"] is False

    def test_the_payload_says_why_there_is_no_shared_scale(self, centre):
        note = centre["severity_note"].lower()
        assert "no severity" in note
        assert "incommensurable" in note

    def test_units_genuinely_differ_between_sources(self, centre):
        """If every feed reported the same units, the disclaimer would be theatre."""
        units = {a["source"]: a["measure"]["units"] for a in centre["alerts"]}
        assert len(units) >= 3, f"only {len(units)} sources present: {units}"
        assert len(set(units.values())) >= 2, (
            f"all sources report the same units {set(units.values())}, so the "
            "non-comparability claim is not being exercised"
        )


class TestFeedsAreConnected:
    """The feature is that four feeds arrive in one queue, so check all four do."""

    def test_every_source_is_represented(self, centre):
        present = set(centre["by_source"])
        expected = {al.SOURCE_PACE, al.SOURCE_ANOMALY,
                    al.SOURCE_DATA_QUALITY, al.SOURCE_SERVICE_SLA}
        assert expected <= present, (
            f"missing {expected - present}; a feed contributing zero alerts while "
            "the header advertises it is exactly the failure this test exists for"
        )

    def test_counts_add_up(self, centre):
        assert sum(centre["by_source"].values()) == centre["total"]
        assert sum(centre["by_actionability"].values()) == centre["total"]
        assert len(centre["alerts"]) == centre["total"]

    def test_every_advertised_source_is_described(self, centre):
        for source in centre["by_source"]:
            assert centre["sources"].get(source), f"{source} has no description"


class TestActionability:
    """The one axis that IS comparable across sources."""

    def test_each_source_lands_in_the_right_band(self, centre):
        expected = {
            al.SOURCE_PACE: al.ACT_NOW,
            al.SOURCE_ANOMALY: al.INVESTIGATE,
            al.SOURCE_DATA_QUALITY: al.STANDING,
            al.SOURCE_SERVICE_SLA: al.STANDING,
        }
        assert centre["alerts"]
        for alert in centre["alerts"]:
            assert alert["actionability"] == expected[alert["source"]]

    def test_only_future_dated_alerts_carry_a_deadline(self, centre, horizon):
        assert centre["alerts"]
        for alert in centre["alerts"]:
            if alert["actionability"] == al.ACT_NOW:
                assert alert["days_to_act"] is not None
                assert dt.date.fromisoformat(alert["raised_for"]) > horizon
            else:
                assert alert["days_to_act"] is None

    def test_investigate_alerts_are_about_dates_that_have_happened(self, centre, horizon):
        past = [a for a in centre["alerts"]
                if a["actionability"] == al.INVESTIGATE]
        assert past, "no investigate-band alerts; this assertion would be vacuous"
        for alert in past:
            assert dt.date.fromisoformat(alert["raised_for"]) <= horizon

    def test_standing_alerts_have_no_date(self, centre):
        standing = [a for a in centre["alerts"]
                    if a["actionability"] == al.STANDING]
        assert standing
        for alert in standing:
            assert alert["raised_for"] is None


class TestRanking:
    """Ordering has to be reproducible and anchored to the data."""

    def test_queue_is_ordered_by_actionability_band(self, centre):
        bands = [al.ACTIONABILITY_ORDER.index(a["actionability"])
                 for a in centre["alerts"]]
        assert bands == sorted(bands), "the queue is not grouped by actionability"

    def test_soonest_deadline_leads_the_act_now_band(self, centre):
        deadlines = [a["days_to_act"] for a in centre["alerts"]
                     if a["actionability"] == al.ACT_NOW]
        assert len(deadlines) >= 2, "too few dated alerts to test the ordering"
        assert deadlines == sorted(deadlines)

    def test_ranking_does_not_depend_on_the_wall_clock(self, horizon):
        """Recency is measured against the as-of date.

        This dataset ends at a fixed horizon, so ordering by `date.today()` would
        drift further from the data every day the real calendar moves.
        """
        import ast
        import inspect
        import textwrap

        # Parsed, not grepped. The docstring of `_rank` mentions the wall-clock
        # call to explain why it is not used, and a substring search flags that
        # as a violation -- a test that fails on its own explanation.
        tree = ast.parse(textwrap.dedent(inspect.getsource(al._rank)))
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not ({"today", "now", "utcnow"} & called), (
            f"_rank reads the wall clock ({called}); it must order against the "
            "as-of date"
        )
        assert al.alert_center(horizon)["alerts"] == \
               al.alert_center(horizon)["alerts"]


class TestCalendarContext:
    """F-101 pairing: a shortfall and its explanation belong together."""

    def test_holiday_adjacent_alerts_carry_their_context(self, centre):
        dated = [a for a in centre["alerts"] if a["raised_for"]]
        assert dated
        contexts = [a for a in dated if a["calendar_context"]]
        assert contexts, (
            "no alert carries calendar context; either the window contains no "
            "holiday or the F-101 join has broken"
        )
        for alert in contexts:
            assert alert["calendar_context"].strip()

    def test_context_matches_the_calendar_dimension(self, centre):
        """Read back independently rather than trusting the module's own join."""
        contexts = {a["raised_for"]: a["calendar_context"]
                    for a in centre["alerts"]
                    if a["raised_for"] and a["calendar_context"]}
        assert contexts
        rows = db.fetch_all(
            "SELECT full_date FROM mart.dim_date "
            "WHERE full_date = ANY(:d) AND is_holiday_adjacent",
            d=[dt.date.fromisoformat(k) for k in contexts],
        )
        assert len(rows) == len(contexts), (
            "an alert claims holiday context for a date the calendar does not "
            "mark holiday-adjacent"
        )

    def test_pace_alerts_on_holiday_dates_are_qualified_not_suppressed(self, centre):
        """The pace benchmark is holiday-blind, so a measured holiday suppression
        reaches this queue as a shortfall.

        The alert must survive with a qualifier attached. Suppressing it would
        hide genuine weakness on dates where occupancy is already fragile.
        """
        holiday_pace = [a for a in centre["alerts"]
                        if a["source"] == al.SOURCE_PACE and a["calendar_context"]]
        assert holiday_pace, "no holiday-adjacent pace alert to check"
        for alert in holiday_pace:
            assert alert["qualifier"], "holiday-adjacent pace alert is unqualified"
            assert "holiday-blind" in alert["qualifier"]

    def test_the_bias_is_published_rather_than_hidden(self, centre):
        bias = centre["known_bias"]
        assert bias["feed"] == al.SOURCE_PACE
        for field in ("mechanism", "magnitude", "handling"):
            assert bias[field].strip(), f"known_bias.{field} is empty"
        assert "not suppressed" in bias["handling"].lower()

    def test_no_over_representation_ratio_is_claimed(self):
        """The pooled 1.9x figure was Simpson's paradox: per-origin base rates
        run 0% to 100%, one origin with a 100% base rate supplied most of the
        alerts, and excluding it leaves no effect. The mechanism is published;
        the magnitude is explicitly not."""
        assert "not established" in al.HOLIDAY_PACE_QUALIFIER.lower() or                "no over-representation ratio" in al.HOLIDAY_PACE_QUALIFIER.lower()
        for banned in ("1.9x", "1.89x", "73.7% of alerts"):
            assert banned not in al.HOLIDAY_PACE_QUALIFIER


class TestServiceLevelFeed:
    """The threshold that was wrong the first time."""

    def test_the_sla_feed_is_not_silently_empty(self, centre):
        """An absolute 25% cut matched nothing on this data: the distribution
        tops out at 22.5%. A feed advertised in the header and contributing zero
        alerts is worse than no feed."""
        assert centre["by_source"].get(al.SOURCE_SERVICE_SLA, 0) > 0

    def test_flagged_cells_clear_both_gates(self, centre):
        flagged = [a for a in centre["alerts"]
                   if a["source"] == al.SOURCE_SERVICE_SLA]
        assert flagged
        rows = db.fetch_all(
            """
            SELECT round(100.0*count(*) FILTER (WHERE is_sla_breached)/count(*), 1) pct
            FROM mart.v_service_kpi
            WHERE resolution_minutes IS NOT NULL
            GROUP BY property_code, day_part_ist
            HAVING count(*) >= :minreq
            """,
            minreq=al.MIN_SLA_REQUESTS,
        )
        rates = sorted(float(r["pct"]) for r in rows)
        index = (len(rates) - 1) * al.SLA_PEER_PERCENTILE / 100.0
        lower = int(index)
        upper = min(lower + 1, len(rates) - 1)
        peer = rates[lower] + (rates[upper] - rates[lower]) * (index - lower)
        for alert in flagged:
            assert alert["measure"]["value"] >= peer

    def test_the_comparison_basis_is_disclosed(self, centre):
        """No target breach rate exists in this warehouse, so the output must not
        imply the comparison is against one."""
        note = centre["sources"][al.SOURCE_SERVICE_SLA].lower()
        assert "no absolute target" in note or "against peers" in note


class TestOpportunityRadar:
    """F-402: the upside half, under the same rules."""

    def test_radar_surfaces_dates_ahead_of_curve(self, radar):
        assert radar["total"] > 0, (
            "no opportunities at an as-of date chosen because it has them; the "
            "assertions below would be vacuous"
        )
        for row in radar["opportunities"]:
            assert row["kind"] == "ahead_of_pace"
            assert row["measure"]["value"] > 0
            assert row["days_to_act"] is not None

    def test_radar_never_recommends_a_price(self, radar):
        """The standing prohibition. There is no elasticity and no competitor
        rate here, so a rate recommendation would be an opinion with a number
        attached. This test must not be weakened."""
        body = str(radar).lower()
        for phrase in ("increase rate", "lower rate", "reprice", "raise price",
                       "recommended rate", "set the rate", "raise the rate"):
            assert phrase not in body
        assert "no signal names a price" in radar["note"].lower()

    def test_radar_and_alert_center_disagree_about_direction(self, horizon):
        """A date cannot be both behind and ahead of its own curve."""
        as_of = horizon - dt.timedelta(days=40)
        behind = {a["subject"] for a in al.alert_center(as_of)["alerts"]
                  if a["source"] == al.SOURCE_PACE}
        ahead = {o["subject"] for o in al.opportunity_radar(as_of)["opportunities"]}
        assert ahead, "no opportunities; the intersection below is vacuous"
        assert not (behind & ahead)


class TestSummary:
    def test_summary_counts_match_the_full_payload(self, horizon):
        summary = al.summary(horizon)
        centre = al.alert_center(horizon)
        assert summary["alerts_total"] == centre["total"]
        assert summary["alerts_by_source"] == centre["by_source"]

    def test_soonest_deadline_is_the_minimum_across_dated_alerts(self, horizon):
        summary = al.summary(horizon)
        centre = al.alert_center(horizon)
        deadlines = [a["days_to_act"] for a in centre["alerts"]
                     if a["days_to_act"] is not None]
        assert deadlines
        assert summary["soonest_deadline_days"] == min(deadlines)
