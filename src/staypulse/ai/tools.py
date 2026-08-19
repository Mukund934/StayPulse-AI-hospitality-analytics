"""The tool layer the copilot is allowed to use, and nothing else.

WHY THIS MODULE EXISTS SEPARATELY FROM THE COPILOT

Everything here is deterministic, synchronous and testable without a single
model call. That separation is deliberate: the part of the copilot that produces
numbers has no language model in it, and the part with a language model in it
produces no numbers.

It also means the tool layer is covered by the ordinary test suite. Nothing in
this file needs an API key, a network round trip or a quota, so its correctness
is established the same way the rest of the analytics is.


WHAT A TOOL IS ALLOWED TO BE

A thin wrapper over an analytics function that already exists and is already
tested. No tool computes anything itself. If a tool needed its own arithmetic
that would be a second definition of a metric, which is the thing the semantic
layer exists to prevent.


THE REFUSALS ARE PART OF THE CONTRACT

Some questions a revenue manager will ask cannot be answered from this
warehouse, and the honest response is to say so rather than to produce a
plausible number. Those are not gaps in the tool list, they are entries in it:
`REFUSALS` names the question, the reason, and what would unblock it. The
copilot is instructed to use them, and a test asserts they survive.

The clearest case is price. There is no competitor rate feed and no elasticity
here, so "what should I charge" has no defensible answer, and a model that
produced one would be inventing the most consequential number in the business.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Callable

from staypulse.analytics import alerts as _alerts
from staypulse.analytics import forecast as _forecast
from staypulse.analytics import intervals as _intervals
from staypulse.analytics import overbooking as _overbooking
from staypulse.analytics import replay as _replay
from staypulse.analytics import revenue as _revenue
from staypulse.analytics import rootcause as _rootcause
from staypulse.analytics import scenario as _scenario
from staypulse.signals import calendar as _calendar

# Text that came from a guest rather than from the warehouse. Anything carrying
# this marker is data to be summarised, never instructions to be followed.
UNTRUSTED_MARKER = "untrusted_user_generated_text"


@dataclass(frozen=True)
class Tool:
    """One deterministic capability the copilot may invoke."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any]

    def declaration(self) -> dict[str, Any]:
        """The shape a model needs to decide whether to call this."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _object(properties: dict[str, Any],
            required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
    }


_DATE = {"type": "string", "description": "ISO date, e.g. 2026-07-12"}


# ---------------------------------------------------------------------------
# Handlers. Each delegates; none computes.
# ---------------------------------------------------------------------------
def _kpi_snapshot(days: int = 30) -> dict[str, Any]:
    from api.app import services

    return services.kpi_overview(days=days)


def _forward_position(as_of: str | None = None) -> dict[str, Any]:
    return _revenue.summary(_parse_date(as_of))


def _pace_detail(as_of: str | None = None) -> dict[str, Any]:
    rows = _revenue.pace(_parse_date(as_of) or _revenue.data_horizon())
    return {
        "scored": len(rows),
        "behind": sum(1 for r in rows if r.status == "behind"),
        "ahead": sum(1 for r in rows if r.status == "ahead"),
        "stay_dates": [
            {
                "stay_date": r.stay_date.isoformat(),
                "property": r.property_name,
                "days_out": r.days_out,
                "nights_on_books": r.nights_on_books,
                "expected_nights": r.expected_nights,
                "gap_nights": r.gap_nights,
                "status": r.status,
                "confidence": r.confidence,
            }
            for r in rows if r.status != "on_track"
        ],
    }


def _explain_revpar(end: str | None = None, days: int = 30) -> dict[str, Any]:
    last = _parse_date(end) or _revenue.data_horizon()
    start = last - dt.timedelta(days=days - 1)
    return _rootcause.explain_revpar(start, last).as_dict()


def _forecast_with_interval(horizon_days: int = 14,
                            level: float = 0.8) -> dict[str, Any]:
    return _intervals.forward(horizon=horizon_days, level=level)


def _forecast_accuracy() -> dict[str, Any]:
    from api.app import services

    return services.rm_forecast_accuracy()


def _holiday_effect() -> dict[str, Any]:
    return _calendar.summary()


def _alert_queue(as_of: str | None = None) -> dict[str, Any]:
    return _alerts.summary(_parse_date(as_of))


def _opportunities(as_of: str | None = None) -> dict[str, Any]:
    return _alerts.opportunity_radar(_parse_date(as_of))


def _decision_replay(as_of: str | None = None) -> dict[str, Any]:
    target = _parse_date(as_of) or (
        _revenue.data_horizon() - dt.timedelta(days=40)
    )
    return _replay.summary(target)


def _cancellation_risk() -> dict[str, Any]:
    from api.app import services

    return services.rm_cancellation_model()


def _overbooking_distribution(stay_date: str | None = None,
                              cost_ratio: float | None = None) -> dict[str, Any]:
    if stay_date:
        parsed = _parse_date(stay_date)
        as_of = parsed - dt.timedelta(days=7)
        return _overbooking.simulate_stay_date(as_of, parsed, cost_ratio=cost_ratio)
    tightest = _overbooking.tightest_stay_date()
    return _overbooking.simulate_stay_date(
        tightest["as_of"], tightest["stay_date"], cost_ratio=cost_ratio
    )


def _scenario_result(occupancy_pp: float = 0.0, adr_pct: float = 0.0,
                     capacity_units_pct: float = 0.0) -> dict[str, Any]:
    return _scenario.apply_levers(
        _scenario.baseline(),
        occupancy_pp=occupancy_pp,
        adr_pct=adr_pct,
        capacity_units_pct=capacity_units_pct,
    ).as_dict()


def _data_quality() -> dict[str, Any]:
    from api.app import services

    return {
        "overview": services.data_quality_overview(),
        "failing_rules": [
            row for row in services.data_quality_rules() if not row["passed"]
        ],
    }


def _metric_definitions() -> dict[str, Any]:
    from api.app import services

    return {"metrics": services.metric_definitions()}


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not an ISO date") from None


# ---------------------------------------------------------------------------
TOOLS: tuple[Tool, ...] = (
    Tool(
        name="get_kpi_snapshot",
        description=(
            "Portfolio occupancy, ADR, RevPAR and revenue over a trailing window, "
            "with a like-for-like comparison against the preceding window of the "
            "same length. Use for 'how are we doing'."
        ),
        parameters=_object({
            "days": {"type": "integer",
                     "description": "Trailing window length, default 30"},
        }),
        handler=_kpi_snapshot,
    ),
    Tool(
        name="get_forward_position",
        description=(
            "Nights and revenue on the books, trailing pickup, and how many "
            "future stay dates are ahead of or behind their own booking curve. "
            "Use for 'what does the forward book look like'."
        ),
        parameters=_object({"as_of": _DATE}),
        handler=_forward_position,
    ),
    Tool(
        name="get_pace_detail",
        description=(
            "Every future stay date that is materially ahead of or behind its "
            "own weekday's booking curve, with the evidence behind each. Use for "
            "'which dates need attention' or 'why is occupancy behind'."
        ),
        parameters=_object({"as_of": _DATE}),
        handler=_pace_detail,
    ),
    Tool(
        name="explain_revpar_change",
        description=(
            "Deterministic decomposition of a RevPAR movement into occupancy and "
            "rate contributions, plus attribution by property and channel, and "
            "whether an ADR move was rate or mix. Use for any 'why did X change' "
            "question. No language model is involved in the attribution."
        ),
        parameters=_object({
            "end": _DATE,
            "days": {"type": "integer",
                     "description": "Window length in days, default 30"},
        }),
        handler=_explain_revpar,
    ),
    Tool(
        name="get_forecast",
        description=(
            "Forward occupancy forecast in room-nights with an empirical "
            "prediction interval around each point. The interval is calibrated "
            "on the model's own backtest residuals; its measured out-of-sample "
            "coverage is published by get_forecast_accuracy."
        ),
        parameters=_object({
            "horizon_days": {"type": "integer", "description": "1-30, default 14"},
            "level": {"type": "number",
                      "description": "Interval level, 0.5 to 0.95, default 0.8"},
        }),
        handler=_forecast_with_interval,
    ),
    Tool(
        name="get_forecast_accuracy",
        description=(
            "Rolling-origin backtest of every forecasting model by horizon, "
            "including the horizons where the default model loses. Use when "
            "asked how accurate or how trustworthy a forecast is."
        ),
        parameters=_object({}),
        handler=_forecast_accuracy,
    ),
    Tool(
        name="get_holiday_effect",
        description=(
            "Measured public-holiday effect on occupancy by holiday and by offset "
            "from the holiday, with confidence intervals and occurrence counts. "
            "In this corporate portfolio holidays SUPPRESS demand."
        ),
        parameters=_object({}),
        handler=_holiday_effect,
    ),
    Tool(
        name="get_alerts",
        description=(
            "Every open alert across four feeds -- pace, anomalies, data-quality "
            "failures and SLA breaches -- ordered by actionability. Use for "
            "'what should I look at today'. There is deliberately no severity "
            "score comparable across feeds."
        ),
        parameters=_object({"as_of": _DATE}),
        handler=_alert_queue,
    ),
    Tool(
        name="get_opportunities",
        description=(
            "Future stay dates filling ahead of their own curve. The upside half "
            "of pace analysis. Names no price."
        ),
        parameters=_object({"as_of": _DATE}),
        handler=_opportunities,
    ),
    Tool(
        name="get_decision_replay",
        description=(
            "Reconstruct what was knowable at a past date and what the system "
            "would have said then, with no hindsight, alongside what actually "
            "happened. Use for 'what did we know on <date>'."
        ),
        parameters=_object({"as_of": _DATE}),
        handler=_decision_replay,
    ),
    Tool(
        name="get_cancellation_risk",
        description=(
            "Cancellation risk model performance: AUC, precision and recall "
            "against the base rate, the calibration curve, and recovery of the "
            "known channel and lead-time mechanism. Use for questions about "
            "which bookings are likely to cancel and how good that model is."
        ),
        parameters=_object({}),
        handler=_cancellation_risk,
    ),
    Tool(
        name="get_overbooking_distribution",
        description=(
            "Outcome distribution for overbooking a stay date: probability of "
            "walking anyone, expected walks and empty rooms at each level, and "
            "the breakeven cost ratio. It returns NO recommended level unless a "
            "cost_ratio is supplied, because this warehouse prices neither a "
            "walked guest nor an empty room."
        ),
        parameters=_object({
            "stay_date": _DATE,
            "cost_ratio": {
                "type": "number",
                "description": (
                    "Cost of walking a guest in units of one empty room. Only "
                    "pass this if the USER supplied it. Never invent one."
                ),
            },
        }),
        handler=_overbooking_distribution,
    ),
    Tool(
        name="get_scenario_result",
        description=(
            "What the RevPAR identity gives if occupancy, ADR or capacity moved "
            "by a stated amount, with an exact decomposition and the assumptions "
            "held constant. This is a SCENARIO, not a forecast: it predicts "
            "nothing and does not claim the change is achievable. Occupancy is "
            "in percentage POINTS."
        ),
        parameters=_object({
            "occupancy_pp": {"type": "number",
                             "description": "Change in percentage POINTS"},
            "adr_pct": {"type": "number", "description": "Change in percent"},
            "capacity_units_pct": {"type": "number",
                                   "description": "Change in percent"},
        }),
        handler=_scenario_result,
    ),
    Tool(
        name="get_data_quality",
        description=(
            "Data-quality score and every rule currently failing its threshold. "
            "Use when asked whether the numbers can be trusted. Some failures are "
            "deliberate: the dataset carries planted defects so the checks have "
            "something to catch."
        ),
        parameters=_object({}),
        handler=_data_quality,
    ),
    Tool(
        name="get_metric_definition",
        description=(
            "The registered definition of every metric, including its date basis "
            "and caveats. Use when asked what a metric means or how it is "
            "computed."
        ),
        parameters=_object({}),
        handler=_metric_definitions,
    ),
)

BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


# ---------------------------------------------------------------------------
REFUSALS: tuple[dict[str, str], ...] = (
    {
        "question": "What rate or price should I set?",
        "refuse_because": (
            "There is no competitor rate feed and no measured price elasticity "
            "in this warehouse. A recommended rate would be an opinion with a "
            "number attached, and it is the most consequential number in the "
            "business to get wrong."
        ),
        "offer_instead": (
            "Pace, opportunity and constrained dates, so a human can see which "
            "dates are filling early or late and price them with market "
            "knowledge the system does not have."
        ),
        "would_unblock": "A licensed rate-shopping feed and a measured elasticity.",
    },
    {
        "question": "What overbooking level should we set?",
        "refuse_because": (
            "The optimum needs the cost of walking a guest relative to an empty "
            "room. This warehouse contains no relocation cost, compensation "
            "figure or goodwill model."
        ),
        "offer_instead": (
            "The full outcome distribution at each level and the breakeven cost "
            "ratio, so the operator can apply their own cost."
        ),
        "would_unblock": "A recorded relocation cost per walked guest.",
    },
    {
        "question": "How much revenue would we gain by doing X?",
        "refuse_because": (
            "Scenario arithmetic says what the books would show if something "
            "changed. It cannot say the change is achievable, or what achieving "
            "it would cost, because there is no demand response in this data."
        ),
        "offer_instead": (
            "The scenario result with its assumptions stated explicitly."
        ),
        "would_unblock": "Price elasticity and an acquisition-cost model.",
    },
    {
        "question": "How do we compare against competitors or the market?",
        "refuse_because": (
            "There is no comp set. MPI, ARI and RGI all require agreed "
            "participants, and scraping OTA rates would breach their terms."
        ),
        "offer_instead": "Internal benchmarks: same-weekday history and own pace.",
        "would_unblock": "An STR subscription or a market consortium.",
    },
)


def declarations() -> list[dict[str, Any]]:
    """Tool declarations, in the shape a model needs to choose between them."""
    return [tool.declaration() for tool in TOOLS]


def invoke(name: str, arguments: dict[str, Any] | None = None) -> Any:
    """Execute a named tool. Raises on an unknown name rather than guessing.

    A model that hallucinates a tool name gets an error it can see and correct,
    not a silent empty result that would then be narrated as a finding.
    """
    if name not in BY_NAME:
        raise KeyError(
            f"unknown tool {name!r}; available tools are {sorted(BY_NAME)}"
        )
    return BY_NAME[name].handler(**(arguments or {}))
