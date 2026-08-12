# Power BI model — ready to assemble

**Status: model prepared, report not authored.** The `.pbix` is a binary format only
Power BI Desktop can write, so it cannot be generated from a script. What *can* be
prepared — and is, here — is everything that makes building it mechanical: the
conformed star schema, the relationship map, and every measure as DAX that mirrors
the SQL definition exactly.

Assembly is roughly 30 minutes of clicking. It is not done, and this file does not
pretend otherwise.

---

## 1. Load

Regenerate the CSVs from the live warehouse:

```bash
python scripts/export_bi_model.py
```

Then in Power BI Desktop: **Get Data → Folder →** `powerbi/data` → *Combine &
Transform*, or load each CSV individually (12 tables, 24,043 rows — comfortably
in-memory).

| Table | Rows | Role |
|---|---|---|
| `dim_date` | 558 | Date dimension. **Mark as date table** on `full_date`. |
| `dim_property` | 3 | Property |
| `dim_unit` | 40 | Unit / room type |
| `dim_channel` | 8 | Distribution channel, carries `commission_pct` |
| `dim_request_type` | 10 | Service taxonomy, carries `sla_minutes` |
| `dim_guest` | 9,360 | **No PII exported** — analytical attributes only |
| `fact_daily_kpi` | 1,278 | Property × stay-date. The report's primary fact. |
| `fact_booking` | 5,228 | Booking grain (booking-date metrics) |
| `fact_service_request` | 1,900 | Service request grain |
| `fact_review_aspect` | 1,116 | Aspect grain from the AI pipeline |
| `fact_payment` | 4,498 | Payment / settlement |
| `dq_rule_results` | 29 | Latest data-quality run |
| `metric_definition` | 16 | Renders the definitions page |

---

## 2. Relationships

All single-direction, one-to-many, from dimension to fact. **Do not enable
bidirectional filtering** — it creates ambiguous paths across three fact tables and
produces silently wrong totals.

```
dim_date[full_date]        1 → *  fact_daily_kpi[stay_date]
dim_date[full_date]        1 → *  fact_service_request[request_date]
dim_date[full_date]        1 → *  fact_review_aspect[review_date]
dim_property[property_key] 1 → *  fact_daily_kpi[property_key]
dim_property[property_key] 1 → *  fact_service_request[property_key]
dim_property[property_key] 1 → *  fact_review_aspect[property_key]
dim_channel[channel_key]   1 → *  fact_booking[channel_key]
dim_request_type[request_type_key] 1 → * fact_service_request[request_type_key]
dim_guest[guest_key]       1 → *  fact_booking[guest_key]
fact_booking[booking_key]  1 → *  fact_payment[booking_key]
```

### The one non-obvious relationship

`fact_booking` needs **two** date relationships, because booking date and stay date
answer different questions and are not interchangeable:

```
dim_date[full_date] 1 → * fact_booking[booking_date]    ACTIVE
dim_date[full_date] 1 → * fact_booking[check_in_date]   INACTIVE
```

Activate the second only inside a measure with `USERELATIONSHIP`. Leaving both
active is impossible in Power BI; leaving only one and forgetting is how a report
starts reporting bookings-made when someone asked about stays.

---

## 3. Measures

Put all of these in a dedicated `_Measures` table (Enter Data → one blank column →
hide it). **Every core KPI is an explicit measure.** Never drag a numeric column
onto a visual and let Power BI implicitly sum it — implicit aggregation is how two
visuals end up disagreeing.

```dax
-- ── Base ────────────────────────────────────────────────────────────────
Rooms Available    = SUM ( fact_daily_kpi[rooms_available] )
Rooms Sold         = SUM ( fact_daily_kpi[rooms_sold] )
Rooms OOO          = SUM ( fact_daily_kpi[rooms_out_of_order] )
Physical Unit Nights = SUM ( fact_daily_kpi[unit_nights_physical] )
Net Room Revenue   = SUM ( fact_daily_kpi[room_revenue_net_inr] )
Gross Room Revenue = SUM ( fact_daily_kpi[room_revenue_gross_inr] )
GST               = SUM ( fact_daily_kpi[gst_inr] )
Commission        = SUM ( fact_daily_kpi[commission_inr] )

-- ── Rate metrics. DIVIDE, never "/" — it handles divide-by-zero. ────────
Occupancy %           = DIVIDE ( [Rooms Sold], [Rooms Available] )
Occupancy % Benchmark = DIVIDE ( [Rooms Sold], [Physical Unit Nights] )
-- The gap IS inventory lost to out-of-order units. Publish both or the two
-- numbers get read as an error rather than as two valid bases.
Occupancy OOO Gap pp  = [Occupancy %] - [Occupancy % Benchmark]

ADR    = DIVIDE ( [Net Room Revenue], [Rooms Sold] )
RevPAR = DIVIDE ( [Net Room Revenue], [Rooms Available] )

-- Sanity measure. Put it on the Data Trust page. It must read 0.
RevPAR Identity Check = [RevPAR] - ( [ADR] * [Occupancy %] )

-- ── Time intelligence. 364 days, NOT 365: 364 is 52 weeks, so Saturday
--    maps to Saturday. Weekday alignment matters more than calendar date
--    in hospitality. ──────────────────────────────────────────────────────
Net Room Revenue STLY =
CALCULATE ( [Net Room Revenue], DATEADD ( dim_date[full_date], -364, DAY ) )

RevPAR STLY =
CALCULATE ( [RevPAR], DATEADD ( dim_date[full_date], -364, DAY ) )

Revenue vs STLY % = DIVIDE ( [Net Room Revenue] - [Net Room Revenue STLY],
                             [Net Room Revenue STLY] )

Net Room Revenue MTD = TOTALMTD ( [Net Room Revenue], dim_date[full_date] )

-- ── Bookings (booking-date basis unless stated) ─────────────────────────
Bookings Made  = COUNTROWS ( fact_booking )
Cancellations  = CALCULATE ( COUNTROWS ( fact_booking ),
                             fact_booking[is_cancelled] = TRUE () )
-- Cohort basis: cancellations OF bookings MADE in the period. The event basis
-- (cancellations occurring in the period) is a different number answering a
-- different question. State which one a visual uses.
Cancellation Rate = DIVIDE ( [Cancellations], [Bookings Made] )

Avg Lead Time Days = AVERAGE ( fact_booking[lead_time_days] )
ALOS = DIVIDE ( SUM ( fact_booking[nights] ),
                CALCULATE ( COUNTROWS ( fact_booking ),
                            fact_booking[stay_type] = "nightly" ) )

-- Stay-date view of bookings via the inactive relationship.
Bookings By Stay Date =
CALCULATE ( COUNTROWS ( fact_booking ),
            USERELATIONSHIP ( dim_date[full_date], fact_booking[check_in_date] ) )

Channel Mix % =
DIVIDE ( [Rooms Sold], CALCULATE ( [Rooms Sold], ALL ( dim_channel ) ) )

-- ── Operations ──────────────────────────────────────────────────────────
Service Requests  = COUNTROWS ( fact_service_request )
SLA Breaches      = CALCULATE ( COUNTROWS ( fact_service_request ),
                                fact_service_request[is_sla_breached] = TRUE () )
SLA Breach Rate   = DIVIDE ( [SLA Breaches], [Service Requests] )
-- Wall clock, not business hours. These are 24-hour serviced apartments and a
-- guest waiting at 02:00 is still waiting.
Avg Resolution Min = AVERAGE ( fact_service_request[resolution_minutes] )
Avg First Response Min = AVERAGE ( fact_service_request[first_response_minutes] )
CSAT = AVERAGE ( fact_service_request[csat_score] )
-- Report this next to CSAT. Roughly a third respond, so CSAT carries selection bias.
CSAT Response Rate = DIVIDE ( COUNT ( fact_service_request[csat_score] ),
                              [Service Requests] )

-- ── Guest feedback (AI) ─────────────────────────────────────────────────
Negative Aspects = CALCULATE ( COUNTROWS ( fact_review_aspect ),
                               fact_review_aspect[polarity] = "negative" )
-- The artifact that justifies aspect extraction over sentiment: operational
-- problems reported inside reviews that rate 4+.
Buried Complaints =
CALCULATE ( COUNTROWS ( fact_review_aspect ),
            fact_review_aspect[polarity] = "negative",
            fact_review_aspect[rating] >= 4.0 )
Severe Aspects = CALCULATE ( COUNTROWS ( fact_review_aspect ),
                             fact_review_aspect[severity] = "severe" )

-- ── Data trust ──────────────────────────────────────────────────────────
DQ Rules        = COUNTROWS ( dq_rule_results )
DQ Rules Passed = CALCULATE ( [DQ Rules], dq_rule_results[passed] = TRUE () )
DQ Rows Failed  = SUM ( dq_rule_results[rows_failed] )
-- Severity-weighted, matching the Python implementation: error 3 / warning 2 /
-- info 1. An unweighted pass rate lets a passing info rule cancel a failing
-- error rule about unresolvable money.
DQ Score =
VAR W =
    SUMX ( dq_rule_results,
           SWITCH ( dq_rule_results[severity], "error", 3, "warning", 2, 1 ) )
VAR E =
    SUMX ( FILTER ( dq_rule_results, dq_rule_results[passed] = TRUE () ),
           SWITCH ( dq_rule_results[severity], "error", 3, "warning", 2, 1 ) )
RETURN DIVIDE ( E, W ) * 100

Orphan Payments = CALCULATE ( COUNTROWS ( fact_payment ),
                              fact_payment[is_orphan_reference] = TRUE () )
Business Date Drift = CALCULATE ( COUNTROWS ( fact_booking ),
                                  fact_booking[has_business_date_drift] = TRUE () )
```

### Format strings

`Occupancy %`, `Cancellation Rate`, `SLA Breach Rate`, `Channel Mix %` → percentage,
1 decimal. `ADR`, `RevPAR`, revenue measures → `₹#,0` with thousands separator.
`CSAT` → 2 decimals. Set these on the measure, not per-visual.

---

## 4. Page layout

Three pages. Not four — nobody reads page four, and a half-finished fourth page
damages the three that work.

### Page 1 — Executive / Revenue

- **KPI row:** Net Room Revenue · Occupancy % · ADR · RevPAR — each with
  `Revenue vs STLY %` as the comparison. A KPI card with no comparison fails the
  "compared to what?" test and cannot drive an action.
- Revenue trend by month, `Net Room Revenue` vs `Net Room Revenue STLY`.
- Channel mix — 100% stacked bar by `dim_channel[channel_name]`, room-night basis.
- Property comparison — matrix: property × (Occupancy, ADR, RevPAR).
- Cancellation rate by channel.
- **Annotation text box:** state the date basis. *"Revenue on stay date. Booking-date
  and payment-date views give different, equally correct totals — see the Definitions
  page."*

### Page 2 — Operations & Guest Experience

- **KPI row:** Service Requests · SLA Breach Rate · Avg Resolution Min · CSAT
  (with `CSAT Response Rate` beside it).
- **Heatmap: property × `day_part_ist`, coloured by SLA Breach Rate.** This is the
  page's most important visual — it is the one that makes the Koramangala evening
  degradation visible. A blended number hides it entirely.
- Resolution time trend by property.
- Negative aspects by category, bar, with `actionable_by` as the legend so the chart
  routes to a team rather than just describing.
- **Buried Complaints card** with a drill-through to the underlying reviews and their
  evidence spans.

### Page 3 — Data Trust / Analyst view

- **KPI row:** DQ Score · DQ Rules Passed / DQ Rules · DQ Rows Failed ·
  `RevPAR Identity Check` (**must read 0**).
- Rule results table: rule, dimension, severity, rows failed, failure %, pass/fail.
  Conditional-format the failures red.
- Orphan Payments and Business Date Drift cards.
- **`metric_definition` table rendered as a definitions list.** Every metric with its
  formula, grain, date basis and caveats — so the report carries its own dictionary
  and a reader never has to guess which occupancy they are looking at.

### Interactions

Add a `dim_property` slicer synced across pages, and a `dim_date` range slicer on
pages 1–2. Turn off cross-filtering from the KPI cards — clicking a total should not
silently re-filter the page.

---

## 5. Sharing — the constraint worth knowing

*Publish to web* requires a tenant setting a Power BI admin must enable, and a
student or personal tenant will not have it. It is also incompatible with row-level
security.

So the realistic deliverables are the `.pbix` committed to the repo, a PDF export,
and screenshots in `assets/img/`. Planning around a live Power BI URL and
discovering the tenant restriction late is the common mistake.

---

## 6. Definition parity

Every DAX measure above mirrors the SQL in `meta.metric_definition`. If you change
one, change both, or the report and the warehouse will disagree — which is the exact
failure the metric registry exists to prevent. Run
`python scripts/validate_metrics.py` after any change; it recomputes every published
figure independently of the views and compares.
