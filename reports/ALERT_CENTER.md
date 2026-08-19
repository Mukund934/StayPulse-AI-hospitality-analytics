# Alert Center and Opportunity Radar

_Generated 2026-08-19T05:28:09+00:00, as of 2026-08-11._

## Four feeds, one queue, no invented severity

Anomalies, data-quality failures, SLA breaches and pace need-dates each
existed already and each had its own shape. This puts them in one queue.
It does **not** put them on one scale.

The tempting move is a severity number applied across every source. It
cannot be computed here: a robust z on ADR, a percentage of failing rows,
an SLA breach rate and a room-night shortfall are incommensurable, and
mapping them onto a shared 1-5 scale needs exchange rates nobody has
measured. The arbitrariness would then be hidden inside an integer that
looks authoritative. Every alert reports its own feed's measure with its
units named, and a test fails if a shared severity field appears.

What **is** comparable is actionability — what a person can still do:

| Band | Meaning | Alerts |
|---|---|---:|
| `act_now` | a future stay date; the book can still move | 11 |
| `investigate` | already happened; only an explanation is available | 5 |
| `standing` | a condition with no single date, true until fixed | 10 |

| Source | Alerts |
|---|---:|
| pace | 11 |
| data_quality | 9 |
| anomaly | 5 |
| service_sla | 1 |

## The pace feed's holiday bias, and a ratio that did not survive

This is the finding worth keeping, and what makes it worth keeping is that
the first version of it was wrong.

The mechanism is real. The pace benchmark compares a stay date against the
last 8 comparable same-weekday dates, which exclude the holiday. F-101
measured genuine suppression on those dates — Diwali −10.5pp, Christmas
−20.4pp — so part of a shortfall on a holiday-adjacent date is plausibly
the holiday rather than a demand problem.

The **magnitude** is where it went wrong. Pooled across origins:

| Measure | Value |
|---|---:|
| Stay dates scored across 8 origins | 177 |
| …of which holiday-adjacent (base rate) | 39.0% |
| Behind-pace alerts raised | 19 |
| …of which holiday-adjacent | 73.7% |
| Apparent over-representation | 1.89× |

That last row reads like a finding. It is an artefact.

### Why the pooled ratio is withdrawn

Per origin, the base rate ranges from 0% to 100%. The pooled comparison
mixes windows that were entirely holiday-adjacent with windows containing
no holiday at all.

| As of | Scored | Holiday-adjacent (base) | Behind-pace | …on holiday dates |
|---|---:|---:|---:|---:|
| 2026-08-11 | 11 | 100.0% | 11 | 100.0% |
| 2026-07-02 | 24 | 0.0% | 1 | 0.0% |
| 2026-05-23 | 22 | 0.0% | 1 | 0.0% |
| 2026-03-04 | 24 | 100.0% | 2 | 100.0% |
| 2025-12-14 | 28 | 71.4% | 3 | 0.0% |
| 2025-11-04 | 23 | 17.4% | 1 | 100.0% |

One origin — **2026-08-11**, sitting immediately before
Independence Day — had a **100.0%** base rate: every
scored date in its window was holiday-adjacent. It contributed
**11 of the 19 alerts**
(57.9%), all holiday-adjacent — which is
exactly what a 100% base rate produces, and evidence of nothing. Another
origin ran a 71.4% base rate and raised *zero* holiday-adjacent alerts,
pointing the opposite way.

Excluding the dominant origin: **37.5%**
of alerts against a **34.9%** base rate.
No effect.

This is Simpson's paradox, and it is the **third time this project has hit
the same class of error**. PART U.2 records pooled holiday multipliers
coming out above 1 for holidays that suppress demand; U.3 records
pseudo-replication in the confidence caveat. Pooling across units with
very different base rates is unsound here in whichever direction it
happens to flatter.

So the alert qualifier names the **mechanism**, which is measured, and not
a **magnitude**, which is not. No ratio is published in the API response.

### They are qualified, not suppressed

Dropping them would be the easy fix and the wrong one. A holiday explains
*part* of a shortfall, not all of it, and these are dates where occupancy
is already fragile — silently removing the alert would hide genuine
weakness exactly where it costs most.

Currently 11 of 26 alerts carry that
qualifier.

## The SLA threshold, and a wrong first answer

The first version of this module flagged cells breaching on 25% or more of
requests, with a comment asserting that sat above the bulk of the
distribution. Measured, the distribution runs **6.5% to 22.5%** with a
median of 16.7% across 11 qualifying cells. A 25% cut would have matched
**nothing**, and the Alert Center would have advertised four feeds while one
silently contributed zero.

This warehouse defines `sla_minutes` per request type but no acceptable
breach *rate* anywhere — not in the metric registry, the DQ rules or the
generator. So an absolute threshold is invented by definition. Cells are
now judged against their peers using the dual gate this codebase already
applies elsewhere: at or above the p75 of comparable cells, **and** at
least 20 breaches in absolute terms. "Bad" means worse than comparable
cells, not worse than a contract, and the output says so.

## Opportunity Radar

As of 2026-07-02: **13** stay
dates running ahead of their own curve.

Pace analysis that only surfaces weak dates is half an instrument. A date
filling unusually early is the one where the remaining inventory was priced
before anyone knew demand would be strong.

**No signal names a price.** There is no competitor rate feed and no price
elasticity in this warehouse, so a rate recommendation would be an opinion
with a number attached. A test enforces it.

## Limitations

- **No cross-source severity, by design.** Compare within a source, never
  across. The queue is ordered by actionability, not by alarm.
- **The pace feed is holiday-blind** and over-represents holiday-adjacent
  dates by the ratio above. Qualified in place rather than corrected: a
  holiday-aware pace benchmark would need a per-holiday effect estimate,
  and F-102 established that this dataset does not support one — most
  holidays occur once in eighteen months.
- **SLA and data-quality alerts are standing conditions**, aggregated over
  the whole record. A single bad shift does not appear here and should not;
  that is what the anomaly feed is for.
- **Anomaly alerts are capped to the trailing 45 days.** Older detections are history rather
  than a queue, and stay in `reports/anomalies.md`.
