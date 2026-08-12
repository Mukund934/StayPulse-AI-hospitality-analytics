-- 004 · Semantic layer and metric registry.
--
-- The point of this file is that a metric is defined ONCE. Every consumer --
-- SQL, Python, Power BI, Zoho, the automated briefing -- reads the same view or
-- the same registered SQL expression, so "one number means one thing" is
-- enforced by construction rather than by discipline.
--
-- Two deliberate design choices:
--
--   1. Occupancy is published TWO ways, because both are legitimate and they
--      disagree. The operational view removes out-of-order units from
--      availability (what a property team can actually sell); the benchmark view
--      keeps full physical inventory (how STR-style comparison works). The gap
--      between them is inventory lost to OOO, which is itself an actionable
--      number. Publishing one and hiding the other is how two dashboards end up
--      disagreeing.
--
--   2. Every date-bucketed view names its date basis in the column name, so a
--      query cannot accidentally mix booking-date and stay-date grains.
--
-- Idempotent.

-- ---------------------------------------------------------------------------
-- GST resolution: rate depends on BOTH the stay date and the nightly rate.
-- Indian folios are GST-inclusive, so a rate metric computed off invoice totals
-- is 5-18% overstated. This makes de-grossing a single function call.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION meta.gst_pct(p_stay_date date, p_nightly_rate numeric)
RETURNS numeric
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
               WHEN p_nightly_rate > g.threshold_inr THEN g.rate_above
               ELSE g.rate_at_or_below
           END
    FROM meta.gst_rate g
    WHERE p_stay_date >= g.effective_from
      AND (g.effective_to IS NULL OR p_stay_date <= g.effective_to)
    ORDER BY g.effective_from DESC
    LIMIT 1
$$;

COMMENT ON FUNCTION meta.gst_pct(date, numeric) IS
    'GST percentage for a room-night, resolved by stay date and nightly rate. '
    'Crosses the 22 Sep 2025 change and the INR 7,500 threshold.';


-- ---------------------------------------------------------------------------
-- v_unit_night_enriched — the atomic grain, with GST resolved.
-- Everything rate-related derives from here.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_unit_night_enriched AS
SELECT
    un.unit_night_key,
    un.unit_key,
    un.property_key,
    un.booking_key,
    un.channel_key,
    un.stay_date,
    un.date_key,
    un.is_sellable,
    un.is_occupied,
    un.is_out_of_order,
    un.is_complimentary,
    un.room_revenue_net_inr,
    un.commission_inr,
    meta.gst_pct(un.stay_date, un.room_revenue_net_inr)                    AS gst_pct,
    round(un.room_revenue_net_inr
          * meta.gst_pct(un.stay_date, un.room_revenue_net_inr) / 100.0, 2) AS gst_inr,
    round(un.room_revenue_net_inr
          * (1 + meta.gst_pct(un.stay_date, un.room_revenue_net_inr) / 100.0), 2)
                                                                            AS gross_incl_gst_inr,
    round(un.room_revenue_net_inr - un.commission_inr, 2)                   AS net_after_commission_inr,
    b.stay_type
FROM mart.fact_unit_night un
LEFT JOIN mart.fact_booking b ON b.booking_key = un.booking_key;

COMMENT ON VIEW mart.v_unit_night_enriched IS
    'Atomic unit-night grain with GST resolved per night. The single source for '
    'occupancy, ADR and RevPAR, so they cannot diverge on denominators.';


-- ---------------------------------------------------------------------------
-- v_daily_kpi — one row per property per STAY DATE. The operational heartbeat.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_daily_kpi AS
WITH base AS (
    SELECT
        e.property_key,
        e.stay_date,
        count(*)                                                    AS unit_nights_physical,
        count(*) FILTER (WHERE e.is_sellable)                       AS rooms_available,
        count(*) FILTER (WHERE e.is_out_of_order)                   AS rooms_out_of_order,
        count(*) FILTER (WHERE e.is_occupied)                       AS rooms_sold,
        count(*) FILTER (WHERE e.is_occupied AND e.stay_type = 'microstay')
                                                                    AS rooms_sold_microstay,
        sum(e.room_revenue_net_inr)                                 AS room_revenue_net_inr,
        sum(e.gst_inr)                                              AS gst_inr,
        sum(e.gross_incl_gst_inr)                                   AS room_revenue_gross_inr,
        sum(e.commission_inr)                                       AS commission_inr,
        sum(e.net_after_commission_inr)                             AS net_after_commission_inr,
        sum(e.room_revenue_net_inr) FILTER (WHERE e.stay_type <> 'microstay')
                                                                    AS revenue_excl_microstay_inr,
        count(*) FILTER (WHERE e.is_occupied AND e.stay_type <> 'microstay')
                                                                    AS rooms_sold_excl_microstay
    FROM mart.v_unit_night_enriched e
    GROUP BY 1, 2
)
SELECT
    b.property_key,
    p.property_code,
    p.property_name,
    b.stay_date,
    d.date_key,
    d.year_month,
    d.day_name,
    d.is_weekend,
    d.same_day_last_year,
    b.rooms_available,
    b.rooms_out_of_order,
    b.unit_nights_physical,
    b.rooms_sold,
    b.rooms_sold_microstay,
    b.room_revenue_net_inr,
    b.gst_inr,
    b.room_revenue_gross_inr,
    b.commission_inr,
    b.net_after_commission_inr,

    -- Occupancy, operational basis: OOO removed from availability.
    CASE WHEN b.rooms_available > 0
         THEN round(100.0 * b.rooms_sold / b.rooms_available, 2) END       AS occupancy_pct,
    -- Occupancy, benchmark basis: full physical inventory in the denominator.
    CASE WHEN b.unit_nights_physical > 0
         THEN round(100.0 * b.rooms_sold / b.unit_nights_physical, 2) END  AS occupancy_pct_benchmark,
    -- The gap IS the inventory lost to out-of-order units.
    CASE WHEN b.rooms_available > 0 AND b.unit_nights_physical > 0
         THEN round(100.0 * b.rooms_sold / b.rooms_available
                  - 100.0 * b.rooms_sold / b.unit_nights_physical, 2) END  AS occupancy_ooo_gap_pp,

    CASE WHEN b.rooms_sold > 0
         THEN round(b.room_revenue_net_inr / b.rooms_sold, 2) END          AS adr_inr,
    CASE WHEN b.rooms_sold_excl_microstay > 0
         THEN round(b.revenue_excl_microstay_inr / b.rooms_sold_excl_microstay, 2) END
                                                                          AS adr_excl_microstay_inr,
    CASE WHEN b.rooms_available > 0
         THEN round(b.room_revenue_net_inr / b.rooms_available, 2) END     AS revpar_inr
FROM base b
JOIN mart.dim_property p ON p.property_key = b.property_key
JOIN mart.dim_date d     ON d.full_date    = b.stay_date;

COMMENT ON VIEW mart.v_daily_kpi IS
    'Property x stay-date KPI grain. Publishes occupancy on both the operational '
    'and benchmark bases plus the gap between them, and ADR both including and '
    'excluding hourly microstays -- a two-hour booking counted as a nightly stay '
    'silently destroys ADR.';


-- ---------------------------------------------------------------------------
-- v_booking_kpi — BOOKING-DATE grain. What was sold, not what was stayed.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_booking_kpi AS
SELECT
    b.booking_key,
    b.booking_id,
    b.property_key,
    p.property_code,
    b.channel_key,
    c.channel_code,
    c.channel_name,
    c.channel_type,
    b.guest_key,
    g.guest_segment,
    b.booking_date,
    meta.business_date(b.booked_at)                                AS booking_date_derived,
    b.booking_date <> meta.business_date(b.booked_at)              AS has_business_date_drift,
    b.check_in_date,
    b.check_out_date,
    b.cancel_date,
    b.stay_type,
    b.status,
    b.status = 'cancelled'                                         AS is_cancelled,
    b.nights,
    b.lead_time_days,
    b.gross_amount_inr,
    b.discount_inr,
    b.net_room_amount_inr,
    b.commission_inr,
    round(b.net_room_amount_inr - b.commission_inr, 2)             AS net_after_commission_inr,
    CASE WHEN b.nights > 0
         THEN round(b.net_room_amount_inr / b.nights, 2)
         ELSE b.net_room_amount_inr END                            AS nightly_rate_inr
FROM mart.fact_booking b
JOIN mart.dim_property p ON p.property_key = b.property_key
JOIN mart.dim_channel  c ON c.channel_key  = b.channel_key
LEFT JOIN mart.dim_guest g ON g.guest_key  = b.guest_key;

COMMENT ON VIEW mart.v_booking_kpi IS
    'Booking grain on BOOKING DATE. has_business_date_drift exposes rows whose '
    'stored reporting date disagrees with the derived IST business date.';


-- ---------------------------------------------------------------------------
-- v_service_kpi — service-request grain, with both SLA clocks explicit.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_service_kpi AS
SELECT
    sr.request_key,
    sr.request_id,
    sr.property_key,
    p.property_code,
    sr.request_type_key,
    rt.category,
    rt.subcategory,
    rt.owning_team,
    sr.request_date,
    meta.business_date(sr.created_at)                                     AS request_date_derived,
    sr.resolved_date,
    sr.created_at,
    sr.resolved_at,
    extract(hour FROM sr.created_at AT TIME ZONE 'Asia/Kolkata')::int     AS created_hour_ist,
    CASE
        WHEN extract(hour FROM sr.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN  6 AND 11 THEN 'morning'
        WHEN extract(hour FROM sr.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN 12 AND 17 THEN 'afternoon'
        WHEN extract(hour FROM sr.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN 18 AND 23 THEN 'evening'
        ELSE 'overnight'
    END                                                                   AS day_part_ist,
    sr.priority,
    sr.status,
    sr.channel,
    sr.sla_minutes,
    sr.resolution_minutes,
    round(EXTRACT(EPOCH FROM (sr.first_response_at - sr.created_at)) / 60.0, 1)
                                                                          AS first_response_minutes,
    sr.is_sla_breached,
    sr.reopened_count,
    sr.csat_score
FROM mart.fact_service_request sr
JOIN mart.dim_property     p  ON p.property_key     = sr.property_key
JOIN mart.dim_request_type rt ON rt.request_type_key = sr.request_type_key;

COMMENT ON VIEW mart.v_service_kpi IS
    'Service-request grain. day_part_ist buckets on IST, not UTC -- bucketing an '
    'evening problem on UTC hours moves it to the afternoon and hides it.';


-- ---------------------------------------------------------------------------
-- v_guest_repeat — repeat behaviour before and after identity resolution.
-- The two numbers differ, and the difference is the finding.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_guest_repeat AS
WITH raw_stays AS (
    SELECT guest_key, count(*) AS stays
    FROM mart.fact_booking
    WHERE status IN ('checked_out','checked_in')
    GROUP BY 1
),
resolved AS (
    -- Deterministic identity resolution: normalised phone, else normalised
    -- email, else the guest key itself. Splink-style probabilistic matching is
    -- the production upgrade path; deterministic normalisation already produces
    -- the business headline.
    SELECT
        COALESCE(NULLIF(g.phone_last10, ''), g.email_normalised, g.guest_key::text) AS identity_key,
        b.booking_key
    FROM mart.fact_booking b
    JOIN mart.dim_guest g ON g.guest_key = b.guest_key
    WHERE b.status IN ('checked_out','checked_in')
),
resolved_stays AS (
    SELECT identity_key, count(*) AS stays FROM resolved GROUP BY 1
)
SELECT
    (SELECT count(*) FROM raw_stays)                                       AS guests_raw,
    (SELECT count(*) FROM raw_stays WHERE stays >= 2)                      AS repeat_guests_raw,
    (SELECT round(100.0 * count(*) FILTER (WHERE stays >= 2) / NULLIF(count(*), 0), 2)
       FROM raw_stays)                                                     AS repeat_rate_raw_pct,
    (SELECT count(*) FROM resolved_stays)                                  AS guests_resolved,
    (SELECT count(*) FROM resolved_stays WHERE stays >= 2)                 AS repeat_guests_resolved,
    (SELECT round(100.0 * count(*) FILTER (WHERE stays >= 2) / NULLIF(count(*), 0), 2)
       FROM resolved_stays)                                                AS repeat_rate_resolved_pct;

COMMENT ON VIEW mart.v_guest_repeat IS
    'Repeat-guest rate before and after deterministic identity resolution. '
    'Duplicate guest profiles understate loyalty; the delta quantifies by how much.';


-- ---------------------------------------------------------------------------
-- Metric registry. sql_expression is the definition consumers execute.
-- ---------------------------------------------------------------------------
DELETE FROM meta.metric_definition;

INSERT INTO meta.metric_definition (
    metric_key, display_name, business_definition, formula_text, sql_expression,
    powerbi_expression, grain, date_basis, unit, revenue_basis,
    includes_comp_units, includes_ooo_in_denom, includes_microstays,
    source_tables, inclusion_rules, exclusion_rules, caveats, owner_team
) VALUES

('occupancy_pct', 'Occupancy %',
 'Share of sellable unit-nights that were actually occupied. The operational basis: units that cannot be sold are not counted as missed sales.',
 'Rooms Sold / Rooms Available x 100',
 'round(100.0 * count(*) FILTER (WHERE is_occupied) / NULLIF(count(*) FILTER (WHERE is_sellable), 0), 2)',
 'DIVIDE([Rooms Sold], [Rooms Available]) -- format as %',
 'property x stay_date', 'stay_date', 'percent', 'not_applicable',
 true, false, true,
 ARRAY['mart.fact_unit_night'],
 'Every unit-night where is_sellable. Complimentary and house-use nights count as sold.',
 'Out-of-order unit-nights are EXCLUDED from the denominator.',
 'Differs from occupancy_pct_benchmark by the share of inventory out of order. Report the basis alongside the number or the two figures will be read as an error.',
 'Revenue'),

('occupancy_pct_benchmark', 'Occupancy % (benchmark basis)',
 'Occupancy against full physical inventory, the basis used for STR-style external comparison.',
 'Rooms Sold / Physical Unit-Nights x 100',
 'round(100.0 * count(*) FILTER (WHERE is_occupied) / NULLIF(count(*), 0), 2)',
 'DIVIDE([Rooms Sold], [Physical Unit Nights])',
 'property x stay_date', 'stay_date', 'percent', 'not_applicable',
 true, true, true,
 ARRAY['mart.fact_unit_night'],
 'All physical unit-nights for an open property.',
 'Nothing excluded from the denominator.',
 'Always lower than the operational basis. Deliberately published alongside it so the gap is visible rather than surprising.',
 'Revenue'),

('adr_inr', 'ADR',
 'Average Daily Rate: net room revenue per occupied unit-night, excluding tax.',
 'Net Room Revenue / Rooms Sold',
 'round(sum(room_revenue_net_inr) / NULLIF(count(*) FILTER (WHERE is_occupied), 0), 2)',
 'DIVIDE([Net Room Revenue], [Rooms Sold])',
 'property x stay_date', 'stay_date', 'inr', 'net_of_tax',
 true, false, true,
 ARRAY['mart.fact_unit_night'],
 'Occupied unit-nights only.',
 'GST excluded. OTA commission NOT deducted -- see adr_net_of_commission_inr.',
 'Includes hourly microstays, which bill at roughly 28% of a nightly rate and pull ADR down. adr_excl_microstay_inr is published alongside for that reason.',
 'Revenue'),

('adr_excl_microstay_inr', 'ADR (excluding microstays)',
 'ADR over nightly stays only, excluding hourly microstay inventory.',
 'Net Room Revenue (nightly) / Rooms Sold (nightly)',
 'round(sum(room_revenue_net_inr) FILTER (WHERE stay_type <> ''microstay'') / NULLIF(count(*) FILTER (WHERE is_occupied AND stay_type <> ''microstay''), 0), 2)',
 'CALCULATE([ADR], fact_booking[stay_type] <> "microstay")',
 'property x stay_date', 'stay_date', 'inr', 'net_of_tax',
 true, false, false,
 ARRAY['mart.fact_unit_night','mart.fact_booking'],
 'Occupied unit-nights where stay_type <> microstay.',
 'Hourly microstays excluded from both numerator and denominator.',
 'The honest comparison figure when benchmarking against nightly-only operators.',
 'Revenue'),

('revpar_inr', 'RevPAR',
 'Revenue per Available Room-night: the single number that reflects both rate and volume.',
 'Net Room Revenue / Rooms Available  ==  ADR x Occupancy',
 'round(sum(room_revenue_net_inr) / NULLIF(count(*) FILTER (WHERE is_sellable), 0), 2)',
 'DIVIDE([Net Room Revenue], [Rooms Available])',
 'property x stay_date', 'stay_date', 'inr', 'net_of_tax',
 true, false, true,
 ARRAY['mart.fact_unit_night'],
 'Sellable unit-nights in the denominator, all room revenue in the numerator.',
 'Out-of-order nights excluded from the denominator, matching occupancy_pct.',
 'RevPAR = ADR x Occupancy holds exactly ONLY because all three read the same table and the same denominator. Asserted in tests. RevPAR is also indifferent between 100% at INR 3,000 and 60% at INR 5,000, which have very different cost per occupied unit -- do not optimise it alone.',
 'Revenue'),

('room_revenue_net_inr', 'Net Room Revenue',
 'Room revenue net of GST and net of discount, before OTA commission.',
 'SUM(room revenue, net of tax)',
 'sum(room_revenue_net_inr)',
 'SUM(fact_unit_night[room_revenue_net_inr])',
 'property x stay_date', 'stay_date', 'inr', 'net_of_tax',
 true, false, true,
 ARRAY['mart.fact_unit_night'],
 'All occupied unit-nights.',
 'GST excluded. Discounts already deducted.',
 'Recognised on STAY DATE, not booking or payment date. Marketing reporting on booking date and Finance on payment date will legitimately produce different totals for the same month.',
 'Revenue'),

('room_revenue_gross_inr', 'Gross Room Revenue (incl GST)',
 'What the guest actually paid, including GST at the rate applicable to that stay date and nightly rate.',
 'Net Room Revenue x (1 + GST%)',
 'sum(gross_incl_gst_inr)',
 'SUM(v_unit_night_enriched[gross_incl_gst_inr])',
 'property x stay_date', 'stay_date', 'inr', 'gross_incl_tax',
 true, false, true,
 ARRAY['mart.v_unit_night_enriched','meta.gst_rate'],
 'GST resolved per night by stay date and nightly rate.',
 'Nothing excluded.',
 'Spans the 22 Sep 2025 GST change (12% slab abolished; 5% no-ITC at or below INR 7,500, 18% with ITC above). An apparent step in gross revenue across that date is a TAX artefact, not performance -- which is why the net measure is the one used for rate metrics.',
 'Finance'),

('cancellation_rate_pct', 'Cancellation Rate',
 'Share of bookings made in the period that were subsequently cancelled.',
 'Cancelled Bookings / Bookings Made x 100',
 'round(100.0 * count(*) FILTER (WHERE status = ''cancelled'') / NULLIF(count(*), 0), 2)',
 'DIVIDE([Cancelled Bookings], [Bookings Made])',
 'booking', 'booking_date', 'percent', 'not_applicable',
 NULL, NULL, true,
 ARRAY['mart.fact_booking'],
 'All bookings whose BOOKING date falls in the period.',
 'No-shows are a separate status and are NOT counted as cancellations.',
 'Deliberately measured on cohort basis -- cancellations OF bookings made in the period -- not cancellations occurring in the period. The two give materially different numbers and answer different questions: cohort basis measures booking quality, event basis measures this period''s revenue loss. State which one you mean.',
 'Revenue'),

('channel_mix_pct', 'Channel Mix',
 'Share of room-nights sold through each distribution channel.',
 'Room-nights per channel / Total room-nights x 100',
 'round(100.0 * count(*) FILTER (WHERE is_occupied) OVER (PARTITION BY channel_key) / NULLIF(count(*) FILTER (WHERE is_occupied) OVER (), 0), 2)',
 'DIVIDE([Rooms Sold], CALCULATE([Rooms Sold], ALL(dim_channel)))',
 'channel x stay_date', 'stay_date', 'percent', 'not_applicable',
 true, false, true,
 ARRAY['mart.fact_unit_night','mart.dim_channel'],
 'Occupied unit-nights, attributed to the booking''s channel.',
 'Cancelled bookings hold no inventory and so do not appear.',
 'Measured on room-nights, not booking count: corporate books longer stays, so a booking-count mix overstates OTA share.',
 'Revenue'),

('avg_lead_time_days', 'Average Booking Lead Time',
 'Mean days between booking and arrival. Short lead times leave less room to reprice.',
 'AVG(check_in_date - booking_date)',
 'round(avg(lead_time_days)::numeric, 1)',
 'AVERAGE(fact_booking[lead_time_days])',
 'booking', 'booking_date', 'days', 'not_applicable',
 NULL, NULL, false,
 ARRAY['mart.fact_booking'],
 'Non-cancelled bookings.',
 'Hourly microstays excluded -- they book minutes ahead and would collapse the mean.',
 'Indian booking windows run 7-21 days against a ~40-day global average, so pickup analysis here should use 0/1/3/7/14-day windows rather than the textbook 30/60/90.',
 'Revenue'),

('alos_nights', 'Average Length of Stay',
 'Mean nights per nightly reservation.',
 'Total Room-Nights / Nightly Bookings',
 'round(sum(nights)::numeric / NULLIF(count(*) FILTER (WHERE stay_type = ''nightly''), 0), 2)',
 'DIVIDE(SUM(fact_booking[nights]), [Nightly Bookings])',
 'booking', 'stay_date', 'nights', 'not_applicable',
 NULL, NULL, false,
 ARRAY['mart.fact_booking'],
 'Non-cancelled nightly bookings.',
 'Microstays and day-use excluded (zero nights by definition).',
 'Uses the half-open interval: departure night is not a night. Counting it inflates ALOS by 1 and room-nights by roughly 1/ALOS.',
 'Revenue'),

('service_tat_minutes', 'Service Turnaround Time',
 'Wall-clock minutes from a guest raising a request to its resolution.',
 'AVG(resolved_at - created_at)',
 'round(avg(resolution_minutes)::numeric, 1)',
 'AVERAGE(fact_service_request[resolution_minutes])',
 'service request', 'request_date', 'minutes', 'not_applicable',
 NULL, NULL, NULL,
 ARRAY['mart.fact_service_request'],
 'Resolved requests.',
 'Open requests excluded -- including them as zero would flatter the mean.',
 'WALL-CLOCK, not business hours. These are 24-hour serviced apartments and a guest waiting at 02:00 is still waiting. A business-hours clock would report this as excellent.',
 'Operations'),

('sla_breach_rate_pct', 'SLA Breach Rate',
 'Share of resolved requests that exceeded the target resolution time for their request type.',
 'Breached Requests / Resolved Requests x 100',
 'round(100.0 * count(*) FILTER (WHERE is_sla_breached) / NULLIF(count(*) FILTER (WHERE resolution_minutes IS NOT NULL), 0), 2)',
 'DIVIDE([Breached Requests], [Resolved Requests])',
 'service request', 'request_date', 'percent', 'not_applicable',
 NULL, NULL, NULL,
 ARRAY['mart.fact_service_request','mart.dim_request_type'],
 'Resolved requests with an SLA target.',
 'Open requests excluded.',
 'The blended rate hides property- and daypart-level failure. A 0.9pp portfolio move concealed a 2.6x degradation at one property in one shift. Always segment before concluding.',
 'Operations'),

('csat_avg', 'CSAT',
 'Mean guest satisfaction score on resolved service requests, 1-5.',
 'AVG(csat_score)',
 'round(avg(csat_score)::numeric, 2)',
 'AVERAGE(fact_service_request[csat_score])',
 'service request', 'request_date', 'score', 'not_applicable',
 NULL, NULL, NULL,
 ARRAY['mart.fact_service_request'],
 'Requests where the guest responded.',
 'Non-responders excluded -- imputing them would invent data.',
 'Response rate is roughly a third, so CSAT carries selection bias: guests who respond are not a random sample. Report the response rate next to the score.',
 'Customer Experience'),

('repeat_guest_rate_pct', 'Repeat Guest Rate',
 'Share of guests with two or more completed stays, after deterministic identity resolution.',
 'Guests with >=2 stays / Total guests x 100',
 'SELECT repeat_rate_resolved_pct FROM mart.v_guest_repeat',
 'DIVIDE([Repeat Guests], [Total Guests])',
 'guest', 'stay_date', 'percent', 'not_applicable',
 NULL, NULL, true,
 ARRAY['mart.fact_booking','mart.dim_guest'],
 'Guests with at least one completed stay, keyed on normalised phone then normalised email.',
 'Cancelled and no-show bookings do not count as stays.',
 'Published on the RESOLVED basis. The raw basis understates loyalty because duplicate profiles split one guest into several; mart.v_guest_repeat exposes both so the size of that understatement is visible.',
 'Customer Experience'),

('cost_per_booking_inr', 'Cost per Booking',
 'Directly attributable acquisition and processing cost per confirmed booking: OTA commission plus payment gateway fees and the GST charged on both.',
 '(Commission + Gateway Fee + GST on fees) / Confirmed Bookings',
 'round((sum(b.commission_inr) + COALESCE(sum(p.gateway_fee_inr), 0) + COALESCE(sum(p.gst_on_fee_inr), 0)) / NULLIF(count(DISTINCT b.booking_key), 0), 2)',
 'DIVIDE([Total Acquisition Cost], [Confirmed Bookings])',
 'booking', 'booking_date', 'inr', 'not_applicable',
 NULL, NULL, true,
 ARRAY['mart.fact_booking','mart.fact_payment','mart.dim_channel'],
 'Non-cancelled bookings with their commission and gateway costs.',
 'Staff, utilities, housekeeping consumables and fixed overhead are NOT included -- that data does not exist here.',
 'This is a DIRECT cost per booking, not a fully loaded cost, and must not be presented as one. GOPPAR is uncomputable on this dataset for the same reason: no departmental cost data. Saying so is more useful than publishing a fully-loaded-looking number built on invented overhead.',
 'Finance');

COMMENT ON TABLE meta.metric_definition IS
    'Executable metric registry: 16 metrics, each declaring its grain, date basis, '
    'revenue basis, inclusions, exclusions and known caveats. date_basis is '
    'CHECK-constrained, so a metric cannot be registered without stating which '
    'date it is measured on.';
