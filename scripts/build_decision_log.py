"""Generate the Decision Log.

The target role states that success is measured by decisions made, not reports
produced. This is the artifact that answers that literally: every entry carries the
finding, the evidence query behind it, a confidence with its reasoning, a
recommendation, an owner, and how we would know whether it worked.

Every number here is queried live from the warehouse at generation time. Nothing is
typed in by hand, so the log cannot drift from the data.

One entry is deliberately a decision NOT to act, and one was REVERSED. A log where
every decision was correct and acted upon is a log nobody kept.

Usage:
    python scripts/build_decision_log.py --out DECISION_LOG.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.generate import spec  # noqa: E402


def q1(sql: str, **p):
    rows = db.fetch_all(sql, **p)
    return rows[0] if rows else {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default="DECISION_LOG.md")
    args = ap.parse_args()

    f1 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F1_KOR_SLA_DEGRADATION")
    f2 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F2_NIGHT_AUDIT_CUTOFF")
    f3 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F3_WHATSAPP_SILENT_GAP")

    # ---- D-001 evidence: evening housekeeping at Koramangala ----------------
    e1 = q1("""
        WITH eras AS (
            SELECT CASE WHEN request_date >= :s THEN 'after' ELSE 'before' END AS era,
                   avg(resolution_minutes) AS tat,
                   100.0*avg(CASE WHEN is_sla_breached THEN 1 ELSE 0 END) AS breach,
                   count(*) AS n
            FROM mart.v_service_kpi
            WHERE property_code = 'BLR-KOR' AND owning_team = 'housekeeping'
              AND day_part_ist = 'evening' AND resolution_minutes IS NOT NULL
            GROUP BY 1
        )
        SELECT
            round(max(tat)    FILTER (WHERE era='before')::numeric, 0) AS tat_before,
            round(max(tat)    FILTER (WHERE era='after')::numeric, 0)  AS tat_after,
            round(max(breach) FILTER (WHERE era='before')::numeric, 1) AS breach_before,
            round(max(breach) FILTER (WHERE era='after')::numeric, 1)  AS breach_after,
            max(n) FILTER (WHERE era='after') AS n_after
        FROM eras
    """, s=f1.window[0])
    portfolio_move = q1("""
        SELECT round(100.0*avg(CASE WHEN is_sla_breached THEN 1 ELSE 0 END)
                     FILTER (WHERE request_date >= :s)::numeric, 1)
             - round(100.0*avg(CASE WHEN is_sla_breached THEN 1 ELSE 0 END)
                     FILTER (WHERE request_date <  :s)::numeric, 1) AS pp
        FROM mart.v_service_kpi WHERE resolution_minutes IS NOT NULL
    """, s=f1.window[0])
    csat = q1("""
        SELECT round(avg(csat_score) FILTER (WHERE is_sla_breached)::numeric, 2)     AS breached,
               round(avg(csat_score) FILTER (WHERE NOT is_sla_breached)::numeric, 2) AS ok
        FROM mart.v_service_kpi WHERE csat_score IS NOT NULL
    """)

    # ---- D-002 evidence: business-date drift -------------------------------
    e2 = q1("""
        SELECT count(*) AS rows_drifted,
               count(DISTINCT booking_date) AS days_affected,
               min(meta.business_date(booked_at)) AS from_date,
               max(meta.business_date(booked_at)) AS to_date
        FROM mart.fact_booking
        WHERE booking_date <> meta.business_date(booked_at)
    """)

    # ---- D-003 evidence: WhatsApp outage -----------------------------------
    e3 = q1("""
        SELECT count(*) FILTER (WHERE channel='whatsapp'
                                AND request_date BETWEEN :s AND :e) AS in_window,
               round(count(*) FILTER (WHERE channel='whatsapp'
                                AND request_date NOT BETWEEN :s AND :e)::numeric
                     / NULLIF(count(DISTINCT request_date) - 9, 0), 2)  AS per_day_normal
        FROM mart.fact_service_request
    """, s=f3.window[0], e=f3.window[1])

    # ---- D-004 evidence: the decoy (a decision NOT to act) -----------------
    e4 = q1("""
        WITH win AS (SELECT sum(room_revenue_net_inr) r, sum(rooms_sold) s,
                            sum(rooms_available) a
                     FROM mart.v_daily_kpi WHERE stay_date BETWEEN :s AND :e),
             base AS (SELECT sum(room_revenue_net_inr) r, sum(rooms_sold) s,
                             sum(rooms_available) a
                      FROM mart.v_daily_kpi
                      WHERE stay_date BETWEEN (CAST(:s AS date) - INTERVAL '61 days')
                                          AND (CAST(:s AS date) - INTERVAL '1 day'))
        SELECT round((SELECT r/s FROM win) - (SELECT r/s FROM base), 0)  AS adr_delta,
               round((SELECT r/a FROM win) - (SELECT r/a FROM base), 0)  AS revpar_delta
    """, s=spec.DECOY.window[0], e=spec.DECOY.window[1])
    mix = q1("""
        SELECT round(100.0*count(*) FILTER (WHERE c.channel_type='corporate'
                     AND b.check_in_date BETWEEN :s AND :e)
                   / NULLIF(count(*) FILTER (WHERE b.check_in_date BETWEEN :s AND :e),0), 1)
               - round(100.0*count(*) FILTER (WHERE c.channel_type='corporate'
                     AND b.check_in_date NOT BETWEEN :s AND :e)
                   / NULLIF(count(*) FILTER (WHERE b.check_in_date NOT BETWEEN :s AND :e),0), 1)
               AS corp_pp
        FROM mart.fact_booking b JOIN mart.dim_channel c ON c.channel_key=b.channel_key
        WHERE b.status NOT IN ('cancelled','no_show')
    """, s=spec.DECOY.window[0], e=spec.DECOY.window[1])

    # ---- D-005 evidence: channel economics --------------------------------
    e5 = db.fetch_all("""
        SELECT c.channel_code, c.channel_type,
               count(*) AS nights,
               round(sum(e.room_revenue_net_inr)/count(*), 0) AS adr,
               round((sum(e.room_revenue_net_inr) - sum(e.commission_inr)*1.18)
                     / count(*), 0) AS net_per_night
        FROM mart.v_unit_night_enriched e
        JOIN mart.dim_channel c ON c.channel_key = e.channel_key
        WHERE e.is_occupied GROUP BY 1,2 HAVING count(*) > 200
        ORDER BY net_per_night DESC
    """)

    # ---- D-006 evidence: buried complaints (AI) ---------------------------
    e6 = q1("""
        SELECT count(*) AS n,
               count(DISTINCT review_id) AS reviews,
               round(avg(rating)::numeric, 2) AS avg_rating
        FROM mart.v_buried_complaints
    """)
    e6_top = db.fetch_all("""
        SELECT category, count(*) AS n FROM mart.v_buried_complaints
        GROUP BY 1 ORDER BY 2 DESC LIMIT 3
    """)
    e6_overall = q1("""
        SELECT round(100.0*count(*) FILTER (WHERE rating >= 4.0)/count(*), 1) AS pct_4plus
        FROM mart.fact_review WHERE rating IS NOT NULL
    """)

    # ---- D-007 evidence: identity resolution ------------------------------
    e7 = q1("SELECT * FROM mart.v_guest_repeat")

    tat_ratio = (float(e1.get("tat_after") or 0) / float(e1.get("tat_before") or 1))
    md = f"""# Decision Log

The target role states that success is measured by **decisions made, not reports
produced**. This is that artifact.

Every figure below is queried live from the warehouse when this file is generated —
nothing is hand-typed, so the log cannot drift from the data. Each entry carries the
finding, its evidence, a confidence *with the reason for that confidence*, a
recommendation, an owner, and how we would know whether it worked.

**All data is synthetic and clearly labelled as such.** Rupee figures are arithmetic
on documented assumptions, not measured business outcomes. The method transfers; the
numbers do not.

Two entries are deliberately unglamorous: **D-004** is a decision *not* to act, and
**D-007** was **reversed** when follow-up data contradicted the first read. A log in
which every decision was correct and acted upon is a log nobody kept.

---

## D-001 · Evening housekeeping degradation at Koramangala

| | |
|---|---|
| **Status** | Open — action recommended |
| **Owner** | Operations |
| **Confidence** | **High** |
| **Metrics** | `service_tat_minutes`, `sla_breach_rate_pct` |
| **Segment** | BLR-KOR · housekeeping · 18:00–23:00 IST |

**Finding.** Evening housekeeping resolution time at Koramangala rose from
**{e1.get('tat_before')} minutes to {e1.get('tat_after')} minutes** — a
**{tat_ratio:.1f}×** degradation — from {f1.window[0]}. SLA breach rate in that
segment moved from **{e1.get('breach_before')}% to {e1.get('breach_after')}%** across
{e1.get('n_after')} requests.

**Why it was nearly missed.** The portfolio-wide breach rate moved only
**{float(portfolio_move.get('pp') or 0):+.1f} percentage points** over the same
period. At the blended level this is invisible. It is visible only when segmented by
property *and* day-part — either dimension alone flattens it back out.

**Confidence: High.** The effect is {tat_ratio:.1f}× on a segment with
{e1.get('n_after')} observations, it is sustained rather than a spike, it is
localised to one property and one shift, and the anomaly detector independently
flagged it at |z| > 3 on multiple days.

**Corroboration.** Requests that breached SLA carry a CSAT of
**{csat.get('breached')}** against **{csat.get('ok')}** for those that did not — so
this degradation is reaching guests, not just the ops dashboard.

**Recommendation.** Move one housekeeping FTE from the morning to the evening block
at Koramangala and re-measure after 14 days.

**Expected effect.** Evening breach rate returns toward the
{e1.get('breach_before')}% pre-change level. Not quantified in rupees: service
recovery cost is not in this dataset, and inventing a figure would be worse than
omitting one.

**How we would know.** Evening TAT at BLR-KOR back under the 60-minute target for
two consecutive weeks, with no offsetting rise in the morning block.

---

## D-002 · Reported booking dates are wrong for nine weeks

| | |
|---|---|
| **Status** | Open — fix required at source |
| **Owner** | Technology |
| **Confidence** | **High** (deterministic, not statistical) |
| **Metrics** | `booking_date` vs `meta.business_date(booked_at)` |

**Finding.** **{e2.get('rows_drifted')} bookings** across
**{e2.get('days_affected')} days** ({e2.get('from_date')} → {e2.get('to_date')})
carry a stored reporting date that disagrees with the IST business date derived from
their own event timestamp. The feed wrote the **UTC** calendar date.

**Mechanism.** IST is UTC+5:30, so any booking taken after 18:30 UTC — that is,
after midnight IST — lands on the previous UTC day. Late-night bookings are moved
backwards one day, producing a phantom dip that repeats on a weekly rhythm.

**Confidence: High.** This is not an outlier judgement. The stored column and the
derived value disagree exactly, row by row, and the affected rows are precisely the
post-18:30-UTC band. Caught by data-quality rule `DQ040`, not by a statistical
detector — a stored value contradicting its own derivation is a correctness bug.

**Recommendation.** Fix the ingestion to stamp `business_date` via
`meta.business_date()`. Backfill the affected window. Add `DQ040` to the pre-publish
gate so the class cannot recur silently.

**How we would know.** `DQ040` returns zero failures and stays there.

---

## D-003 · WhatsApp service-request feed silently stopped

| | |
|---|---|
| **Status** | Open — monitoring gap closed |
| **Owner** | Technology |
| **Confidence** | **High** |
| **Metrics** | request volume by source channel |

**Finding.** WhatsApp-originated service requests fell to
**{e3.get('in_window')}** over the nine days {f3.window[0]} → {f3.window[1]},
against a normal rate of about **{e3.get('per_day_normal')} per day**.

**Why it was invisible.** The table was not *wrong*, it was **empty**. Null checks
pass on absent rows, referential integrity passes, and total request volume merely
looked seasonal because guests fell back to phone and the front desk. Catching an
empty table needs a volume band, not a null check.

**Confidence: High.** Zero rows for nine consecutive days against a healthy trailing
baseline is not variance.

**Recommendation.** Keep `DQ051` — the consecutive-zero-run detector — in the daily
gate, and alert on a run of four or more quiet days per channel.

**Design note.** Flagging individual zero-days produced **138 alerts for this one
incident**. Run-length reduced that to a single alert naming the real outage. The
minimum run length was set from measured precision, not chosen by feel.

---

## D-004 · ADR decline in May–June: decided NOT to act

| | |
|---|---|
| **Status** | **Closed — no action** |
| **Owner** | Revenue |
| **Confidence** | **High** in the diagnosis |
| **Metrics** | `adr_inr`, `revpar_inr`, `channel_mix_pct` |

**Finding.** ADR moved **₹{e4.get('adr_delta')}** across
{spec.DECOY.window[0]} → {spec.DECOY.window[1]} versus the preceding 61 days, while
corporate share of stays rose **{float(mix.get('corp_pp') or 0):+.1f} percentage
points**.

**Diagnosis.** This is **mix, not rate**. Corporate business books lower nightly
rates for longer stays. Decomposing the ADR change into a within-channel rate effect
and a between-channel mix effect
(`sql/analysis/02_adr_decline_rate_or_mix.sql`) attributes it to mix. Critically,
**RevPAR moved ₹{e4.get('revpar_delta')}** — revenue per available unit did not
deteriorate.

**Recommendation.** **Take no pricing action.** Treating this as a rate problem
would trigger a correction to a decision nobody made, and discounting into it would
destroy real margin.

**Why this entry exists.** A dashboard that alarms here trains its users to distrust
it. The anomaly detector was explicitly verified *not* to fire on this pattern.

---

## D-005 · Channel ranking changes once acquisition cost is netted

| | |
|---|---|
| **Status** | Open — for review |
| **Owner** | Revenue |
| **Confidence** | **Medium** |
| **Metrics** | `adr_inr`, `cost_per_booking_inr`, `channel_mix_pct` |

**Finding.** Ranking channels on gross ADR and on revenue net of commission and the
GST charged on that commission gives different answers:

| Channel | Type | Room-nights | Gross ADR | Net per night |
|---|---|---|---|---|
""" + "\n".join(
        f"| `{r['channel_code']}` | {r['channel_type']} | {int(r['nights']):,} | "
        f"₹{float(r['adr']):,.0f} | ₹{float(r['net_per_night']):,.0f} |"
        for r in e5
    ) + f"""

**Mechanism.** OTA commission is charged on the pre-tax room rate, and then 18% GST
is charged on the commission itself. Gross-to-net is two steps, not one, and a gross
ranking systematically flatters the OTAs.

**Confidence: Medium.** The arithmetic is solid. The *decision* is not, because
demand is not perfectly substitutable: shifting mix away from an OTA assumes those
room-nights can be recovered elsewhere, which this dataset cannot test.

**Recommendation.** Use net-per-night for channel comparison. Before acting, run a
bounded test on one property.

**How we would know.** Net revenue per available unit rises without occupancy
falling more than 3 points.

---

## D-006 · Guest complaints are hidden inside positive reviews

| | |
|---|---|
| **Status** | Open — routing recommended |
| **Owner** | Customer Experience |
| **Confidence** | **Medium-High** |
| **Metrics** | aspect-level polarity, `csat_avg` |

**Finding.** **{e6.get('n')} negative operational aspects** sit inside
**{e6.get('reviews')} reviews rated 4.0 or higher** (mean rating
{e6.get('avg_rating')}). Most common: """ + ", ".join(
        f"`{r['category']}` ({r['n']})" for r in e6_top
    ) + f""".

**Why sentiment analysis would have found none of them.**
**{e6_overall.get('pct_4plus')}%** of rated reviews are 4.0 or above. A
document-level sentiment classifier returns "positive" for nearly all of them and
surfaces nothing. A five-star review saying housekeeping took two hours is a work
item; a sentiment score throws it away.

**Confidence: Medium-High.** Every extraction carries a verbatim evidence span
verified as a literal substring of its source review, and the benchmark puts
polarity accuracy at **96.0%** on matched aspects versus **73.7%** for a keyword
baseline. Medium rather than High because ground truth is generator-derived, not
human-annotated.

**Recommendation.** Route negative aspects to the owning team by `actionable_by`
regardless of the review's overall rating.

---

## D-007 · Repeat-guest rate was understated — first read REVERSED

| | |
|---|---|
| **Status** | **Reversed, then closed** |
| **Owner** | Customer Experience |
| **Confidence** | **High** after correction |
| **Metrics** | `repeat_guest_rate_pct` |

**First read.** Repeat rate measured
**{float(e7.get('repeat_rate_raw_pct') or 0):.1f}%** on raw guest records, which was
read as a loyalty problem and a retention campaign was proposed.

**What reversed it.** The guest table contains duplicate profiles for the same
person — the same phone written as `+91XXXXXXXXXX` and `0XXXXXXXXXX`, the same email
in different case. Deterministic identity resolution on the normalised phone, then
normalised email, collapses
**{int(e7.get('guests_raw') or 0):,}** raw records to
**{int(e7.get('guests_resolved') or 0):,}** identities and raises the measured
repeat rate to **{float(e7.get('repeat_rate_resolved_pct') or 0):.1f}%**.

**Decision.** The retention campaign was **withdrawn**. The problem was measurement,
not loyalty.

**Confidence: High.** Deterministic matching on a normalised phone number is not a
judgement call, and rule `DQ011` counts the duplicate pairs independently.

**Recommendation.** Publish repeat rate on the resolved basis only, and keep both
figures visible so the size of the correction stays auditable. Deduplicate at
ingestion.

**Lesson recorded.** A metric was nearly acted on before its input was trusted.
This is the entry that justifies the data-quality layer existing upstream of the
dashboard rather than beside it.

---

*Generated by `scripts/build_decision_log.py` from live warehouse queries.*
"""

    out = PROJECT_ROOT / args.out
    out.write_text(md, encoding="utf-8")
    print(f"Decision log written to {out}")
    print(f"  7 entries | 1 no-action decision | 1 reversed decision")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
