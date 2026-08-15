"""Why did this KPI change? Deterministic decomposition of a metric movement.

WHAT THIS IS FOR

A dashboard that reports "RevPAR fell 8.2%" has handed the analyst the whole job.
The questions that follow are always the same -- was it rate or volume, which
property, which channel, weekday or weekend, is it one bad week or a trend -- and
answering them by hand takes an afternoon and is where most of an analyst's time
actually goes. This module answers them in one call, and shows its work.


THE RULE THIS MODULE EXISTS TO ENFORCE

No language model discovers, ranks or invents a cause. Every driver here comes out
of arithmetic on the warehouse, every number is reproducible, and the ranking is by
measured contribution. An LLM may later phrase these findings in prose -- that is
narration of a computed result, and it is the only role it is allowed. The
difference matters: "revenue fell because of weak weekday demand" is worth nothing
if a model guessed it from context, and is worth a great deal if it fell out of a
decomposition that sums exactly to the observed movement.


THE DECOMPOSITION IS EXACT, NOT INDICATIVE

RevPAR = Occupancy x ADR is multiplicative, so the split into "how much came from
occupancy" and "how much came from rate" is genuinely ambiguous: there is an
interaction term and it has to go somewhere. Three choices are common. Assigning it
to one factor is arbitrary and flatters whichever one you pick. Reporting it as a
third residual line is honest but unreadable in a brief. This module uses the
symmetric (Shapley) split, which distributes the interaction evenly:

    occupancy contribution = d(Occ) x mean(ADR_before, ADR_after)
    rate contribution      = d(ADR) x mean(Occ_before, Occ_after)

Those two sum to the total change exactly, with no residual, which is asserted in
the test suite rather than asserted in a comment.

Dimensional attribution needs no such care, because revenue is additive: the sum of
each property's revenue change IS the portfolio revenue change. The contributions
are therefore exact by construction and are reported as shares of the movement.


MIX VERSUS RATE

An ADR fall does not mean anyone lowered a price. It can equally mean the same
prices with more nights sold through cheaper channels. Those demand opposite
responses, so the two are separated explicitly rather than left for the reader to
infer.


CONFIDENCE COMES FROM CONCENTRATION

If one property accounts for 72% of a decline, that is a finding. If seven segments
each account for 14%, there is no driver and saying "the largest contributor is
Koramangala at 14%" would be technically true and actively misleading. Confidence
here measures how concentrated the explanation is, and the module will say that no
single driver exists when that is the truth.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from staypulse import db

# A contributor must move the metric by at least this share of the total movement
# before it is worth naming. Below it, the "driver" is noise with a label.
MATERIAL_SHARE = 0.10

# Concentration thresholds for confidence, measured as the share of the total
# movement explained by the single largest contributor within a dimension.
HIGH_CONCENTRATION = 0.50
MEDIUM_CONCENTRATION = 0.30

# Any relative change smaller than this is treated as flat. Prevents the engine
# announcing a root cause for a 0.3% wobble.
NOISE_FLOOR_PCT = 1.0


@dataclass
class Contribution:
    """One member of one dimension, and what it did to the metric."""

    dimension: str
    member: str
    before: float
    after: float
    change: float
    share_of_movement: float
    basis: str = "revpar_inr"
    capacity_mix_effect: float | None = None
    performance_effect: float | None = None
    nights_change: int | None = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "dimension": self.dimension,
            "member": self.member,
            "basis": self.basis,
            "before": round(self.before, 2),
            "after": round(self.after, 2),
            "change": round(self.change, 2),
            "share_of_movement_pct": round(100 * self.share_of_movement, 1),
        }
        if self.capacity_mix_effect is not None:
            d["capacity_mix_effect"] = round(self.capacity_mix_effect, 2)
            d["performance_effect"] = round(self.performance_effect or 0.0, 2)
        if self.nights_change is not None:
            d["nights_change"] = self.nights_change
        return d


@dataclass
class Explanation:
    metric: str
    current: dict[str, Any]
    baseline: dict[str, Any]
    change_abs: float
    change_pct: float
    components: list[dict[str, Any]] = field(default_factory=list)
    drivers: list[Contribution] = field(default_factory=list)
    channel_revenue: list[dict[str, Any]] = field(default_factory=list)
    capacity: dict[str, Any] = field(default_factory=dict)
    mix_vs_rate: dict[str, Any] | None = None
    primary_signal: str = ""
    confidence: str = "low"
    evidence_count: int = 0
    caveats: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "metric": self.metric,
            "current_period": self.current,
            "baseline_period": self.baseline,
            "change": round(self.change_abs, 2),
            "change_pct": round(self.change_pct, 2),
            "components": self.components,
            "drivers": [d.as_dict() for d in self.drivers],
            "channel_revenue_attribution": self.channel_revenue,
            "capacity": self.capacity,
            "mix_vs_rate": self.mix_vs_rate,
            "primary_signal": self.primary_signal,
            "confidence": self.confidence,
            "evidence_count": self.evidence_count,
            "caveats": self.caveats,
            "method": (
                "Symmetric (Shapley) split of the multiplicative identity "
                "RevPAR = Occupancy x ADR, then additive attribution across "
                "dimensions. Contributions sum to the total movement exactly. "
                "No language model participates in finding or ranking a cause."
            ),
        }


# ---------------------------------------------------------------------------
def _window(start: dt.date, end: dt.date) -> dict[str, Any]:
    """Portfolio aggregates for a date window, from the semantic layer only."""
    r = db.fetch_all(
        """
        SELECT sum(rooms_available)                       AS rooms_available,
               sum(rooms_sold)                            AS rooms_sold,
               sum(room_revenue_net_inr)                  AS revenue,
               sum(rooms_out_of_order)                    AS ooo
        FROM mart.v_daily_kpi
        WHERE stay_date BETWEEN :a AND :b
        """,
        a=start, b=end,
    )[0]
    avail = float(r["rooms_available"] or 0)
    sold = float(r["rooms_sold"] or 0)
    rev = float(r["revenue"] or 0)
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "days": (end - start).days + 1,
        "rooms_available": int(avail),
        "rooms_sold": int(sold),
        "revenue_inr": round(rev, 2),
        "occupancy": (sold / avail) if avail else 0.0,
        "adr": (rev / sold) if sold else 0.0,
        "revpar": (rev / avail) if avail else 0.0,
        "rooms_out_of_order": int(r["ooo"] or 0),
    }


def _attribute_revpar(dimension: str, label_sql: str, join_sql: str,
                      cur: tuple[dt.date, dt.date], base: tuple[dt.date, dt.date],
                      total_change: float) -> list[Contribution]:
    """Decompose a portfolio RevPAR movement across a dimension that HAS capacity.

    REVENUE ATTRIBUTION CANNOT DECOMPOSE REVPAR, and getting this wrong is the
    single easiest way to produce a confident, wrong answer. The first version of
    this module attributed the revenue change and then narrated it as a RevPAR
    story. In March 2026 that reported HSR Layout as the driver of an 18% RevPAR
    DECLINE while HSR's revenue had risen by 341,858 INR -- and gave it a 134%
    share. Both absurdities have the same root: the portfolio added a third more
    inventory that month, so revenue rose while RevPAR fell. Attributing the
    numerator of a ratio explains nothing about the ratio.

    The correct instrument writes portfolio RevPAR as a capacity-weighted average of
    each member's own RevPAR:

        RevPAR = SUM_i w_i * RevPAR_i        where w_i = available_i / available

    which splits exactly, by the same symmetric rule used for occupancy and rate:

        capacity mix effect  = d(w_i)      * mean(RevPAR_i before, after)
        performance effect   = mean(w_i)   * d(RevPAR_i)

    Summed over all members these reproduce the portfolio movement with no residual.
    A member can now correctly show a NEGATIVE contribution while its revenue grew,
    which is exactly what happened in March and is the actual finding.
    """
    rows = db.fetch_all(
        f"""
        WITH cur AS (
            SELECT {label_sql} AS member,
                   sum(n.room_revenue_net_inr) AS rev,
                   count(*) FILTER (WHERE n.is_sellable) AS avail
            FROM mart.v_unit_night_enriched n
            {join_sql}
            WHERE n.stay_date BETWEEN :c1 AND :c2
            GROUP BY 1
        ),
        base AS (
            SELECT {label_sql} AS member,
                   sum(n.room_revenue_net_inr) AS rev,
                   count(*) FILTER (WHERE n.is_sellable) AS avail
            FROM mart.v_unit_night_enriched n
            {join_sql}
            WHERE n.stay_date BETWEEN :b1 AND :b2
            GROUP BY 1
        )
        SELECT coalesce(c.member, b.member) AS member,
               coalesce(b.rev, 0) AS rev_before, coalesce(c.rev, 0) AS rev_after,
               coalesce(b.avail, 0) AS avail_before, coalesce(c.avail, 0) AS avail_after
        FROM cur c FULL OUTER JOIN base b USING (member)
        """,
        c1=cur[0], c2=cur[1], b1=base[0], b2=base[1],
    )

    total_avail_before = sum(float(r["avail_before"]) for r in rows)
    total_avail_after = sum(float(r["avail_after"]) for r in rows)
    if not total_avail_before or not total_avail_after:
        return []

    out: list[Contribution] = []
    for r in rows:
        ab, aa = float(r["avail_before"]), float(r["avail_after"])
        rb, ra = float(r["rev_before"]), float(r["rev_after"])
        w_before = ab / total_avail_before
        w_after = aa / total_avail_after
        revpar_before = (rb / ab) if ab else 0.0
        revpar_after = (ra / aa) if aa else 0.0

        mix = (w_after - w_before) * (revpar_before + revpar_after) / 2.0
        perf = (w_before + w_after) / 2.0 * (revpar_after - revpar_before)
        change = mix + perf

        out.append(Contribution(
            dimension=dimension,
            member=str(r["member"]),
            before=revpar_before,
            after=revpar_after,
            change=change,
            share_of_movement=(change / total_change) if total_change else 0.0,
            capacity_mix_effect=mix,
            performance_effect=perf,
            basis="revpar_inr",
        ))
    return sorted(out, key=lambda c: -abs(c.change))


def _attribute_revenue(dimension: str, label_sql: str, join_sql: str,
                       cur: tuple[dt.date, dt.date], base: tuple[dt.date, dt.date],
                       ) -> list[Contribution]:
    """Revenue change per member of a dimension that has NO capacity of its own.

    A channel cannot be given a RevPAR: rooms are not allocated to Booking.com, so
    there is no denominator to divide by. The honest move is to attribute the
    revenue movement and label it as such, rather than inventing a per-channel
    inventory so that a tidier number can be printed.

    Shares here are shares of the REVENUE movement and are reported against that
    denominator, never mixed into the RevPAR narrative.
    """
    rows = db.fetch_all(
        f"""
        WITH cur AS (
            SELECT {label_sql} AS member, sum(n.room_revenue_net_inr) AS rev,
                   count(*) FILTER (WHERE n.is_occupied) AS nights
            FROM mart.v_unit_night_enriched n
            {join_sql}
            WHERE n.stay_date BETWEEN :c1 AND :c2
            GROUP BY 1
        ),
        base AS (
            SELECT {label_sql} AS member, sum(n.room_revenue_net_inr) AS rev,
                   count(*) FILTER (WHERE n.is_occupied) AS nights
            FROM mart.v_unit_night_enriched n
            {join_sql}
            WHERE n.stay_date BETWEEN :b1 AND :b2
            GROUP BY 1
        )
        SELECT coalesce(c.member, b.member) AS member,
               coalesce(b.rev, 0) AS before, coalesce(c.rev, 0) AS after,
               coalesce(b.nights, 0) AS nights_before,
               coalesce(c.nights, 0) AS nights_after
        FROM cur c FULL OUTER JOIN base b USING (member)
        """,
        c1=cur[0], c2=cur[1], b1=base[0], b2=base[1],
    )
    total_change = sum(float(r["after"]) - float(r["before"]) for r in rows)

    out: list[Contribution] = []
    for r in rows:
        before, after = float(r["before"]), float(r["after"])
        change = after - before
        out.append(Contribution(
            dimension=dimension,
            member=str(r["member"]),
            before=before,
            after=after,
            change=change,
            share_of_movement=(change / total_change) if total_change else 0.0,
            nights_change=int(r["nights_after"]) - int(r["nights_before"]),
            basis="revenue_inr",
        ))
    return sorted(out, key=lambda c: -abs(c.change))


def _mix_vs_rate(cur: tuple[dt.date, dt.date], base: tuple[dt.date, dt.date]
                 ) -> dict[str, Any]:
    """Split the ADR movement into a rate effect and a channel-mix effect.

    Rate effect  -- each channel's own rate changed, holding last period's mix.
    Mix effect   -- the same rates, but a different share of nights per channel.

    The two sum to the total ADR change with a small interaction residual, which is
    reported rather than absorbed, because absorbing it is how a mix analysis
    quietly becomes a rate analysis.
    """
    rows = db.fetch_all(
        """
        WITH per AS (
            SELECT c.channel_name AS channel,
                   count(*) FILTER (WHERE n.stay_date BETWEEN :b1 AND :b2) AS n_base,
                   count(*) FILTER (WHERE n.stay_date BETWEEN :c1 AND :c2) AS n_cur,
                   sum(n.room_revenue_net_inr) FILTER (WHERE n.stay_date BETWEEN :b1 AND :b2) AS r_base,
                   sum(n.room_revenue_net_inr) FILTER (WHERE n.stay_date BETWEEN :c1 AND :c2) AS r_cur
            FROM mart.v_unit_night_enriched n
            JOIN mart.dim_channel c ON c.channel_key = n.channel_key
            WHERE n.is_occupied
            GROUP BY 1
        )
        SELECT channel, n_base, n_cur, r_base, r_cur FROM per
        """,
        c1=cur[0], c2=cur[1], b1=base[0], b2=base[1],
    )

    tot_base = sum(int(r["n_base"] or 0) for r in rows)
    tot_cur = sum(int(r["n_cur"] or 0) for r in rows)
    if not tot_base or not tot_cur:
        return {"available": False, "reason": "no occupied nights in one of the windows"}

    adr_base = sum(float(r["r_base"] or 0) for r in rows) / tot_base
    adr_cur = sum(float(r["r_cur"] or 0) for r in rows) / tot_cur

    rate_effect = 0.0
    mix_effect = 0.0
    detail = []
    for r in rows:
        nb, nc = int(r["n_base"] or 0), int(r["n_cur"] or 0)
        if not nb and not nc:
            continue
        rb = float(r["r_base"] or 0) / nb if nb else 0.0
        rc = float(r["r_cur"] or 0) / nc if nc else 0.0
        wb = nb / tot_base
        wc = nc / tot_cur
        # Rate at last period's weights; mix at last period's rates.
        rate_effect += wb * (rc - rb)
        mix_effect += (wc - wb) * rb
        detail.append({
            "channel": r["channel"],
            "share_before_pct": round(100 * wb, 1),
            "share_after_pct": round(100 * wc, 1),
            "adr_before_inr": round(rb, 2),
            "adr_after_inr": round(rc, 2),
        })

    total = adr_cur - adr_base
    return {
        "available": True,
        "adr_before_inr": round(adr_base, 2),
        "adr_after_inr": round(adr_cur, 2),
        "adr_change_inr": round(total, 2),
        "rate_effect_inr": round(rate_effect, 2),
        "mix_effect_inr": round(mix_effect, 2),
        "interaction_residual_inr": round(total - rate_effect - mix_effect, 2),
        "verdict": _mix_verdict(rate_effect, mix_effect),
        "by_channel": sorted(detail, key=lambda d: -abs(
            d["share_after_pct"] - d["share_before_pct"])),
    }


def _mix_verdict(rate: float, mix: float) -> str:
    if abs(rate) < 1e-9 and abs(mix) < 1e-9:
        return "no material ADR movement"
    if abs(rate) >= 2 * abs(mix):
        return "predominantly rate: channels changed what they charged"
    if abs(mix) >= 2 * abs(rate):
        return "predominantly mix: the same rates, a different channel blend"
    return "rate and mix both contributed materially"


# ---------------------------------------------------------------------------
def explain_revpar(current_start: dt.date, current_end: dt.date,
                   baseline_start: dt.date | None = None,
                   baseline_end: dt.date | None = None) -> Explanation:
    """Decompose a RevPAR movement between two windows.

    The baseline defaults to the immediately preceding window OF THE SAME LENGTH.
    Comparing a 30-day period against "last month" would compare 30 days with 31 and
    manufacture a 3% difference out of the calendar.
    """
    days = (current_end - current_start).days + 1
    if baseline_end is None:
        baseline_end = current_start - dt.timedelta(days=1)
    if baseline_start is None:
        baseline_start = baseline_end - dt.timedelta(days=days - 1)

    cur = _window(current_start, current_end)
    base = _window(baseline_start, baseline_end)

    change = cur["revpar"] - base["revpar"]
    change_pct = (100 * change / base["revpar"]) if base["revpar"] else 0.0

    # Symmetric split of the multiplicative identity. Sums to `change` exactly.
    d_occ = cur["occupancy"] - base["occupancy"]
    d_adr = cur["adr"] - base["adr"]
    occ_contrib = d_occ * (base["adr"] + cur["adr"]) / 2.0
    adr_contrib = d_adr * (base["occupancy"] + cur["occupancy"]) / 2.0

    components = [
        {
            "component": "occupancy",
            "before": round(100 * base["occupancy"], 2),
            "after": round(100 * cur["occupancy"], 2),
            "change_pp": round(100 * d_occ, 2),
            "revpar_contribution_inr": round(occ_contrib, 2),
            "share_of_movement_pct": (
                round(100 * occ_contrib / change, 1) if change else None
            ),
        },
        {
            "component": "adr",
            "before": round(base["adr"], 2),
            "after": round(cur["adr"], 2),
            "change_pct": (
                round(100 * d_adr / base["adr"], 2) if base["adr"] else None
            ),
            "revpar_contribution_inr": round(adr_contrib, 2),
            "share_of_movement_pct": (
                round(100 * adr_contrib / change, 1) if change else None
            ),
        },
    ]

    cwin, bwin = (current_start, current_end), (baseline_start, baseline_end)

    # Dimensions that own inventory are decomposed on RevPAR; those that do not are
    # attributed on revenue and kept in a separate list so the two can never be
    # ranked against each other as if they were the same quantity.
    revpar_dims = [
        _attribute_revpar("property", "p.property_name",
                          "JOIN mart.dim_property p ON p.property_key = n.property_key",
                          cwin, bwin, change),
        _attribute_revpar("day_type",
                          "CASE WHEN d.is_weekend THEN 'Weekend' ELSE 'Weekday' END",
                          "JOIN mart.dim_date d ON d.date_key = n.date_key",
                          cwin, bwin, change),
    ]
    channel_rows = _attribute_revenue(
        "channel", "c.channel_name",
        "JOIN mart.dim_channel c ON c.channel_key = n.channel_key", cwin, bwin,
    )

    drivers = [
        c for dim in revpar_dims for c in dim
        if abs(c.share_of_movement) >= MATERIAL_SHARE
    ]
    drivers.sort(key=lambda c: -abs(c.change))

    exp = Explanation(
        metric="revpar_inr",
        current=cur,
        baseline=base,
        change_abs=change,
        change_pct=change_pct,
        components=components,
        drivers=drivers[:8],
        channel_revenue=[c.as_dict() for c in channel_rows[:6]],
        capacity=_capacity_note(base, cur),
        mix_vs_rate=_mix_vs_rate(cwin, bwin),
        evidence_count=sum(len(d) for d in revpar_dims) + len(channel_rows),
    )
    _conclude(exp, occ_contrib, adr_contrib, change, revpar_dims)
    return exp


def _capacity_note(base: dict[str, Any], cur: dict[str, Any]) -> dict[str, Any]:
    """Did the denominator move? If so, that reframes the entire comparison.

    Added after the engine confidently blamed a property for an 18% RevPAR fall in a
    month when the portfolio had grown its sellable inventory by a third. RevPAR is
    revenue per AVAILABLE room, so opening rooms faster than demand fills them
    lowers it by arithmetic. That is a capital-deployment story, not a commercial
    failure, and an engine that cannot tell the two apart should not be trusted with
    either.
    """
    before, after = base["rooms_available"], cur["rooms_available"]
    pct = (100.0 * (after - before) / before) if before else 0.0
    material = abs(pct) >= 5.0
    return {
        "rooms_available_before": before,
        "rooms_available_after": after,
        "change_pct": round(pct, 1),
        "material": material,
        "note": (
            f"Sellable inventory changed {pct:+.1f}% between the two windows. "
            "RevPAR is revenue per available room, so part of this movement is "
            "capacity, not trading. Read the capacity-mix and performance effects "
            "separately below."
            if material else
            "Sellable inventory was effectively unchanged, so the comparison is "
            "like for like."
        ),
    }


def _conclude(exp: Explanation, occ_contrib: float, adr_contrib: float,
              change: float, dims: list[list[Contribution]]) -> None:
    """Attach the primary signal, confidence and caveats.

    All of this is derived from the numbers already computed. Nothing here consults
    a model, and nothing is phrased more strongly than the concentration supports.
    """
    if abs(exp.change_pct) < NOISE_FLOOR_PCT:
        exp.primary_signal = (
            f"RevPAR moved {exp.change_pct:+.1f}%, within normal variation. "
            "No driver analysis is warranted."
        )
        exp.confidence = "high"
        exp.caveats.append(
            f"Movements under {NOISE_FLOOR_PCT}% are treated as flat by design."
        )
        return

    direction = "fell" if change < 0 else "rose"
    lead = "occupancy" if abs(occ_contrib) >= abs(adr_contrib) else "rate"
    lead_share = (
        abs(occ_contrib if lead == "occupancy" else adr_contrib) / abs(change)
        if change else 0.0
    )

    parts = [f"RevPAR {direction} {abs(exp.change_pct):.1f}%, {lead}-led"]
    # Shares above 100% are legitimate here -- occupancy and rate can pull in
    # opposite directions, so one component can overshoot the net movement and the
    # other claw it back. Saying so beats printing "126%" unexplained.
    if lead_share > 1.0:
        parts[0] += (
            f" ({lead} alone accounts for {lead_share:.0%}; the other component "
            "moved the opposite way and offset part of it)"
        )
    else:
        parts[0] += f" ({lead_share:.0%} of the movement)"

    # CONCENTRATION IS MEASURED AGAINST GROSS MOVEMENT, NOT NET.
    #
    # Measured against the net it is unbounded and meaningless whenever
    # contributions offset. A 30-day window against another 30-day window contains a
    # different number of weekends, so weekday and weekend capacity-mix effects came
    # out at +260 and -231 INR -- almost entirely cancelling -- and the engine
    # announced "concentrated in Weekday (322% of the RevPAR movement)". Dividing by
    # the sum of absolute contributions keeps the figure inside 0-100% and makes it
    # mean what the word concentration implies.
    concentrations: list[tuple[str, str, float, float]] = []
    for dim in dims:
        gross = sum(abs(c.change) for c in dim)
        if not gross or not change:
            continue
        same = [c for c in dim if (c.change < 0) == (change < 0)]
        if not same:
            continue
        top = max(same, key=lambda c: abs(c.change))
        # Offsetting ratio: how much of the gross movement survives as net.
        survives = abs(change) / gross
        concentrations.append(
            (top.dimension, top.member, abs(top.change) / gross, survives)
        )
    concentrations.sort(key=lambda t: -t[2])

    if concentrations and concentrations[0][2] >= MATERIAL_SHARE:
        dim_name, member, shr, survives = concentrations[0]
        parts.append(
            f"concentrated in {member} ({shr:.0%} of the gross {dim_name} movement)"
        )
        if survives < 0.4:
            exp.caveats.append(
                f"Contributions across {dim_name} largely offset -- only "
                f"{survives:.0%} of the gross movement survives as net change. "
                "Treat the split as descriptive rather than as a driver."
            )
        exp.confidence = (
            "high" if shr >= HIGH_CONCENTRATION
            else "medium" if shr >= MEDIUM_CONCENTRATION
            else "low"
        )
    else:
        parts.append("with no single dominant driver -- the movement is spread "
                     "across segments")
        exp.confidence = "low"
        exp.caveats.append(
            "No contributor exceeded the materiality threshold. Naming a largest "
            "contributor here would be technically true and misleading."
        )

    # Capacity movement outranks everything else as an explanation.
    if exp.capacity.get("material"):
        mix_total = sum(
            c.capacity_mix_effect or 0.0
            for dim in dims for c in dim if c.dimension == "property"
        )
        parts.insert(1, (
            f"but sellable inventory changed {exp.capacity['change_pct']:+.1f}% "
            f"between the windows, contributing {mix_total:+,.0f} INR of the "
            "movement through capacity mix alone"
        ))
        exp.caveats.insert(0, exp.capacity["note"])
        # A capacity-driven movement is a weaker commercial claim, whatever the
        # concentration says.
        if exp.confidence == "high":
            exp.confidence = "medium"

    exp.primary_signal = "; ".join(parts) + "."

    if exp.mix_vs_rate and exp.mix_vs_rate.get("available"):
        exp.caveats.append(
            "ADR movement: " + exp.mix_vs_rate["verdict"] + "."
        )
    if exp.baseline["days"] != exp.current["days"]:
        exp.caveats.append(
            "Comparison windows differ in length; the change includes a calendar effect."
        )
    exp.caveats.append(
        "Attribution is descriptive, not causal. It identifies where the movement "
        "occurred, not why demand behaved as it did."
    )


def render(exp: Explanation) -> str:
    """Plain-text rendering, for the daily brief and the terminal."""
    lines: list[str] = []
    sign = "+" if exp.change_abs >= 0 else ""
    lines.append(f"WHY DID REVPAR CHANGE?")
    lines.append(f"  {exp.current['from']} .. {exp.current['to']}  vs  "
                 f"{exp.baseline['from']} .. {exp.baseline['to']}")
    lines.append("")
    lines.append(f"  RevPAR  {exp.baseline['revpar']:,.0f} -> {exp.current['revpar']:,.0f} INR"
                 f"   {sign}{exp.change_pct:.1f}%")
    lines.append("")
    for c in exp.components:
        if c["component"] == "occupancy":
            lines.append(f"    occupancy   {c['before']:.1f}% -> {c['after']:.1f}%"
                         f"  ({c['change_pp']:+.1f}pp)"
                         f"   contributes {c['revpar_contribution_inr']:+,.0f} INR")
        else:
            lines.append(f"    ADR         {c['before']:,.0f} -> {c['after']:,.0f} INR"
                         f"  ({c['change_pct']:+.1f}%)"
                         f"   contributes {c['revpar_contribution_inr']:+,.0f} INR")
    if exp.capacity.get("material"):
        lines.append("")
        lines.append(f"  CAPACITY    rooms available "
                     f"{exp.capacity['rooms_available_before']:,} -> "
                     f"{exp.capacity['rooms_available_after']:,}"
                     f"  ({exp.capacity['change_pct']:+.1f}%)")

    if exp.drivers:
        lines.append("")
        lines.append("  DRIVERS  (contribution to the RevPAR movement, INR)")
        lines.append("    dimension  member                             total"
                     "   capacity-mix  performance")
        for d in exp.drivers:
            mix = "" if d.capacity_mix_effect is None else f"{d.capacity_mix_effect:+12,.0f}"
            perf = "" if d.performance_effect is None else f"{d.performance_effect:+12,.0f}"
            lines.append(f"    {d.dimension:<10} {d.member:<30} "
                         f"{d.change:+9,.0f} {mix} {perf}   "
                         f"({100*d.share_of_movement:.0f}%)")

    if exp.channel_revenue:
        lines.append("")
        lines.append("  CHANNEL  (revenue movement, INR -- channels hold no inventory,")
        lines.append("            so they cannot be given a RevPAR)")
        for c in exp.channel_revenue:
            lines.append(f"    {c['member']:<30} {c['change']:+12,.0f}"
                         f"   {c['share_of_movement_pct']:6.0f}% of revenue change"
                         f"   {c['nights_change']:+5d} nights")

    if exp.mix_vs_rate and exp.mix_vs_rate.get("available"):
        m = exp.mix_vs_rate
        lines.append("")
        lines.append(f"  ADR: rate effect {m['rate_effect_inr']:+,.0f} INR, "
                     f"mix effect {m['mix_effect_inr']:+,.0f} INR")
        lines.append(f"       {m['verdict']}")
    lines.append("")
    lines.append(f"  PRIMARY SIGNAL   {exp.primary_signal}")
    lines.append(f"  CONFIDENCE       {exp.confidence}  "
                 f"({exp.evidence_count} segment comparisons)")
    for c in exp.caveats:
        lines.append(f"  NOTE             {c}")
    return "\n".join(lines)
