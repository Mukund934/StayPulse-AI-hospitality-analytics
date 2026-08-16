"""Public-holiday demand effects, measured rather than assumed.

WHAT THIS ANSWERS

"Is 20 October weak because of Diwali, and by how much?" -- with a number derived
from booking data, an interval around it, and an honest statement of how little
evidence supports it.


THE MEASUREMENT IS NON-CIRCULAR, AND THAT IS THE WHOLE POINT

The generator plants four suppressive festival windows. It would be trivial, and
worthless, to read those windows out of `spec.py`, flag the same dates, and report
that the effect appears exactly where it was planted.

So nothing here knows about the planted windows. The warehouse stores only what is
externally true -- the dates public holidays fell on -- and this module measures
occupancy at each OFFSET from a holiday, assuming no window at all. The window
emerges from the data or it does not.

`validate_against_planted()` is the only function that touches the generator spec,
it is used at validation time only, and it exists to answer "did the measurement
recover the truth" rather than to produce the measurement.


THE BASELINE CONTROLS FOR TWO THINGS, BECAUSE IT HAS TO

  1. DAY OF WEEK. Diwali 2025 fell on a Monday. Monday carries a 1.14 demand
     multiplier here against Saturday's 0.74, so comparing a holiday Monday to an
     all-days average would report a demand *lift* on a date demand actually fell.

  2. LOCAL LEVEL. Sellable inventory grew about a third in March 2026 and
     occupancy trends upward across the series. A baseline drawn from all history
     would put every late date above its own comparison. The baseline is therefore
     drawn from a window around each date, not from the whole series.

Same reasoning, and same fix, as the pace benchmark in `analytics/revenue.py` --
which was shipped broken for exactly this reason before it was caught.


ON THE SIZE OF THE EVIDENCE

Three festival windows fall inside the data. That is a small sample and no amount
of arithmetic changes it. Every effect is reported with an interval and an explicit
observation count, and `interpretation()` refuses to call a result reliable when it
is not.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Any

from staypulse import db

# Offsets scanned either side of a holiday. Wider than the adjacency radius used by
# the loader so the profile shows where the effect actually stops, rather than
# stopping wherever the radius was set.
PROFILE_RANGE = range(-7, 11)

# Half-width of the window the local baseline is drawn from. Eight weeks either
# side gives enough same-weekday observations to be stable while staying local
# enough to track the growth trend.
BASELINE_HALF_WINDOW_DAYS = 56

# Minimum comparable observations before an offset is scored at all.
MIN_BASELINE_OBS = 4

# Below this many observed dates an effect is reported but flagged unreliable.
MIN_EFFECT_OBS = 3


@dataclass
class OffsetEffect:
    """Measured occupancy effect at one offset from a public holiday."""

    offset: int
    observations: int
    mean_occupancy_pct: float
    mean_baseline_pct: float
    effect_pp: float
    ci_low_pp: float
    ci_high_pp: float

    @property
    def is_significant(self) -> bool:
        """Interval excludes zero. With this sample size, read it as a hint."""
        return (self.ci_low_pp > 0) or (self.ci_high_pp < 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "offset_days": self.offset,
            "observations": self.observations,
            "mean_occupancy_pct": round(self.mean_occupancy_pct, 2),
            "mean_baseline_pct": round(self.mean_baseline_pct, 2),
            "effect_pp": round(self.effect_pp, 2),
            "ci_low_pp": round(self.ci_low_pp, 2),
            "ci_high_pp": round(self.ci_high_pp, 2),
            "excludes_zero": self.is_significant,
        }


@dataclass
class HolidayEffect:
    """Aggregate effect for one named holiday, across its adjacent dates."""

    name: str
    occurrences: int
    observations: int
    effect_pp: float
    ci_low_pp: float
    ci_high_pp: float
    peak_offset: int | None
    peak_effect_pp: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "holiday": self.name,
            "occurrences_in_data": self.occurrences,
            "observations": self.observations,
            "effect_pp": round(self.effect_pp, 2),
            "ci_low_pp": round(self.ci_low_pp, 2),
            "ci_high_pp": round(self.ci_high_pp, 2),
            "peak_offset_days": self.peak_offset,
            "peak_effect_pp": (
                None if self.peak_effect_pp is None else round(self.peak_effect_pp, 2)
            ),
            "direction": (
                "suppresses demand" if self.effect_pp < 0 else "raises demand"
            ),
            "reliable": self.observations >= MIN_EFFECT_OBS and self.occurrences >= 1,
        }


# ---------------------------------------------------------------------------
def _paired_observations(as_of: dt.date | None = None) -> list[dict[str, Any]]:
    """Every holiday-adjacent date paired with its own local, same-weekday baseline.

    The baseline for a date is the median occupancy of the SAME weekday at the SAME
    property within +/-BASELINE_HALF_WINDOW_DAYS, excluding every date that is
    itself holiday-adjacent -- otherwise one holiday would contaminate the baseline
    of the next.

    `as_of` restricts every input to dates strictly before it. That is what makes
    the forecasting multiplier leak-free.
    """
    return db.fetch_all(
        """
        WITH kpi AS (
            SELECT property_key, stay_date, date_key, occupancy_pct,
                   EXTRACT(ISODOW FROM stay_date)::int AS dow,
                   days_to_holiday, nearest_holiday, is_holiday_adjacent
            FROM mart.v_daily_kpi_calendar
            WHERE occupancy_pct IS NOT NULL
              AND (CAST(:as_of AS date) IS NULL OR stay_date < CAST(:as_of AS date))
        ),
        target AS (
            SELECT * FROM kpi WHERE is_holiday_adjacent AND days_to_holiday IS NOT NULL
        )
        SELECT t.property_key,
               t.stay_date,
               t.days_to_holiday          AS offset,
               t.nearest_holiday          AS holiday,
               t.occupancy_pct            AS occupancy,
               b.baseline_pct,
               b.baseline_obs
        FROM target t
        CROSS JOIN LATERAL (
            SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY k.occupancy_pct)
                       AS baseline_pct,
                   count(*) AS baseline_obs
            FROM kpi k
            WHERE k.property_key = t.property_key
              AND k.dow          = t.dow
              AND NOT k.is_holiday_adjacent
              AND k.stay_date BETWEEN t.stay_date - :w AND t.stay_date + :w
        ) b
        WHERE b.baseline_obs >= :minobs
        ORDER BY t.stay_date, t.property_key
        """,
        as_of=as_of,
        w=BASELINE_HALF_WINDOW_DAYS,
        minobs=MIN_BASELINE_OBS,
    )


def _mean_ci(values: list[float]) -> tuple[float, float, float]:
    """Mean and a t-style 95% interval. Falls back to the mean when n < 2.

    A normal approximation on three-to-thirty observations is generous, so the
    interval is reported as an indication of spread rather than a formal test, and
    every consumer states the observation count next to it.
    """
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, mean, mean
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    stderr = math.sqrt(var / n)
    # t critical value at 95%, small-sample table, flattening to 2.0 once n is large.
    t = {2: 12.71, 3: 4.30, 4: 3.18, 5: 2.78, 6: 2.57, 7: 2.45, 8: 2.36,
         9: 2.31, 10: 2.26}.get(n, 2.09 if n < 30 else 1.96)
    return mean, mean - t * stderr, mean + t * stderr


def offset_profile(as_of: dt.date | None = None) -> list[OffsetEffect]:
    """Occupancy effect at each offset from a public holiday.

    No window is assumed. If a demand window exists, it shows up as a run of
    negative offsets in this profile.
    """
    rows = _paired_observations(as_of)
    by_offset: dict[int, list[tuple[float, float]]] = {}
    for r in rows:
        off = int(r["offset"])
        if off in PROFILE_RANGE:
            by_offset.setdefault(off, []).append(
                (float(r["occupancy"]), float(r["baseline_pct"]))
            )

    out: list[OffsetEffect] = []
    for off in PROFILE_RANGE:
        pairs = by_offset.get(off, [])
        if not pairs:
            continue
        diffs = [occ - base for occ, base in pairs]
        mean, lo, hi = _mean_ci(diffs)
        out.append(OffsetEffect(
            offset=off,
            observations=len(pairs),
            mean_occupancy_pct=sum(o for o, _ in pairs) / len(pairs),
            mean_baseline_pct=sum(b for _, b in pairs) / len(pairs),
            effect_pp=mean,
            ci_low_pp=lo,
            ci_high_pp=hi,
        ))
    return out


def holiday_effects(as_of: dt.date | None = None) -> list[HolidayEffect]:
    """Aggregate effect per named holiday, worst first."""
    rows = _paired_observations(as_of)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        grouped.setdefault(str(r["holiday"]), []).append(r)

    out: list[HolidayEffect] = []
    for name, items in grouped.items():
        diffs = [float(i["occupancy"]) - float(i["baseline_pct"]) for i in items]
        mean, lo, hi = _mean_ci(diffs)

        by_off: dict[int, list[float]] = {}
        for i in items:
            by_off.setdefault(int(i["offset"]), []).append(
                float(i["occupancy"]) - float(i["baseline_pct"])
            )
        peak_off, peak_val = None, None
        for off, vals in by_off.items():
            avg = sum(vals) / len(vals)
            if peak_val is None or abs(avg) > abs(peak_val):
                peak_off, peak_val = off, avg

        out.append(HolidayEffect(
            name=name,
            occurrences=len({i["stay_date"].year for i in items}),
            observations=len(items),
            effect_pp=mean,
            ci_low_pp=lo,
            ci_high_pp=hi,
            peak_offset=peak_off,
            peak_effect_pp=peak_val,
        ))
    return sorted(out, key=lambda e: e.effect_pp)


# ---------------------------------------------------------------------------
def holiday_multiplier(as_of: dt.date | None = None,
                       significant_only: bool = True) -> dict[tuple[str, int], float]:
    """(holiday, offset) -> occupancy multiplier, for forecasting.

    Estimated ONLY from holidays that had already completed before `as_of`. A model
    that used the multiplier from the holiday it is currently forecasting would be
    reading its own answer, and would score beautifully while being useless.

    SIGNIFICANCE GATE, and why it is on by default.

    Measured on this dataset, adjusting by every holiday's multiplier made the
    forecast WORSE than leaving it alone -- MAE 4.94 against a 4.19 baseline on
    holiday-adjacent dates. The reason is not a coding error, it is the data:

      - The holidays with a real planted effect (Diwali, year end) occur ONCE
        inside eighteen months, so at any point in the test window they have no
        prior occurrence to learn from.
      - The holidays that DO repeat (Republic Day, Good Friday, Ugadi,
        Independence Day) have no real effect, so their fitted multipliers are
        noise.

    Adjusting by noise adds variance and removes nothing. So a multiplier is only
    returned when that holiday's own measured effect has an interval excluding
    zero. On this dataset that admits almost nothing, and the model correctly
    degrades to its unadjusted baseline.

    Pass `significant_only=False` to reproduce the unfiltered version and the
    numbers above.
    """
    rows = _paired_observations(as_of)

    allowed: set[str] | None = None
    if significant_only:
        allowed = {e.name for e in holiday_effects(as_of)
                   if e.observations >= MIN_EFFECT_OBS
                   and ((e.ci_low_pp > 0) or (e.ci_high_pp < 0))}

    grouped: dict[tuple[str, int], list[float]] = {}
    for r in rows:
        base = float(r["baseline_pct"])
        name = str(r["holiday"])
        if base <= 0 or (allowed is not None and name not in allowed):
            continue
        grouped.setdefault((name, int(r["offset"])), []).append(
            float(r["occupancy"]) / base
        )
    return {k: sum(v) / len(v) for k, v in grouped.items() if v}


def generic_multiplier(as_of: dt.date | None = None) -> dict[int, float]:
    """offset -> multiplier, pooled across holidays.

    The fallback when a specific holiday has no prior occurrence. Diwali appears
    once inside the data, so on its only occurrence there is no Diwali-specific
    history to learn from and the pooled shape is all a forecaster can honestly use.
    """
    rows = _paired_observations(as_of)
    grouped: dict[int, list[float]] = {}
    for r in rows:
        base = float(r["baseline_pct"])
        if base > 0:
            grouped.setdefault(int(r["offset"]), []).append(
                float(r["occupancy"]) / base
            )
    return {k: sum(v) / len(v) for k, v in grouped.items() if len(v) >= MIN_EFFECT_OBS}


# ---------------------------------------------------------------------------
def validate_against_planted() -> dict[str, Any]:
    """Compare the measured effect to the generator's planted windows.

    THE ONLY function here that imports the generator spec, and it never feeds the
    measurement. It answers one question: did measuring from holiday dates alone
    recover a demand suppression the measurement was never told about?
    """
    from staypulse.generate.spec import FESTIVAL_WINDOWS

    horizon = db.scalar("SELECT max(stay_date) FROM mart.fact_unit_night")
    planted = [
        {
            "name": name,
            "from": start.isoformat(),
            "to": end.isoformat(),
            "planted_demand_multiplier": mult,
            "in_data": end <= horizon,
        }
        for start, end, mult, name in FESTIVAL_WINDOWS
    ]

    measured = {e.name: e for e in holiday_effects()}
    profile = offset_profile()
    suppressed = [p for p in profile if p.effect_pp < 0]

    checks = {
        "direction_is_suppressive": bool(suppressed) and (
            sum(p.effect_pp for p in profile) < 0
        ),
        "diwali_measured": "Diwali" in measured,
        "diwali_effect_negative": (
            measured["Diwali"].effect_pp < 0 if "Diwali" in measured else None
        ),
    }

    return {
        "method": (
            "Effect measured from public-holiday DATES only, at each offset, with a "
            "same-weekday local-window baseline. The planted windows below were not "
            "used in the measurement and are shown only as ground truth."
        ),
        "planted_windows": planted,
        "planted_windows_in_data": sum(1 for p in planted if p["in_data"]),
        "measured_holidays": [e.as_dict() for e in measured.values()],
        "checks": checks,
        "caveat": (
            "Only three planted windows fall inside the data and Diwali appears "
            "once. Recovery of the direction and rough shape is meaningful; a "
            "precise magnitude from this sample is not."
        ),
    }


def interpretation(effects: list[HolidayEffect]) -> str:
    """A plain sentence about what was found, that declines to overclaim."""
    if not effects:
        return "No holiday-adjacent dates with a usable baseline were found."

    worst = effects[0]
    negative = [e for e in effects if e.effect_pp < 0]
    direction = (
        "suppress" if len(negative) > len(effects) / 2 else "raise"
    )

    text = (
        f"Across {len(effects)} holidays with measurable adjacent dates, public "
        f"holidays {direction} occupancy at this portfolio. The largest effect is "
        f"{worst.name} at {worst.effect_pp:+.1f}pp "
        f"({worst.ci_low_pp:+.1f} to {worst.ci_high_pp:+.1f}, "
        f"n={worst.observations})."
    )
    if direction == "suppress":
        text += (
            " That is the inverse of a leisure property and is what a corporate "
            "aparthotel should show: business travel stops during festivals."
        )

    # The caveat keys on OCCURRENCES, not observations, and the distinction is the
    # whole point. Twenty-two adjacent dates around a single Diwali are twenty-two
    # measurements of one event, not twenty-two independent samples. Treating them
    # as independent is pseudo-replication: it shrinks the interval without adding
    # any evidence, and it is exactly how a small-sample artifact comes to look
    # statistically significant.
    single = [e.name for e in effects if e.occurrences < 2]
    if worst.occurrences < 2:
        text += (
            f" {worst.name} occurs once in this dataset, so those "
            f"{worst.observations} adjacent dates are repeated measurements of a "
            "single event rather than independent samples. Treat the direction as "
            "the finding and the magnitude as indicative; the interval is narrower "
            "than the evidence warrants."
        )
    elif single:
        text += (
            f" {len(single)} of {len(effects)} holidays occur only once here, so "
            "their intervals are narrower than the evidence warrants. Treat "
            "direction as the finding and magnitude as indicative."
        )
    elif worst.observations < 10:
        text += (
            " The sample is small, so treat the direction as the finding and the "
            "magnitude as indicative."
        )
    return text


def summary(as_of: dt.date | None = None) -> dict[str, Any]:
    """The one call the API and the report both use."""
    effects = holiday_effects(as_of)
    profile = offset_profile(as_of)
    src = db.fetch_all(
        "SELECT source_key, entry_count, needs_review, coverage_from, coverage_to "
        "FROM meta.calendar_source WHERE source_key = 'india_holidays'"
    )
    return {
        "as_of": as_of.isoformat() if as_of else None,
        "source": (
            {
                "key": src[0]["source_key"],
                "entries": int(src[0]["entry_count"]),
                "entries_needing_human_review": int(src[0]["needs_review"]),
                "coverage": f"{src[0]['coverage_from']} .. {src[0]['coverage_to']}",
                "origin": (
                    "committed file; no external API - Nager.Date does not cover "
                    "India (verified HTTP 204)"
                ),
            }
            if src else None
        ),
        "method": (
            "Occupancy at each offset from a public holiday against a same-weekday "
            "baseline drawn from a +/-8 week local window that excludes all "
            "holiday-adjacent dates. No demand window is assumed; the window "
            "emerges from the profile."
        ),
        "interpretation": interpretation(effects),
        "holidays": [e.as_dict() for e in effects],
        "offset_profile": [p.as_dict() for p in profile],
    }
