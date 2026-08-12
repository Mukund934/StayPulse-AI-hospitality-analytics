# Decision Log

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
**40 minutes to 101 minutes** — a
**2.5×** degradation — from 2026-03-09. SLA breach rate in that
segment moved from **13.9% to 73.7%** across
19 requests.

**Why it was nearly missed.** The portfolio-wide breach rate moved only
**+1.4 percentage points** over the same
period. At the blended level this is invisible. It is visible only when segmented by
property *and* day-part — either dimension alone flattens it back out.

**Confidence: High.** The effect is 2.5× on a segment with
19 observations, it is sustained rather than a spike, it is
localised to one property and one shift, and the anomaly detector independently
flagged it at |z| > 3 on multiple days.

**Corroboration.** Requests that breached SLA carry a CSAT of
**2.96** against **4.43** for those that did not — so
this degradation is reaching guests, not just the ops dashboard.

**Recommendation.** Move one housekeeping FTE from the morning to the evening block
at Koramangala and re-measure after 14 days.

**Expected effect.** Evening breach rate returns toward the
13.9% pre-change level. Not quantified in rupees: service
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

**Finding.** **42 bookings** across
**29 days** (2026-01-17 → 2026-03-14)
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
**0** over the nine days 2025-11-14 → 2025-11-22,
against a normal rate of about **1.61 per day**.

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

**Finding.** ADR moved **₹21** across
2026-05-01 → 2026-06-30 versus the preceding 61 days, while
corporate share of stays rose **+18.5 percentage
points**.

**Diagnosis.** This is **mix, not rate**. Corporate business books lower nightly
rates for longer stays. Decomposing the ADR change into a within-channel rate effect
and a between-channel mix effect
(`sql/analysis/02_adr_decline_rate_or_mix.sql`) attributes it to mix. Critically,
**RevPAR moved ₹124** — revenue per available unit did not
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
| `DIRECT` | direct | 2,259 | ₹4,556 | ₹4,556 |
| `CORP` | corporate | 5,532 | ₹4,474 | ₹4,474 |
| `AIRBNB` | ota | 1,163 | ₹4,565 | ₹3,757 |
| `BDC` | ota | 1,586 | ₹4,512 | ₹3,607 |
| `AGODA` | ota | 689 | ₹4,495 | ₹3,540 |
| `MMT` | ota | 1,817 | ₹4,501 | ₹3,439 |
| `B2B-HR` | hourly | 380 | ₹1,278 | ₹1,097 |

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

**Finding.** **135 negative operational aspects** sit inside
**132 reviews rated 4.0 or higher** (mean rating
4.51). Most common: `maintenance` (22), `check_in` (17), `other` (16).

**Why sentiment analysis would have found none of them.**
**96.1%** of rated reviews are 4.0 or above. A
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
**25.8%** on raw guest records, which was
read as a loyalty problem and a retention campaign was proposed.

**What reversed it.** The guest table contains duplicate profiles for the same
person — the same phone written as `+91XXXXXXXXXX` and `0XXXXXXXXXX`, the same email
in different case. Deterministic identity resolution on the normalised phone, then
normalised email, collapses
**2,847** raw records to
**2,813** identities and raises the measured
repeat rate to **26.6%**.

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
