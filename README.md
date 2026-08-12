# StayPulse — one consolidated view of three serviced-apartment properties, run from 1,200 km away

**A hospitality analytics warehouse where every number carries its own definition, every AI claim quotes its source, and every finding ends in a decision.**

A serviced-apartment operator running Bengaluru properties from a back office in another state has one structural problem: nobody in the back office can walk into a unit at 7am and see that two apartments are dirty and a guest is angry. The dashboard is not a reporting convenience — it is the operation's only sense organ. And a sense organ that reports the same thing two different ways is worse than none.

This project builds that view end to end: **18 months, 3 properties, 40 units, 46,700 rows** across bookings, unit-nights, payments, service requests, guest reviews and inventory — plus the governance layer that makes the numbers trustworthy.

> **All data here is synthetic and labelled as such.** It is generated from a documented, seeded model of Indian serviced-apartment operations. Rupee figures are arithmetic on stated assumptions, not measured business outcomes. **The method transfers; the numbers do not.**

---

## Headline numbers

| | |
|---|---|
| Occupancy | **75.8%** |
| ADR | **₹4,415** |
| RevPAR | **₹3,347** |
| Net room revenue (18 months) | **₹6.01 crore** |
| Data-quality score | **69.1 / 100** — low *by design*, the data carries deliberate defects |
| Tests | **51 passing** |

`RevPAR = ADR × Occupancy` reconciles to a residual of **0.000000**, which is only possible because all three read one table with one denominator.

---

## Three findings the system discovered

**1. Evening housekeeping at one property degraded 2.5× — and the portfolio average moved 0.9pp.**
Resolution time at Koramangala's 18:00–23:00 block went from 30 to 76 minutes. At the blended level this is invisible. It is visible only when segmented by property **and** day-part; either dimension alone flattens it back out. Requests that breached SLA carry measurably lower CSAT, so it reached guests, not just the ops sheet.

**2. Nine weeks of reported booking dates were silently wrong.**
A feed wrote the **UTC** calendar date instead of the IST business date. Since IST is UTC+5:30, every booking taken after midnight IST moved back one day, producing a phantom dip on a weekly rhythm. Caught by comparing the stored column against its own derivation — not by a statistical detector, because a value contradicting its own derivation is a correctness bug, not an outlier.

**3. A service-request integration stopped for nine days and nothing failed.**
The table was not *wrong*, it was **empty**. Null checks passed, referential integrity passed, and total volume merely looked seasonal because guests fell back to phone. Catching an empty table needs a volume band, not a null check.

**And one deliberate non-finding.** ADR fell across May–June while corporate mix rose 18.6pp. That is **mix, not rate** — RevPAR held. The recommendation was *take no pricing action*. A dashboard that alarms here sends Operations chasing a decision nobody made, and the anomaly detector is explicitly tested not to fire on it.

Full write-ups with evidence, confidence and owners: **[DECISION_LOG.md](DECISION_LOG.md)**

---

## How this maps to the role

| What the role asks for | Where it is evidenced |
|---|---|
| Consolidate PMS / CRM / payments / WhatsApp into one reliable base | [`migrations/`](migrations/) — 4 layered schemas, 14 fact & dimension tables |
| “One number means one thing” | [METRICS.md](METRICS.md) — 16 metrics, each with grain, date basis, inclusions, exclusions, caveats |
| Occupancy, ADR, RevPAR, channel mix, cancellations, TAT, SLA, CSAT, repeat rate, cost per booking | All 16 registered in `meta.metric_definition` and executed by the semantic layer |
| SQL with joins, CTEs, window functions | [`sql/analysis/`](sql/analysis/) — 6 analyses, each answering one business question |
| Investigate anomalies, establish root cause with data | [reports/anomalies.md](reports/anomalies.md) — detection, attribution, ground-truth verification |
| Alerts and exception reports | 29 quality rules + a scheduled brief + a staleness watchdog |
| LLMs on unstructured data at scale | [`src/staypulse/ai/`](src/staypulse/ai/) — aspect-based extraction, closed taxonomy |
| **“Validate AI output against source data”** | Every aspect carries a verbatim evidence span asserted to be a literal substring of its source. [reports/ai_evaluation.md](reports/ai_evaluation.md) |
| Find gaps, duplicates, mismatches, broken integrations | [`src/staypulse/quality/`](src/staypulse/quality/) — scored on **recall per defect class**, 10/10 caught |
| Automate recurring reports | [`.github/workflows/daily_brief.yml`](.github/workflows/daily_brief.yml) → [reports/LATEST_BRIEF.md](reports/LATEST_BRIEF.md) |
| Insight, **confidence level**, recommended action | Every Decision Log entry carries all three, with the reason for the confidence |
| Excel / Power Query | See *Known gaps* — honestly not yet built |

---

## Architecture

```
  SOURCE SIMULATION            WAREHOUSE (PostgreSQL 17.6)          CONSUMERS
  ┌──────────────────┐        ┌──────────────────────────┐        ┌──────────────┐
  │ seeded generator │───────▶│  raw → staging → mart    │───────▶│ SQL analyses │
  │  · bookings      │  COPY  │                          │        │ daily brief  │
  │  · payments      │        │  fact_unit_night ◀── the │        │ decision log │
  │  · tickets       │        │  atomic grain            │        │ Power BI     │
  │  · reviews       │        │                          │        └──────────────┘
  │  · inventory     │        │  meta.metric_definition  │
  └──────────────────┘        │  meta.dq_rule / dq_result│        ┌──────────────┐
                              │  meta.business_date()    │───────▶│ Gemini ABSA  │
                              │  meta.gst_rate           │        │  ↓ validate  │
                              └──────────────────────────┘        │  ↓ quarantine│
                                                                  └──────────────┘
```

**The load-bearing decision** is `fact_unit_night`: one row per unit per night, occupied or not. Occupancy's denominator and ADR's numerator come from the same table, so `RevPAR = ADR × Occupancy` is an identity a test can assert rather than a coincidence to hope for. The half-open interval `[check_in, check_out)` is applied *once*, here — re-deriving it per query is how the departure night gets counted and room-nights inflate by roughly 1/ALOS (~33% at a 3-night stay).

**The second** is `meta.business_date()` — a single `IMMUTABLE` function converting UTC instants to IST calendar dates. The server runs UTC and the business runs IST, so casting a timestamp to a date misassigns every late-night event.

---

## Metric governance

`meta.metric_definition` is not documentation *about* the metrics — it is the registry the semantic layer **executes**, and [METRICS.md](METRICS.md) is generated from it, so the published dictionary cannot drift from what the warehouse computes.

`date_basis` is `CHECK`-constrained. A metric physically cannot be registered without declaring which date it is measured on. That matters more than it sounds:

> **June 2026 revenue, three legitimately correct answers.** ₹4,385,337 by stay date (what Operations earned) · ₹4,556,997 by booking date (what Marketing sold) · ₹4,567,681 by payment date (what Finance collected). A **₹182,344 spread** on the same month. None is wrong. This is why the constraint exists.

Two metrics are published **two ways on purpose**, because both readings are defensible and they disagree:

- **Occupancy** — operational basis (out-of-order units removed from availability, 75.8%) and benchmark basis (full physical inventory). The 1.67pp gap *is* inventory lost to OOO, which is itself actionable.
- **ADR** — including and excluding hourly microstays. A two-hour booking billed at ~28% of a nightly rate destroys ADR if counted as a nightly stay.

GST is resolved per night by **both** stay date and nightly rate, spanning the 22 Sep 2025 restructure (12% slab abolished; 5% without ITC at or below ₹7,500, 18% with ITC above). Blended effective rate is **7.92%** — exactly how much ADR would be overstated if computed off gross invoice values, which is the standard error on Indian folios.

---

## Data quality — scored on recall, not on running

29 declarative rules across the six DAMA dimensions. Each carries its expectation, severity and tolerated failure rate, and executes as one SQL statement. A rule that **errors** is recorded as failed, never skipped — a broken check must not read as healthy.

**All 10 planted defect classes are caught**: duplicate bookings, duplicate guest identities, payment mismatches, orphan references, missing contacts, impossible stay dates, invalid ratings, inventory imbalances, business-date drift, silent integration gaps.

Two rules are worth reading:

**`DQ051`** detects a silently dead feed using **gaps-and-islands** (the difference of two `row_number()`s is constant within a consecutive run). Flagging individual zero-days produced **138 alerts for one 9-day incident** — which is how an alerting system teaches its users to ignore it. The 56-day baseline window and ≥4-day minimum run are both set from *measurement*: at 28 days the baseline wandered across the threshold and fragmented the real outage into 3 days; at ≥3 days precision was 1-of-2. It now fires once, on the real incident.

**`DQ032`** is a **regression guard, not a detector**. A `CHECK` constraint makes the incoherent cancellation state unloadable, so the expected result is zero. A rule that cannot fire because a constraint already prevents the state is a stronger control than one that reports it — recorded as such rather than left looking like a miss.

The score reads 69.1/100 and **that is correct**. The dataset carries deliberate defects; a clean score would mean the checks were decorative.

---

## AI — the model had to earn its place

Aspect-based extraction over a **closed 13-category taxonomy**, not document-level sentiment and not topic modelling.

- **Not sentiment.** Public ratings for this segment sit at 4.8–4.9. A classifier returns "positive" for ~96% of rows and surfaces nothing. **135 negative operational aspects sit inside reviews rated 4.0 or higher** — a five-star review saying housekeeping took two hours is a work item; a sentiment score throws it away.
- **Not topic modelling.** Unsupervised topics are unstable run-to-run, so no comparable week-over-week trend is possible. An operator wants "housekeeping delays up 34% at BTM this week", which needs a stable label set.
- **The model never emits a number.** Labels and quotes only. Every count, rate and total comes from SQL.

### Benchmark: Gemini vs a deterministic keyword baseline

Both scored on the same reviews against known aspect labels.

| | Detection F1 | Polarity F1 | Polarity accuracy | Hinglish F1 |
|---|---|---|---|---|
| `gemini-3.1-flash-lite` | **81.1%** | **79.9%** | **96.0%** | **97.0%** |
| keyword baseline | 80.6% | 62.3% | 73.7% | 88.9% |
| **Δ** | +0.5 | **+17.6** | **+22.3** | +8.1 |

**The asymmetry is the finding.** Hospitality vocabulary is narrow enough that keyword matching *finds* aspects about as well as a language model. It fails on **polarity**, because "the AC was not cooling" and "the AC cooled quickly" share their aspect keyword entirely. Polarity is what decides whether a row becomes a work item or gets closed — so that is where the model earns its cost, and the benchmark says so rather than assuming it.

### Validation

Every extraction carries a **verbatim evidence span**, asserted to be a literal substring of its source review. Anything failing is **quarantined, not flagged** — a quote that does not appear in the source is a fabricated citation. On this corpus **0 rows were quarantined**, so the suite includes a test that *forces* the gate to reject a fabricated span: a validator that has never rejected anything is indistinguishable from one that cannot.

### The evaluation found a defect in my own taxonomy

`food` scores 6.0% F1 against the baseline's 95.2%. That is not a model failure — it is a **flaw in my label set**. Tea, coffee and grocery restocking are describable as either `food` or `amenities`; the baseline resolves it by keyword precedence, Gemini by meaning. Both readings are defensible. Reported rather than dropped, because a benchmark that quietly removes its two worst categories is not a benchmark. This is precisely what an evaluation is for.

Cost: **113,632 tokens** measured from `usage_metadata` and stored in `meta.llm_run_log` — not estimated from a price list. Runs on the free tier.

---

## Anomaly detection

Day-of-week aware trailing baseline, robust scale via **MAD** (one outlier inflates σ and hides the next; MAD is unaffected), **dual thresholds** — statistically unusual *and* materially large — and a **published false-alert budget**.

**Isolation Forest was deliberately rejected.** On a weekly-seasonal univariate series it learns that low-occupancy days are unusual and flags every weekend, while missing the Tuesday running 40% below its own Tuesday norm — the case Operations actually cares about. It also yields no baseline, no magnitude and no explanation, so its output cannot be acted on.

Verified against ground truth: **F1, F2 and F3 all detected; the D1 decoy raises no alarm** (ADR moved +₹21 against a ₹350 materiality gate; RevPAR *rose* ₹124). The negative test matters most. Note that F2 and F3 are caught by **deterministic rules**, not the detector — a stored date contradicting its derivation is a correctness bug, and a stopped feed is a run-length question. Reaching for a z-score on either would be worse engineering.

---

## Automation

[`daily_brief.yml`](.github/workflows/daily_brief.yml) runs at 02:07 UTC (07:37 IST) — deliberately **not** on the hour, because GitHub names the top of every hour as a high-load window where scheduled jobs can be delayed or dropped. The claimed SLA is *"before 08:00 IST"*, not *"at 07:37:00"*; every free scheduler is approximate and the brief carries its own `generated_at` stamp rather than pretending otherwise.

The job loads data → runs the quality gate → computes KPIs → scans for anomalies → composes a brief with a recommendation and an explicit confidence → commits the artifact. **The run of dated commits is the verifiable evidence the schedule fires** — not a screenshot.

[`staleness_watchdog.yml`](.github/workflows/staleness_watchdog.yml) is a monitor that watches the monitor. Every free scheduler silently disables itself; the interesting failure is not "the pipeline errored" but "the pipeline stopped and nobody noticed."

Every number in the brief is computed in SQL or pandas and passed to the template as a fact. **No language model calculates anything in it** — the documented failure mode of LLM narration is inventing growth rates that were never computed.

Latest output: **[reports/LATEST_BRIEF.md](reports/LATEST_BRIEF.md)**

---

## Repository map

```
migrations/          5 numbered SQL migrations, checksum-tracked
src/staypulse/
  config.py          env loading; builds the DSN via URL.create so a password
                     containing ? or # is escaped correctly, not by hand
  db.py              engine factory, pooling
  generate/          seeded generator — spec.py holds every assumption
  quality/           29 declarative rules + execution + recall scoring
  ai/                taxonomy, Gemini client, keyword baseline, evaluation
  analytics/         anomaly detection with attribution
sql/analysis/        6 curated business analyses
scripts/             verify_env · migrate · generate_data · validate_dataset
                     validate_metrics · run_quality · run_analyses
                     run_ai_pipeline · run_ai_eval · run_anomaly_detection
                     build_decision_log · daily_brief
tests/               51 tests
reports/             generated: analyses, AI evaluation, anomalies, briefings
METRICS.md           generated from the metric registry
DECISION_LOG.md      generated from live warehouse queries
```

---

## Running it

```bash
python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
cp .env.example .env          # fill in PGHOST/PGUSER/PGPASSWORD + GEMINI_API_KEY
.venv/Scripts/python.exe scripts/verify_env.py
```

`verify_env.py` checks runtime, packages, database, Gemini, weather, desktop tooling, git state and secret hygiene. **Every secret is scrubbed from its output**, including from third-party exception text, so the report is safe to paste anywhere.

```bash
python scripts/migrate.py            # schema
python scripts/generate_data.py      # seeded dataset → PostgreSQL
python scripts/validate_dataset.py   # assert the planted signals emerged
python scripts/run_quality.py        # 29 rules + defect recall
python scripts/validate_metrics.py --export METRICS.md
python scripts/run_analyses.py --out reports/analyses.md
python scripts/run_ai_pipeline.py --limit 400 --recent-first
python scripts/run_ai_eval.py --out reports/ai_evaluation.md
python scripts/run_anomaly_detection.py --out reports/anomalies.md
python scripts/build_decision_log.py
python scripts/daily_brief.py
python -m pytest tests/ -v
```

Paste the password into `PGPASSWORD` **verbatim** — do not percent-encode it. The connection URL is built with `sqlalchemy.engine.URL.create`, which escapes every component; hand-escaping produces a double-encoded password and an auth error that looks exactly like a wrong one.

---

## Known gaps — stated, not hidden

- **Power BI.** The semantic layer, star schema and DAX-ready measures exist, but the `.pbix` is not in the repo. Power BI Desktop authors a binary format, and `Publish to web` requires a tenant setting a student account does not control — so a live Power BI link was never achievable. The honest position is that the *model* is built and the report is not.
- **Excel / Power Query.** A named must-have in the target role and genuinely not built yet. The folder-connector refresh pattern is the intended next artifact.
- **Zoho Analytics.** Evaluated. Its free tier caps at 10,000 rows account-wide and stops loading silently at the ceiling, so a curated row-budgeted extract is the only viable shape. Not built; blocked on confirming that "Make Public" yields a working zero-login URL.
- **Ground truth is generator-derived, not human-annotated.** The AI benchmark measures whether a method recovers deliberately injected aspects — **not** agreement with human judgement. A hand-labelled set would be stronger evidence. No claim of human agreement is made anywhere.
- **Review text is template-composed**, so its prose is less varied than real guest writing. Both methods likely score better here than they would on live reviews.
- **GOPPAR and fully-loaded cost per booking are uncomputable** on this dataset — there is no departmental cost data. `cost_per_booking_inr` is explicitly a *direct* cost (commission + gateway fees + GST on both) and is labelled as such. Publishing a fully-loaded-looking figure built on invented overhead would be worse than omitting it.
- **The Hinglish sample is small** (34 reviews), so its per-language figure is indicative rather than precise.

---

## What I would do in week one with real data

1. **Reconcile one month three ways** — PMS folio vs gateway settlement vs bank credit — and assign every unmatched rupee a typed reason. A reconciliation is clean when *unexplained* variance is zero, not when variance is zero.
2. **Audit the date basis of every existing report.** The ₹182,344 spread above is not hypothetical; it is what happens when Marketing, Operations and Finance each pick a different date and nobody wrote it down.
3. **Check whether the timezone defect is real.** Compare every stored reporting date against its own event timestamp. It costs one query and it silently moves late-night revenue.
4. **Run identity resolution before quoting repeat rate.** Here it moved the number by 0.8pp and, in an earlier iteration, would have triggered a retention campaign against a measurement artefact.
5. **Segment SLA by property and day-part before concluding anything** from a blended number. A 0.9pp portfolio move concealed a 2.5× degradation in one shift at one property.

---

<sub>Synthetic data throughout, generated from a documented seeded model. Built with Python, PostgreSQL, SQL and the Gemini API on free tiers. Not affiliated with any hospitality company.</sub>
