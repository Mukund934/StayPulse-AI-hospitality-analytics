# Metric dictionary

One definition per metric, generated from `meta.metric_definition` — the same
registry the semantic layer executes. This file is not documentation *about* the
metrics; it is a rendering *of* them, so it cannot drift from what the warehouse
computes.

`date_basis` is `CHECK`-constrained in the database, so a metric cannot be
registered without declaring which date it is measured on — the single most
common cause of two dashboards disagreeing.

**22 active metrics.** All figures derive from clearly-labelled
synthetic data.

---

## ADR (excluding microstays)  ·  `adr_excl_microstay_inr`

ADR over nightly stays only, excluding hourly microstay inventory.

| | |
|---|---|
| **Formula** | `Net Room Revenue (nightly) / Rooms Sold (nightly)` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | inr |
| **Revenue basis** | net_of_tax |
| **Source tables** | `mart.fact_unit_night`, `mart.fact_booking` |
| **Owner** | Revenue |

**Includes** — Occupied unit-nights where stay_type <> microstay.

**Excludes** — Hourly microstays excluded from both numerator and denominator.

> **Caveat.** The honest comparison figure when benchmarking against nightly-only operators.

<details><summary>SQL</summary>

```sql
round(sum(room_revenue_net_inr) FILTER (WHERE stay_type <> 'microstay') / NULLIF(count(*) FILTER (WHERE is_occupied AND stay_type <> 'microstay'), 0), 2)
```

</details>

---

## ADR  ·  `adr_inr`

Average Daily Rate: net room revenue per occupied unit-night, excluding tax.

| | |
|---|---|
| **Formula** | `Net Room Revenue / Rooms Sold` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | inr |
| **Revenue basis** | net_of_tax |
| **Source tables** | `mart.fact_unit_night` |
| **Owner** | Revenue |

**Includes** — Occupied unit-nights only.

**Excludes** — GST excluded. OTA commission NOT deducted -- see adr_net_of_commission_inr.

> **Caveat.** Includes hourly microstays, which bill at roughly 28% of a nightly rate and pull ADR down. adr_excl_microstay_inr is published alongside for that reason.

<details><summary>SQL</summary>

```sql
round(sum(room_revenue_net_inr) / NULLIF(count(*) FILTER (WHERE is_occupied), 0), 2)
```

</details>

---

## Cost per Booking  ·  `cost_per_booking_inr`

Directly attributable acquisition and processing cost per confirmed booking: OTA commission plus payment gateway fees and the GST charged on both.

| | |
|---|---|
| **Formula** | `(Commission + Gateway Fee + GST on fees) / Confirmed Bookings` |
| **Grain** | booking |
| **Date basis** | `booking_date` |
| **Unit** | inr |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking`, `mart.fact_payment`, `mart.dim_channel` |
| **Owner** | Finance |

**Includes** — Non-cancelled bookings with their commission and gateway costs.

**Excludes** — Staff, utilities, housekeeping consumables and fixed overhead are NOT included -- that data does not exist here.

> **Caveat.** This is a DIRECT cost per booking, not a fully loaded cost, and must not be presented as one. GOPPAR is uncomputable on this dataset for the same reason: no departmental cost data. Saying so is more useful than publishing a fully-loaded-looking number built on invented overhead.

<details><summary>SQL</summary>

```sql
round((sum(b.commission_inr) + COALESCE(sum(p.gateway_fee_inr), 0) + COALESCE(sum(p.gst_on_fee_inr), 0)) / NULLIF(count(DISTINCT b.booking_key), 0), 2)
```

</details>

---

## RevPAR  ·  `revpar_inr`

Revenue per Available Room-night: the single number that reflects both rate and volume.

| | |
|---|---|
| **Formula** | `Net Room Revenue / Rooms Available  ==  ADR x Occupancy` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | inr |
| **Revenue basis** | net_of_tax |
| **Source tables** | `mart.fact_unit_night` |
| **Owner** | Revenue |

**Includes** — Sellable unit-nights in the denominator, all room revenue in the numerator.

**Excludes** — Out-of-order nights excluded from the denominator, matching occupancy_pct.

> **Caveat.** RevPAR = ADR x Occupancy holds exactly ONLY because all three read the same table and the same denominator. Asserted in tests. RevPAR is also indifferent between 100% at INR 3,000 and 60% at INR 5,000, which have very different cost per occupied unit -- do not optimise it alone.

<details><summary>SQL</summary>

```sql
round(sum(room_revenue_net_inr) / NULLIF(count(*) FILTER (WHERE is_sellable), 0), 2)
```

</details>

---

## RevPOR  ·  `revpor_inr`

Room revenue per occupied room-night. Unlike ADR this is not diluted by how many rooms were available.

| | |
|---|---|
| **Formula** | `Room Revenue (net) / Rooms Sold` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | inr |
| **Revenue basis** | net_of_tax |
| **Source tables** | `mart.fact_unit_night` |
| **Owner** | Revenue |

**Includes** — Every occupied unit-night.

**Excludes** — Room revenue only. This is NOT TRevPOR: there is no food, beverage or ancillary revenue in this warehouse, so total-revenue metrics are deliberately not published.

> **Caveat.** On a room-only dataset RevPOR and ADR coincide. It is registered separately so that adding ancillary revenue later does not silently change what ADR means.

<details><summary>SQL</summary>

```sql
round(sum(room_revenue_net_inr) / NULLIF(count(*) FILTER (WHERE is_occupied), 0), 2)
```

</details>

---

## Gross Room Revenue (incl GST)  ·  `room_revenue_gross_inr`

What the guest actually paid, including GST at the rate applicable to that stay date and nightly rate.

| | |
|---|---|
| **Formula** | `Net Room Revenue x (1 + GST%)` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | inr |
| **Revenue basis** | gross_incl_tax |
| **Source tables** | `mart.v_unit_night_enriched`, `meta.gst_rate` |
| **Owner** | Finance |

**Includes** — GST resolved per night by stay date and nightly rate.

**Excludes** — Nothing excluded.

> **Caveat.** Spans the 22 Sep 2025 GST change (12% slab abolished; 5% no-ITC at or below INR 7,500, 18% with ITC above). An apparent step in gross revenue across that date is a TAX artefact, not performance -- which is why the net measure is the one used for rate metrics.

<details><summary>SQL</summary>

```sql
sum(gross_incl_gst_inr)
```

</details>

---

## Net Room Revenue  ·  `room_revenue_net_inr`

Room revenue net of GST and net of discount, before OTA commission.

| | |
|---|---|
| **Formula** | `SUM(room revenue, net of tax)` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | inr |
| **Revenue basis** | net_of_tax |
| **Source tables** | `mart.fact_unit_night` |
| **Owner** | Revenue |

**Includes** — All occupied unit-nights.

**Excludes** — GST excluded. Discounts already deducted.

> **Caveat.** Recognised on STAY DATE, not booking or payment date. Marketing reporting on booking date and Finance on payment date will legitimately produce different totals for the same month.

<details><summary>SQL</summary>

```sql
sum(room_revenue_net_inr)
```

</details>

---

## Booking pace vs curve  ·  `booking_pace_pct`

Nights on the books for a stay date, as a percentage of what is normally on the books at the same number of days out.

| | |
|---|---|
| **Formula** | `Nights on Books / Median Nights on Books at same days_out x 100` |
| **Grain** | stay_date x property x as_of_date |
| **Date basis** | `as_of_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.v_booking_night`, `mart.v_booking_curve` |
| **Owner** | Revenue |

**Includes** — Compared against the median curve for the same property and the same days-out horizon.

**Excludes** — Stay dates with fewer than 6 comparable historical observations are not scored.

> **Caveat.** A pace below 100 is not automatically bad: it can mean the same demand arriving later. Read with lead-time mix, not alone.

<details><summary>SQL</summary>

```sql
round(100.0 * nights_on_books / NULLIF(expected_nights_at_horizon, 0), 1)
```

</details>

---

## Cancellation Rate  ·  `cancellation_rate_pct`

Share of bookings made in the period that were subsequently cancelled.

| | |
|---|---|
| **Formula** | `Cancelled Bookings / Bookings Made x 100` |
| **Grain** | booking |
| **Date basis** | `booking_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking` |
| **Owner** | Revenue |

**Includes** — All bookings whose BOOKING date falls in the period.

**Excludes** — No-shows are a separate status and are NOT counted as cancellations.

> **Caveat.** Deliberately measured on cohort basis -- cancellations OF bookings made in the period -- not cancellations occurring in the period. The two give materially different numbers and answer different questions: cohort basis measures booking quality, event basis measures this period's revenue loss. State which one you mean.

<details><summary>SQL</summary>

```sql
round(100.0 * count(*) FILTER (WHERE status = 'cancelled') / NULLIF(count(*), 0), 2)
```

</details>

---

## Channel Mix  ·  `channel_mix_pct`

Share of room-nights sold through each distribution channel.

| | |
|---|---|
| **Formula** | `Room-nights per channel / Total room-nights x 100` |
| **Grain** | channel x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_unit_night`, `mart.dim_channel` |
| **Owner** | Revenue |

**Includes** — Occupied unit-nights, attributed to the booking's channel.

**Excludes** — Cancelled bookings hold no inventory and so do not appear.

> **Caveat.** Measured on room-nights, not booking count: corporate books longer stays, so a booking-count mix overstates OTA share.

<details><summary>SQL</summary>

```sql
round(100.0 * count(*) FILTER (WHERE is_occupied) OVER (PARTITION BY channel_key) / NULLIF(count(*) FILTER (WHERE is_occupied) OVER (), 0), 2)
```

</details>

---

## Occupancy %  ·  `occupancy_pct`

Share of sellable unit-nights that were actually occupied. The operational basis: units that cannot be sold are not counted as missed sales.

| | |
|---|---|
| **Formula** | `Rooms Sold / Rooms Available x 100` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_unit_night` |
| **Owner** | Revenue |

**Includes** — Every unit-night where is_sellable. Complimentary and house-use nights count as sold.

**Excludes** — Out-of-order unit-nights are EXCLUDED from the denominator.

> **Caveat.** Differs from occupancy_pct_benchmark by the share of inventory out of order. Report the basis alongside the number or the two figures will be read as an error.

<details><summary>SQL</summary>

```sql
round(100.0 * count(*) FILTER (WHERE is_occupied) / NULLIF(count(*) FILTER (WHERE is_sellable), 0), 2)
```

</details>

---

## Occupancy % (benchmark basis)  ·  `occupancy_pct_benchmark`

Occupancy against full physical inventory, the basis used for STR-style external comparison.

| | |
|---|---|
| **Formula** | `Rooms Sold / Physical Unit-Nights x 100` |
| **Grain** | property x stay_date |
| **Date basis** | `stay_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_unit_night` |
| **Owner** | Revenue |

**Includes** — All physical unit-nights for an open property.

**Excludes** — Nothing excluded from the denominator.

> **Caveat.** Always lower than the operational basis. Deliberately published alongside it so the gap is visible rather than surprising.

<details><summary>SQL</summary>

```sql
round(100.0 * count(*) FILTER (WHERE is_occupied) / NULLIF(count(*), 0), 2)
```

</details>

---

## Repeat Guest Rate  ·  `repeat_guest_rate_pct`

Share of guests with two or more completed stays, after deterministic identity resolution.

| | |
|---|---|
| **Formula** | `Guests with >=2 stays / Total guests x 100` |
| **Grain** | guest |
| **Date basis** | `stay_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking`, `mart.dim_guest` |
| **Owner** | Customer Experience |

**Includes** — Guests with at least one completed stay, keyed on normalised phone then normalised email.

**Excludes** — Cancelled and no-show bookings do not count as stays.

> **Caveat.** Published on the RESOLVED basis. The raw basis understates loyalty because duplicate profiles split one guest into several; mart.v_guest_repeat exposes both so the size of that understatement is visible.

<details><summary>SQL</summary>

```sql
SELECT repeat_rate_resolved_pct FROM mart.v_guest_repeat
```

</details>

---

## SLA Breach Rate  ·  `sla_breach_rate_pct`

Share of resolved requests that exceeded the target resolution time for their request type.

| | |
|---|---|
| **Formula** | `Breached Requests / Resolved Requests x 100` |
| **Grain** | service request |
| **Date basis** | `request_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_service_request`, `mart.dim_request_type` |
| **Owner** | Operations |

**Includes** — Resolved requests with an SLA target.

**Excludes** — Open requests excluded.

> **Caveat.** The blended rate hides property- and daypart-level failure. A 0.9pp portfolio move concealed a 2.6x degradation at one property in one shift. Always segment before concluding.

<details><summary>SQL</summary>

```sql
round(100.0 * count(*) FILTER (WHERE is_sla_breached) / NULLIF(count(*) FILTER (WHERE resolution_minutes IS NOT NULL), 0), 2)
```

</details>

---

## Wash rate  ·  `wash_rate_pct`

Share of bookings made for a stay month that did not convert into a stay, through cancellation or no-show.

| | |
|---|---|
| **Formula** | `(Cancelled + No-show) / Bookings Made x 100` |
| **Grain** | stay_month x property x channel |
| **Date basis** | `stay_date` |
| **Unit** | percent |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking` |
| **Owner** | Revenue |

**Includes** — Cohorted on stay month so the denominator is demand for that month.

**Excludes** — Bookings amended rather than cancelled are not tracked; the source has no amendment history.

> **Caveat.** This is the number an overbooking policy would rest on. It is NOT a forecast of future wash.

<details><summary>SQL</summary>

```sql
round(100.0 * count(*) FILTER (WHERE status IN ('cancelled','no_show')) / count(*), 2)
```

</details>

---

## Average Length of Stay  ·  `alos_nights`

Mean nights per nightly reservation.

| | |
|---|---|
| **Formula** | `Total Room-Nights / Nightly Bookings` |
| **Grain** | booking |
| **Date basis** | `stay_date` |
| **Unit** | nights |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking` |
| **Owner** | Revenue |

**Includes** — Non-cancelled nightly bookings.

**Excludes** — Microstays and day-use excluded (zero nights by definition).

> **Caveat.** Uses the half-open interval: departure night is not a night. Counting it inflates ALOS by 1 and room-nights by roughly 1/ALOS.

<details><summary>SQL</summary>

```sql
round(sum(nights)::numeric / NULLIF(count(*) FILTER (WHERE stay_type = 'nightly'), 0), 2)
```

</details>

---

## Average Booking Lead Time  ·  `avg_lead_time_days`

Mean days between booking and arrival. Short lead times leave less room to reprice.

| | |
|---|---|
| **Formula** | `AVG(check_in_date - booking_date)` |
| **Grain** | booking |
| **Date basis** | `booking_date` |
| **Unit** | days |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking` |
| **Owner** | Revenue |

**Includes** — Non-cancelled bookings.

**Excludes** — Hourly microstays excluded -- they book minutes ahead and would collapse the mean.

> **Caveat.** Indian booking windows run 7-21 days against a ~40-day global average, so pickup analysis here should use 0/1/3/7/14-day windows rather than the textbook 30/60/90.

<details><summary>SQL</summary>

```sql
round(avg(lead_time_days)::numeric, 1)
```

</details>

---

## Cancellation notice  ·  `cancel_notice_days`

Days between a cancellation and the stay date it was cancelled from.

| | |
|---|---|
| **Formula** | `median(check_in_date - cancel_date)` |
| **Grain** | stay_month x channel |
| **Date basis** | `cancel_date` |
| **Unit** | days |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_booking` |
| **Owner** | Revenue |

**Includes** — Cancelled bookings with a recorded cancellation date.

**Excludes** — No-shows are excluded: they gave no notice at all, and folding them in as zero would understate the notice actually given by people who did cancel.

> **Caveat.** Separates resellable cancellations from lost inventory. A 20-day notice is recoverable; a same-day one is not.

<details><summary>SQL</summary>

```sql
percentile_cont(0.5) WITHIN GROUP (ORDER BY (check_in_date - cancel_date))
```

</details>

---

## CSAT  ·  `csat_avg`

Mean guest satisfaction score on resolved service requests, 1-5.

| | |
|---|---|
| **Formula** | `AVG(csat_score)` |
| **Grain** | service request |
| **Date basis** | `request_date` |
| **Unit** | score |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_service_request` |
| **Owner** | Customer Experience |

**Includes** — Requests where the guest responded.

**Excludes** — Non-responders excluded -- imputing them would invent data.

> **Caveat.** Response rate is roughly a third, so CSAT carries selection bias: guests who respond are not a random sample. Report the response rate next to the score.

<details><summary>SQL</summary>

```sql
round(avg(csat_score)::numeric, 2)
```

</details>

---

## Nights on the books  ·  `nights_on_books`

Room-nights sold for a future stay date, as the book stood on a given snapshot date.

| | |
|---|---|
| **Formula** | `count(booking-nights where entered_on <= as_of and not cancelled by as_of)` |
| **Grain** | stay_date x property x as_of_date |
| **Date basis** | `as_of_date` |
| **Unit** | nights |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.v_booking_night` |
| **Owner** | Revenue |

**Includes** — Every booking-night on the books at the snapshot, including ones later cancelled.

**Excludes** — Zero-night hourly bookings contribute nothing. Stays on or before the snapshot date are excluded as occupancy, not pickup.

> **Caveat.** Bi-temporal. Meaningless without stating the snapshot date. Reconstructed from booking and cancellation dates, not from stored nightly snapshots, so it assumes a booking never silently changed its dates.

<details><summary>SQL</summary>

```sql
count(*) FILTER (WHERE entered_on <= :as_of AND (left_on IS NULL OR left_on > :as_of))
```

</details>

---

## Pickup (nights)  ·  `pickup_nights`

Room-nights added to the book on a given activity date for a given stay date.

| | |
|---|---|
| **Formula** | `nights added on activity_date` |
| **Grain** | stay_date x activity_date |
| **Date basis** | `as_of_date` |
| **Unit** | nights |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.v_pickup_daily` |
| **Owner** | Revenue |

**Includes** — Gross additions to the book.

**Excludes** — Cancellations are reported separately as pickup_cancellations, not netted here.

> **Caveat.** Gross by design. A day that added 20 and lost 18 reads identically to one that added 2 and lost 0 if only net pickup is published.

<details><summary>SQL</summary>

```sql
sum(nights_added)
```

</details>

---

## Service Turnaround Time  ·  `service_tat_minutes`

Wall-clock minutes from a guest raising a request to its resolution.

| | |
|---|---|
| **Formula** | `AVG(resolved_at - created_at)` |
| **Grain** | service request |
| **Date basis** | `request_date` |
| **Unit** | minutes |
| **Revenue basis** | not_applicable |
| **Source tables** | `mart.fact_service_request` |
| **Owner** | Operations |

**Includes** — Resolved requests.

**Excludes** — Open requests excluded -- including them as zero would flatter the mean.

> **Caveat.** WALL-CLOCK, not business hours. These are 24-hour serviced apartments and a guest waiting at 02:00 is still waiting. A business-hours clock would report this as excellent.

<details><summary>SQL</summary>

```sql
round(avg(resolution_minutes)::numeric, 1)
```

</details>

---
