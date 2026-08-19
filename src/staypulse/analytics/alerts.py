"""Alert Center and Opportunity Radar: four existing feeds, one queue.

WHAT THIS ADDS, GIVEN THAT ALL FOUR FEEDS ALREADY EXISTED

Anomalies, data-quality failures, SLA breaches and pace need-dates were each
reachable and each in its own shape, so anyone wanting to know "what needs
attention" had to visit four places and hold four mental models. This puts them in
one queue with one envelope.

It does NOT put them on one scale, and that distinction is the whole design.


THE THING MOST ALERT CENTRES FAKE

The tempting move is a severity number -- critical/high/medium, or 1 to 5 --
applied across every source. It cannot be computed here. A robust z of 4.1 on
ADR, a data-quality rule failing on 3% of rows, a 22% SLA breach rate and a stay
date six room-nights behind pace are four incommensurable quantities. Mapping them
onto a shared 1-5 scale requires exchange rates nobody has measured, and the
result looks authoritative precisely because the arbitrariness is hidden inside an
integer.

So every alert carries its own feed's measure, in that feed's units, with the
units named. `measure_value` is never compared across sources, and a test asserts
no cross-source severity field exists.


WHAT *IS* COMPARABLE, AND IT IS THE USEFUL PART

Not severity -- ACTIONABILITY. What an operator can still do about a thing is
genuinely comparable across sources, and it is derivable rather than invented:

  ACT_NOW      A future stay date. The book can still move, so the alert has a
               deadline: `days_to_act` counts down to arrival.
  INVESTIGATE  A date that has already happened. Nothing can change it; the only
               thing available is an explanation.
  STANDING     A condition with no single date -- a failing rule, a chronically
               breaching property/day-part. True until fixed.

That ordering is what the queue sorts by, because it maps to what a person does
next rather than to how alarming a number looks.


CALENDAR CONTEXT

Date-scoped alerts carry their holiday context from F-101, which is the pairing
the dependency graph anticipated. "Six room-nights behind" and "six room-nights
behind, and it is the day before Diwali, which measured -10.5pp last year" are
different alerts, and only one of them tells an operator whether to act.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from staypulse import db
from staypulse.analytics import anomaly as an
from staypulse.analytics import revenue as rv

# --- actionability classes, in the order the queue presents them ------------
ACT_NOW = "act_now"
INVESTIGATE = "investigate"
STANDING = "standing"

ACTIONABILITY_ORDER = (ACT_NOW, INVESTIGATE, STANDING)

# --- sources ---------------------------------------------------------------
SOURCE_PACE = "pace"
SOURCE_ANOMALY = "anomaly"
SOURCE_DATA_QUALITY = "data_quality"
SOURCE_SERVICE_SLA = "service_sla"

# Trailing days of history the anomaly feed scans when raising alerts. Anomalies
# older than this are history rather than an alert queue; the full record stays in
# reports/anomalies.md.
ANOMALY_LOOKBACK_DAYS = 45

# An SLA cell needs this many requests before a breach rate is worth raising. The
# service KPI view already applies a floor of 5; this is stricter because a queue
# is a call to action and 6-of-7 is not a pattern.
MIN_SLA_REQUESTS = 15

# HOW A BREACH RATE IS JUDGED, AND WHY THERE IS NO ABSOLUTE THRESHOLD.
#
# The warehouse defines `sla_minutes` per request type and derives
# `is_sla_breached` from it, but it defines no acceptable breach RATE anywhere --
# no "95% within SLA" target exists in the metric registry, the DQ rules or the
# generator. So any absolute cut is invented.
#
# The first version of this module used 25%, with a docstring asserting that sat
# above the bulk of the distribution. Measured, the distribution runs 6.5% to
# 22.5% with a median of 16.7 across 11 qualifying cells: 25% would have matched
# NOTHING, and the Alert Center would have advertised four feeds while one
# silently contributed zero. The claim was wrong and the threshold was worse.
#
# So a cell is judged against its peers, using the dual gate this codebase
# already applies in `anomaly.detect` and `revenue.PaceRow.status`:
#
#   1. STATISTICAL -- breach rate at or above the p75 of comparable cells.
#   2. MATERIAL    -- at least MIN_SLA_BREACHES breaches in absolute terms, so a
#                     small cell cannot qualify on a high percentage of very
#                     little.
#
# "Bad" here therefore means "worse than most comparable cells", not "worse than
# a contractual target", and the published output says exactly that.
SLA_PEER_PERCENTILE = 75
MIN_SLA_BREACHES = 20


# WHY A HOLIDAY-ADJACENT PACE ALERT IS QUALIFIED, AND THE CLAIM THAT DID NOT SURVIVE.
#
# The pace benchmark is holiday-blind by construction: it compares a stay date
# against the last 8 comparable same-weekday dates, which will not include the
# holiday. F-101 measured real suppression on those dates -- Diwali -10.5pp,
# Christmas -20.4pp -- so part of a shortfall on a holiday-adjacent date is
# plausibly the holiday rather than a demand problem. That much is sound, and it
# is what the qualifier says.
#
# WHAT WAS CLAIMED HERE FIRST, AND WHY IT WAS WITHDRAWN.
#
# An earlier version of this module asserted that behind-pace alerts were 1.9x
# over-represented on holiday-adjacent dates: 73.7% of alerts against a 39.0%
# base rate, pooled over 8 origins. The pooled figures are arithmetically correct
# and the conclusion drawn from them is wrong.
#
# Broken down per origin, the base rate ranges from 0% to 100%. One origin sat
# immediately before Independence Day where EVERY scored date in the window was
# holiday-adjacent; it contributed 11 of the 19 alerts, all holiday-adjacent,
# which is exactly what a 100% base rate produces and is no evidence of anything.
# Another origin ran 71.4% holiday-adjacent dates and raised zero holiday-adjacent
# alerts -- the opposite direction. Excluding the dominant origin the rate is
# 37.5% against a 34.9% base rate: no effect.
#
# This is Simpson's paradox, and it is the third time this project has hit the
# same class of error: PART U.2 records pooling across holidays producing
# multipliers above 1 for holidays that suppress demand, and U.3 records
# pseudo-replication in the confidence caveat. Pooling across units with wildly
# different base rates is unsound here in whichever direction it flatters.
#
# So no over-representation ratio is published. The qualifier states the
# mechanism, which is measured, and not a magnitude, which is not.
HOLIDAY_PACE_QUALIFIER = (
    "This date is holiday-adjacent and the pace benchmark is holiday-blind -- its "
    "baseline is the last 8 comparable weekdays, which exclude the holiday. F-101 "
    "measured real suppression on holiday-adjacent dates, so part of this gap is "
    "plausibly the holiday rather than a demand problem. Not suppressed: a holiday "
    "explains part of a shortfall, not all of it. No over-representation ratio is "
    "quoted -- the pooled figure that suggested one did not survive a per-origin "
    "breakdown."
)


@dataclass
class Alert:
    """One thing that needs attention, in the units its own feed measures in."""

    source: str
    kind: str
    subject: str
    subject_type: str
    actionability: str
    headline: str
    measure_name: str
    measure_value: float
    measure_units: str
    evidence: list[str] = field(default_factory=list)
    suggested_check: str = ""
    raised_for: dt.date | None = None
    days_to_act: int | None = None
    calendar_context: str | None = None
    qualifier: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "kind": self.kind,
            "subject": self.subject,
            "subject_type": self.subject_type,
            "actionability": self.actionability,
            "headline": self.headline,
            "measure": {
                "name": self.measure_name,
                "value": round(self.measure_value, 2),
                "units": self.measure_units,
                "comparable_across_sources": False,
            },
            "evidence": self.evidence,
            "suggested_check": self.suggested_check,
            "raised_for": self.raised_for.isoformat() if self.raised_for else None,
            "days_to_act": self.days_to_act,
            "calendar_context": self.calendar_context,
            "qualifier": self.qualifier,
        }


# ---------------------------------------------------------------------------
def _calendar_context(dates: list[dt.date]) -> dict[dt.date, str]:
    """Holiday context for the dates an alert queue is about.

    One query for the whole queue rather than one per alert. Empty when no date
    in the window is near a holiday, which is the common case and correctly
    produces no context rather than a filler string.
    """
    if not dates:
        return {}
    rows = db.fetch_all(
        """
        SELECT full_date, nearest_holiday, days_to_holiday, is_public_holiday
        FROM mart.dim_date
        WHERE full_date = ANY(:dates)
          AND is_holiday_adjacent
          AND nearest_holiday IS NOT NULL
        """,
        dates=list(dates),
    )
    out: dict[dt.date, str] = {}
    for row in rows:
        offset = int(row["days_to_holiday"])
        name = str(row["nearest_holiday"])
        if row["is_public_holiday"]:
            out[row["full_date"]] = f"{name} (the day itself)"
        elif offset < 0:
            out[row["full_date"]] = f"{-offset} day(s) before {name}"
        else:
            out[row["full_date"]] = f"{offset} day(s) after {name}"
    return out


def _pace_alerts(as_of: dt.date, scored: list[rv.PaceRow]) -> list[Alert]:
    """Future stay dates running behind their own weekday's curve.

    The only feed whose alerts have a deadline, which is why they lead the queue.
    Ranked by absolute room-night shortfall, matching `need_dates` -- a percentage
    ranking would put a 50% gap on a four-night date above an eight-night hole on
    a large one.
    """
    behind = rv.need_dates(as_of, scored=scored)
    return [
        Alert(
            source=SOURCE_PACE,
            kind="behind_pace",
            subject=f"{row.stay_date:%Y-%m-%d} · {row.property_name}",
            subject_type="stay_date",
            actionability=ACT_NOW,
            headline=(
                f"{row.stay_date:%a %d %b} is {abs(row.gap_nights):.0f} room-nights "
                f"behind its usual position with {row.days_out} days to go"
            ),
            measure_name="room-night shortfall against the same-weekday median",
            measure_value=abs(row.gap_nights),
            measure_units="room-nights",
            evidence=[
                f"{row.nights_on_books} nights on the books",
                f"typically {row.expected_nights:.1f} by this point on a "
                f"{row.stay_date:%A} (usual range "
                f"{row.p25_nights:.0f}-{row.p75_nights:.0f})",
                f"baseline from the last {row.support} comparable "
                f"{row.stay_date:%A}s at this property",
                f"confidence {row.confidence}",
            ],
            suggested_check=(
                "Compare channel pickup for this date against the same weekday, "
                "and confirm no rate or availability restriction is suppressing it."
            ),
            raised_for=row.stay_date,
            days_to_act=row.days_out,
        )
        for row in behind
    ]


def _anomaly_alerts(as_of: dt.date) -> list[Alert]:
    """Recent portfolio anomalies, detected rather than re-parsed.

    Calls `anomaly.detect` with the gates that live in that module, so this is not
    a second definition of what counts as an anomaly. The alternative -- scraping
    the generated markdown report, as the /api/anomalies endpoint does -- would
    have made the queue depend on a report's table layout.
    """
    rows = db.fetch_all(
        """
        SELECT stay_date,
               sum(room_revenue_net_inr)                                    AS revenue,
               round(100.0*sum(rooms_sold)/NULLIF(sum(rooms_available),0),2) AS occupancy_pct,
               round(sum(room_revenue_net_inr)/NULLIF(sum(rooms_sold),0),2)  AS adr_inr
        FROM mart.v_daily_kpi
        WHERE stay_date <= :as_of
        GROUP BY 1 ORDER BY 1
        """,
        as_of=as_of,
    )
    if not rows:
        return []

    frame = pd.DataFrame(rows)
    for column in ("revenue", "occupancy_pct", "adr_inr"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    median_revenue = float(frame["revenue"].median())
    gates = {
        "revenue": median_revenue * an.REVENUE_GATE_FRACTION_OF_MEDIAN,
        "occupancy_pct": an.PORTFOLIO_GATES["occupancy_pct"],
        "adr_inr": an.PORTFOLIO_GATES["adr_inr"],
    }
    units = {"revenue": "INR", "occupancy_pct": "percentage points", "adr_inr": "INR"}
    cutoff = as_of - dt.timedelta(days=ANOMALY_LOOKBACK_DAYS)

    out: list[Alert] = []
    for metric, gate in gates.items():
        for found in an.detect(frame, metric=metric, segment="PORTFOLIO",
                               min_abs_change=gate):
            raised = dt.date.fromisoformat(found.date[:10])
            if raised < cutoff:
                continue
            out.append(Alert(
                source=SOURCE_ANOMALY,
                kind=f"{metric}_{found.direction}",
                subject=f"{raised:%Y-%m-%d} · {metric}",
                subject_type="stay_date",
                actionability=INVESTIGATE,
                headline=(
                    f"{metric} on {raised:%a %d %b} came in {found.deviation_pct:+.1f}% "
                    f"against its same-weekday baseline"
                ),
                measure_name="robust z against the trailing same-weekday median",
                measure_value=abs(found.robust_z),
                measure_units="robust standard deviations",
                evidence=[
                    f"actual {found.actual:,.1f} against a baseline of "
                    f"{found.baseline:,.1f}",
                    f"deviation {found.deviation:+,.1f} {units[metric]}",
                    f"materiality gate {gate:,.1f} {units[metric]}",
                    f"confidence {found.confidence}",
                ] + ([f"likely drivers: {'; '.join(found.drivers)}"]
                     if found.drivers else []),
                suggested_check=(
                    "Already realised, so this is an explanation rather than an "
                    "action. Check the property split and whether a known event "
                    "or holiday covers the date."
                ),
                raised_for=raised,
            ))
    return out


def _data_quality_alerts() -> list[Alert]:
    """Rules failing their own threshold on the most recent check.

    Standing rather than dated: a failing rule is true of the dataset until
    someone fixes it, and giving it a stay date would imply a deadline it has not
    got.

    Some of these failures are DELIBERATE -- the generator plants defect classes
    so the checks have something to find -- and the alert says so rather than
    presenting a designed defect as a surprise.
    """
    rows = db.fetch_all(
        """
        SELECT ru.rule_id, ru.dimension, ru.target_table, ru.severity, ru.description,
               res.rows_checked, res.rows_failed, res.failure_pct, ru.threshold_pct,
               res.checked_at
        FROM meta.dq_result res
        JOIN meta.dq_rule ru ON ru.rule_id = res.rule_id
        WHERE res.result_id IN (SELECT max(result_id) FROM meta.dq_result GROUP BY rule_id)
          AND NOT res.passed
        ORDER BY ru.severity, res.failure_pct DESC
        """
    )
    return [
        Alert(
            source=SOURCE_DATA_QUALITY,
            kind=f"rule_failed_{row['severity']}",
            subject=f"{row['rule_id']} · {row['target_table']}",
            subject_type="data_quality_rule",
            actionability=STANDING,
            headline=(
                f"{row['rule_id']} is failing on {float(row['failure_pct']):.2f}% of "
                f"{int(row['rows_checked']):,} rows in {row['target_table']}"
            ),
            measure_name="rows failing the rule",
            measure_value=float(row["failure_pct"]),
            measure_units="percent of rows checked",
            evidence=[
                str(row["description"]),
                f"dimension {row['dimension']}, severity {row['severity']}",
                f"{int(row['rows_failed']):,} of {int(row['rows_checked']):,} rows",
                f"threshold {float(row['threshold_pct']):.2f}%",
                f"last checked {row['checked_at']}",
            ],
            suggested_check=(
                "Some defect classes in this dataset are planted on purpose so the "
                "rules have something to catch. Check reports/analyses.md before "
                "treating this as a regression."
            ),
        )
        for row in rows
    ]


def _sla_alerts() -> list[Alert]:
    """Property and day-part combinations breaching SLA worse than their peers.

    Standing, not dated: this is an aggregate over the whole record, so it
    describes a chronic condition rather than an event. A single bad Tuesday does
    not appear here and should not -- that is what the anomaly feed is for.

    Judged relative to comparable cells rather than against an absolute rate, for
    the reason recorded at SLA_PEER_PERCENTILE: this warehouse defines no
    acceptable breach rate, so an absolute cut would be invented.
    """
    rows = db.fetch_all(
        """
        SELECT property_code, day_part_ist, owning_team,
               count(*)                                                          AS requests,
               count(*) FILTER (WHERE is_sla_breached)                           AS breaches,
               round(100.0*count(*) FILTER (WHERE is_sla_breached)/count(*), 1)  AS breach_pct,
               round(avg(resolution_minutes)::numeric, 0)                        AS avg_tat_min
        FROM mart.v_service_kpi
        WHERE resolution_minutes IS NOT NULL
        GROUP BY 1, 2, 3
        HAVING count(*) >= :minreq
        ORDER BY breach_pct DESC
        """,
        minreq=MIN_SLA_REQUESTS,
    )
    if not rows:
        return []

    rates = sorted(float(r["breach_pct"]) for r in rows)
    index = (len(rates) - 1) * SLA_PEER_PERCENTILE / 100.0
    lower, upper = int(index), min(int(index) + 1, len(rates) - 1)
    peer_threshold = rates[lower] + (rates[upper] - rates[lower]) * (index - lower)

    return [
        Alert(
            source=SOURCE_SERVICE_SLA,
            kind="sla_breach_rate",
            subject=f"{row['property_code']} · {row['day_part_ist']}",
            subject_type="property_day_part",
            actionability=STANDING,
            headline=(
                f"{row['property_code']} breaches SLA on "
                f"{float(row['breach_pct']):.1f}% of {row['day_part_ist']} requests, "
                f"against a peer p{SLA_PEER_PERCENTILE} of {peer_threshold:.1f}%"
            ),
            measure_name="requests breaching SLA",
            measure_value=float(row["breach_pct"]),
            measure_units="percent of requests",
            evidence=[
                f"{int(row['breaches'])} breaches in {int(row['requests'])} requests",
                f"average turnaround {int(row['avg_tat_min'])} minutes",
                f"owning team {row['owning_team']}",
                f"peer p{SLA_PEER_PERCENTILE} across {len(rates)} comparable cells "
                f"is {peer_threshold:.1f}%; range {rates[0]:.1f}-{rates[-1]:.1f}%",
                f"materiality gate {MIN_SLA_BREACHES} breaches",
            ],
            suggested_check=(
                "This warehouse defines no target breach rate, so 'bad' here means "
                "worse than comparable cells, not worse than a contract. "
                "Aggregated over the whole record, so it is staffing or process "
                "rather than one bad shift."
            ),
        )
        for row in rows
        if float(row["breach_pct"]) >= peer_threshold
        and int(row["breaches"]) >= MIN_SLA_BREACHES
    ]


# ---------------------------------------------------------------------------
def _attach_calendar(alerts: list[Alert]) -> None:
    """Give every date-scoped alert its holiday context, in place."""
    dates = [a.raised_for for a in alerts if a.raised_for is not None]
    context = _calendar_context(dates)
    for alert in alerts:
        if alert.raised_for is None:
            continue
        alert.calendar_context = context.get(alert.raised_for)
        if alert.calendar_context and alert.source == SOURCE_PACE:
            alert.qualifier = HOLIDAY_PACE_QUALIFIER


def _rank(alerts: list[Alert], as_of: dt.date) -> list[Alert]:
    """Order the queue by what an operator does next.

    Actionability first, then within `act_now` by deadline -- soonest first, since
    a date three days out is closing whatever its shortfall. Everything else falls
    back to the feed's own measure, which is meaningful WITHIN a source even
    though it is meaningless across sources.

    Recency is measured against `as_of`, not against the wall clock. This dataset
    ends at a fixed inventory horizon, so ordering by `date.today()` would drift a
    little further from the data every day the calendar moves -- the same trap
    `revenue.data_horizon` exists to avoid.
    """
    def key(alert: Alert) -> tuple[int, float, float]:
        band = ACTIONABILITY_ORDER.index(alert.actionability)
        if alert.actionability == ACT_NOW:
            return (band, float(alert.days_to_act or 0), -alert.measure_value)
        if alert.actionability == INVESTIGATE:
            days_ago = (as_of - alert.raised_for).days if alert.raised_for else 0
            return (band, float(days_ago), -alert.measure_value)
        return (band, 0.0, -alert.measure_value)

    return sorted(alerts, key=key)


def alert_center(as_of: dt.date | None = None) -> dict[str, Any]:
    """Every open alert from all four feeds, in one ranked queue."""
    as_of = as_of or rv.data_horizon()

    with db.session():
        scored = rv.pace(as_of)
        alerts = (
            _pace_alerts(as_of, scored)
            + _anomaly_alerts(as_of)
            + _data_quality_alerts()
            + _sla_alerts()
        )
        _attach_calendar(alerts)

    ranked = _rank(alerts, as_of)
    by_source: dict[str, int] = {}
    by_actionability: dict[str, int] = {}
    for alert in ranked:
        by_source[alert.source] = by_source.get(alert.source, 0) + 1
        by_actionability[alert.actionability] = (
            by_actionability.get(alert.actionability, 0) + 1
        )

    return {
        "as_of": as_of.isoformat(),
        "total": len(ranked),
        "by_source": by_source,
        "by_actionability": by_actionability,
        "sources": {
            SOURCE_PACE: "future stay dates behind their own weekday's booking curve",
            SOURCE_ANOMALY: (
                f"portfolio metric outliers in the last {ANOMALY_LOOKBACK_DAYS} days, "
                "day-of-week aware with a robust scale and a materiality gate"
            ),
            SOURCE_DATA_QUALITY: "registered rules failing their own threshold",
            SOURCE_SERVICE_SLA: (
                f"property/day-part cells whose breach rate sits at or above the "
                f"p{SLA_PEER_PERCENTILE} of comparable cells, with at least "
                f"{MIN_SLA_BREACHES} breaches. No absolute target rate exists in "
                f"this warehouse, so the comparison is against peers."
            ),
        },
        "ranking": (
            "Actionability first: dates you can still influence, then things that "
            "have already happened, then standing conditions. Within the first "
            "band, soonest deadline first."
        ),
        "known_bias": {
            "feed": SOURCE_PACE,
            "mechanism": (
                "The pace benchmark compares a date against the last 8 comparable "
                "same-weekday dates, which exclude the holiday, so the suppression "
                "F-101 measured on holiday-adjacent dates arrives here as a "
                "shortfall against a baseline that never saw a holiday."
            ),
            "magnitude": (
                "Not established. Pooled across 8 origins, 73.7% of behind-pace "
                "alerts fell on holiday-adjacent dates against a 39.0% base rate, "
                "which suggests 1.9x over-representation. That comparison does not "
                "survive a per-origin breakdown: base rates range from 0% to 100%, "
                "one origin with a 100% base rate contributed 11 of 19 alerts, and "
                "another with a 71.4% base rate raised none. Excluding the dominant "
                "origin gives 37.5% against 34.9% -- no effect. Simpson's paradox, "
                "so no ratio is published."
            ),
            "handling": (
                "Qualified, not suppressed. Affected alerts carry a `qualifier` "
                "naming the mechanism without claiming a magnitude. Dropping them "
                "would hide genuine weakness on dates where occupancy is already "
                "fragile."
            ),
        },
        "severity_note": (
            "There is deliberately NO severity score shared across sources. A "
            "robust z on ADR, a percentage of failing rows and a room-night "
            "shortfall are incommensurable, and collapsing them onto one 1-5 scale "
            "would require exchange rates nobody has measured. Each alert reports "
            "its own feed's measure with its units named; compare within a source, "
            "not across them."
        ),
        "alerts": [alert.as_dict() for alert in ranked],
    }


def opportunity_radar(as_of: dt.date | None = None) -> dict[str, Any]:
    """F-402: the upside half. Dates filling unusually early.

    Pace analysis that only surfaces weak dates is half an instrument. A date
    running ahead of its curve is the one where the remaining inventory was priced
    before anyone knew demand would be strong -- and it has the same deadline
    structure as a need date, so it belongs in the same envelope.

    It names no price. There is no elasticity and no competitor rate in this
    warehouse, so a rate recommendation would be an opinion with a number attached;
    a test enforces that and must not be weakened.
    """
    as_of = as_of or rv.data_horizon()

    with db.session():
        scored = rv.pace(as_of)
        ahead = rv.constrained_dates(as_of, scored=scored)
        alerts = [
            Alert(
                source=SOURCE_PACE,
                kind="ahead_of_pace",
                subject=f"{row.stay_date:%Y-%m-%d} · {row.property_name}",
                subject_type="stay_date",
                actionability=ACT_NOW,
                headline=(
                    f"{row.stay_date:%a %d %b} is {row.gap_nights:.0f} room-nights "
                    f"ahead of its usual position with {row.days_out} days to go"
                ),
                measure_name="room-night surplus against the same-weekday median",
                measure_value=row.gap_nights,
                measure_units="room-nights",
                evidence=[
                    f"{row.nights_on_books} nights on the books",
                    f"typically {row.expected_nights:.1f} by this point on a "
                    f"{row.stay_date:%A} (usual range "
                    f"{row.p25_nights:.0f}-{row.p75_nights:.0f})",
                    f"baseline from the last {row.support} comparable "
                    f"{row.stay_date:%A}s at this property",
                    f"confidence {row.confidence}",
                ],
                suggested_check=(
                    "Filling early. Verify remaining inventory and check whether "
                    "the rate on the remaining units was set before this demand "
                    "appeared."
                ),
                raised_for=row.stay_date,
                days_to_act=row.days_out,
            )
            for row in ahead
        ]
        _attach_calendar(alerts)

    ranked = _rank(alerts, as_of)
    return {
        "as_of": as_of.isoformat(),
        "total": len(ranked),
        "method": (
            "Stay dates carrying materially more nights than the median of the "
            "last 8 comparable same-weekday dates at the same property, scored "
            "only from stay dates before the as-of date."
        ),
        "note": (
            "No signal names a price. There is no competitor rate feed and no "
            "price elasticity in this warehouse, so a rate recommendation would "
            "be an opinion wearing a number."
        ),
        "opportunities": [alert.as_dict() for alert in ranked],
    }


def summary(as_of: dt.date | None = None) -> dict[str, Any]:
    """Counts only -- the one call a dashboard header needs."""
    as_of = as_of or rv.data_horizon()
    centre = alert_center(as_of)
    radar = opportunity_radar(as_of)
    return {
        "as_of": centre["as_of"],
        "alerts_total": centre["total"],
        "alerts_by_source": centre["by_source"],
        "alerts_by_actionability": centre["by_actionability"],
        "opportunities_total": radar["total"],
        "soonest_deadline_days": min(
            (a["days_to_act"] for a in centre["alerts"]
             if a["days_to_act"] is not None),
            default=None,
        ),
    }
