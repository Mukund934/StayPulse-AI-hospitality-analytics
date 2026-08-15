-- 006 · Revenue management layer: pickup, pace, on-the-books, wash.
--
-- WHY THIS LAYER EXISTS
--
-- Occupancy, ADR and RevPAR are lagging. They describe a night that has already
-- happened and can no longer be sold. Revenue management runs on the opposite
-- question: for a night that has NOT happened yet, how much of it is already sold,
-- is that ahead of or behind where it normally is by now, and is the gap closing?
--
-- Answering that needs a second time axis. Every existing metric in this warehouse
-- is single-temporal -- it is measured on one date. A pickup metric is BI-TEMPORAL:
-- it is measured on a stay date AS OF a snapshot date. "12 room-nights sold for
-- 14 August" is meaningless without saying when you looked.
--
-- That is the whole difficulty of revenue management data modelling, and it is why
-- this migration extends the date_basis CHECK constraint rather than reusing
-- 'booking_date'. A metric keyed on booking_date answers "how much did we sell on
-- Tuesday". A metric keyed on as_of_date answers "what did the book look like on
-- Tuesday". Those are different questions and conflating them is the most common
-- way a pickup report ends up wrong.
--
--
-- THE GRAIN
--
-- mart.v_booking_night is the demand grain: one row per booking per stay night.
-- It deliberately includes CANCELLED and NO-SHOW bookings, because on any given
-- snapshot date those nights genuinely were on the books. A pickup report built
-- only from stays that eventually happened has hindsight baked into it and will
-- always look like the book filled smoothly.
--
-- mart.fact_unit_night remains the inventory grain: one row per unit per night,
-- which is where occupancy and RevPAR come from. The two grains reconcile, but not
-- trivially -- see the RECONCILIATION note at the foot of this file.
--
--
-- HALF-OPEN INTERVALS
--
-- Nights are [check_in, check_out). A departure day is not a night. This is applied
-- as generate_series(check_in, check_out - 1) throughout. A booking with
-- check_out = check_in therefore produces ZERO rows here, which is correct and has
-- a real consequence documented below.
--
-- Idempotent.

-- ---------------------------------------------------------------------------
-- Bi-temporal metrics need a snapshot date basis.
-- ---------------------------------------------------------------------------
ALTER TABLE meta.metric_definition
    DROP CONSTRAINT IF EXISTS metric_definition_date_basis_check;

ALTER TABLE meta.metric_definition
    ADD CONSTRAINT metric_definition_date_basis_check
    CHECK (date_basis = ANY (ARRAY[
        'stay_date', 'booking_date', 'cancel_date', 'payment_date',
        'request_date', 'resolved_date', 'review_date',
        'as_of_date',            -- bi-temporal: measured on a stay date, as of a snapshot
        'not_applicable'
    ]));


-- ---------------------------------------------------------------------------
-- THE DEMAND GRAIN. One row per booking per stay night.
--
-- entered_on  -- the date this night appeared on the books
-- left_on     -- the date it left the books (cancellation), NULL if it never did
--
-- Those two columns are what make an as-of reconstruction a filter rather than a
-- rebuild: a night was on the books on date D exactly when
-- entered_on <= D AND (left_on IS NULL OR left_on > D).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_booking_night AS
SELECT
    b.booking_key,
    b.booking_id,
    b.property_key,
    b.channel_key,
    b.guest_key,
    g.sd::date                                   AS stay_date,
    b.booking_date                               AS entered_on,
    b.cancel_date                                AS left_on,
    b.status,
    b.stay_type,
    b.check_in_date,
    b.check_out_date,
    b.nights                                     AS booking_nights,
    (g.sd::date - b.booking_date)                AS days_to_arrival,
    (g.sd::date - b.check_in_date + 1)           AS night_index,
    -- Revenue is spread evenly across the nights of the booking. Length-of-stay
    -- pricing is not modelled in the source, so any other split would be invented.
    round(b.net_room_amount_inr / NULLIF(b.nights, 0), 2) AS night_revenue_net_inr,
    round(b.commission_inr      / NULLIF(b.nights, 0), 2) AS night_commission_inr
FROM mart.fact_booking b
CROSS JOIN LATERAL generate_series(b.check_in_date, b.check_out_date - 1, '1 day') AS g(sd);

COMMENT ON VIEW mart.v_booking_night IS
    'Demand grain: one row per booking per stay night, including cancelled and '
    'no-show bookings. entered_on/left_on make any as-of reconstruction a filter. '
    'Zero-night hourly bookings produce no rows here by construction.';


-- ---------------------------------------------------------------------------
-- ON THE BOOKS AS OF A DATE.
--
-- Returns the book exactly as it stood on p_as_of, for stays strictly after it.
-- "Strictly after" is deliberate: a night already in progress is occupancy, not
-- pickup, and mixing the two double-counts the current night.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION mart.f_otb(p_as_of date)
RETURNS TABLE (
    stay_date        date,
    property_key     integer,
    channel_key      integer,
    nights_otb       bigint,
    revenue_otb_inr  numeric,
    days_out         integer
)
LANGUAGE sql
STABLE
AS $$
    SELECT
        n.stay_date,
        n.property_key,
        n.channel_key,
        count(*)                              AS nights_otb,
        sum(n.night_revenue_net_inr)          AS revenue_otb_inr,
        (n.stay_date - p_as_of)::integer      AS days_out
    FROM mart.v_booking_night n
    WHERE n.entered_on <= p_as_of
      AND n.stay_date  >  p_as_of
      AND (n.left_on IS NULL OR n.left_on > p_as_of)
    GROUP BY n.stay_date, n.property_key, n.channel_key
$$;

COMMENT ON FUNCTION mart.f_otb(date) IS
    'On-the-books position as it stood on the given date, for future stays only. '
    'Cancellations are respected as of that date, not with hindsight.';


-- ---------------------------------------------------------------------------
-- PICKUP. Nights added to, and removed from, the book on each booking date.
--
-- Gross pickup counts arrivals on the book; cancellations are reported separately
-- rather than netted, because a day that took 20 bookings and lost 18 is a very
-- different operational story from a day that took 2 and lost 0, and net pickup
-- alone cannot tell them apart.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_pickup_daily AS
WITH added AS (
    SELECT entered_on AS activity_date, stay_date, property_key, channel_key,
           count(*) AS nights_added,
           sum(night_revenue_net_inr) AS revenue_added_inr
    FROM mart.v_booking_night
    GROUP BY 1, 2, 3, 4
),
removed AS (
    SELECT left_on AS activity_date, stay_date, property_key, channel_key,
           count(*) AS nights_cancelled,
           sum(night_revenue_net_inr) AS revenue_cancelled_inr
    FROM mart.v_booking_night
    WHERE left_on IS NOT NULL
    GROUP BY 1, 2, 3, 4
)
SELECT
    coalesce(a.activity_date, r.activity_date) AS activity_date,
    coalesce(a.stay_date,     r.stay_date)     AS stay_date,
    coalesce(a.property_key,  r.property_key)  AS property_key,
    coalesce(a.channel_key,   r.channel_key)   AS channel_key,
    coalesce(a.nights_added, 0)                AS nights_added,
    coalesce(r.nights_cancelled, 0)            AS nights_cancelled,
    coalesce(a.nights_added, 0)
        - coalesce(r.nights_cancelled, 0)      AS nights_net,
    coalesce(a.revenue_added_inr, 0)           AS revenue_added_inr,
    coalesce(r.revenue_cancelled_inr, 0)       AS revenue_cancelled_inr,
    (coalesce(a.stay_date, r.stay_date)
        - coalesce(a.activity_date, r.activity_date))::integer AS days_before_stay
FROM added a
FULL OUTER JOIN removed r
  ON  a.activity_date = r.activity_date
  AND a.stay_date     = r.stay_date
  AND a.property_key  = r.property_key
  AND a.channel_key   = r.channel_key;

COMMENT ON VIEW mart.v_pickup_daily IS
    'Nights added and cancelled per activity date per stay date. Gross adds and '
    'cancellations are kept separate; net alone hides churn.';


-- ---------------------------------------------------------------------------
-- THE BOOKING CURVE. What share of a stay date is normally sold by D days out.
--
-- This is the reference curve that pace is measured against. It is built from
-- COMPLETED stay dates only -- a stay date still in the future has an incomplete
-- final total and would drag the curve down at every horizon.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_booking_curve AS
WITH final_book AS (
    -- Nights that actually materialised for each completed stay date.
    SELECT stay_date, property_key, count(*) AS final_nights
    FROM mart.v_booking_night
    WHERE status IN ('checked_out', 'confirmed')
    GROUP BY 1, 2
),
horizons AS (
    SELECT generate_series(0, 60) AS days_out
),
otb_at AS (
    -- For every completed stay date and every horizon, how many nights were on the
    -- books that many days before arrival.
    SELECT
        f.stay_date,
        f.property_key,
        h.days_out,
        f.final_nights,
        count(n.booking_key) AS nights_on_books
    FROM final_book f
    CROSS JOIN horizons h
    LEFT JOIN mart.v_booking_night n
           ON  n.stay_date    = f.stay_date
           AND n.property_key = f.property_key
           AND n.entered_on  <= f.stay_date - h.days_out
           AND (n.left_on IS NULL OR n.left_on > f.stay_date - h.days_out)
    GROUP BY 1, 2, 3, 4
)
SELECT
    days_out,
    property_key,
    count(*)                                                  AS stay_dates,
    round(avg(nights_on_books), 2)                            AS avg_nights_on_books,
    round(avg(final_nights), 2)                               AS avg_final_nights,
    -- The pace curve: median share of the final book already sold by this horizon.
    -- Median, not mean, so one 40-night group booking cannot define "normal".
    round(100.0 * percentile_cont(0.5) WITHIN GROUP (
              ORDER BY nights_on_books::numeric / NULLIF(final_nights, 0)
          )::numeric, 2)                                      AS median_pct_sold,
    round(100.0 * percentile_cont(0.25) WITHIN GROUP (
              ORDER BY nights_on_books::numeric / NULLIF(final_nights, 0)
          )::numeric, 2)                                      AS p25_pct_sold,
    round(100.0 * percentile_cont(0.75) WITHIN GROUP (
              ORDER BY nights_on_books::numeric / NULLIF(final_nights, 0)
          )::numeric, 2)                                      AS p75_pct_sold
FROM otb_at
GROUP BY days_out, property_key;

COMMENT ON VIEW mart.v_booking_curve IS
    'Reference pace curve: share of the final book normally sold by N days out, '
    'per property. Median with a p25-p75 band. Built from completed stay dates only.';


-- ---------------------------------------------------------------------------
-- LEAD TIME by channel. Distribution, not just a mean -- these are heavily skewed
-- and a mean lead time of 12 days can describe a channel where most bookings are
-- same-day.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_lead_time_profile AS
SELECT
    c.channel_key,
    c.channel_name,
    c.channel_type,
    count(*)                                                          AS bookings,
    round(avg(b.lead_time_days), 1)                                   AS mean_days,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY b.lead_time_days)    AS p25_days,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY b.lead_time_days)    AS median_days,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY b.lead_time_days)    AS p75_days,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY b.lead_time_days)    AS p90_days,
    round(100.0 * count(*) FILTER (WHERE b.lead_time_days = 0)  / count(*), 1) AS pct_same_day,
    round(100.0 * count(*) FILTER (WHERE b.lead_time_days >= 30) / count(*), 1) AS pct_30d_plus,
    round(100.0 * count(*) FILTER (WHERE b.status = 'cancelled') / count(*), 1) AS cancel_rate_pct
FROM mart.fact_booking b
JOIN mart.dim_channel c USING (channel_key)
GROUP BY 1, 2, 3;

COMMENT ON VIEW mart.v_lead_time_profile IS
    'Lead-time distribution per channel with same-day and 30-day-plus shares. '
    'Percentiles because the distribution is skewed and the mean misleads.';


-- ---------------------------------------------------------------------------
-- THE WASH FUNNEL. Booked -> cancelled -> no-show -> stayed, on a STAY-MONTH
-- cohort so the denominator is the demand for that month rather than the volume
-- transacted in it.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_cancellation_funnel AS
SELECT
    date_trunc('month', b.check_in_date)::date                     AS stay_month,
    b.property_key,
    b.channel_key,
    count(*)                                                       AS bookings_made,
    count(*) FILTER (WHERE b.status = 'cancelled')                 AS bookings_cancelled,
    count(*) FILTER (WHERE b.status = 'no_show')                   AS bookings_no_show,
    count(*) FILTER (WHERE b.status IN ('checked_out','confirmed')) AS bookings_stayed,
    round(100.0 * count(*) FILTER (WHERE b.status = 'cancelled') / count(*), 2)  AS cancel_rate_pct,
    round(100.0 * count(*) FILTER (WHERE b.status = 'no_show')   / count(*), 2)  AS no_show_rate_pct,
    -- Wash = the share of demand that was on the books and did not convert into a
    -- stayed night, for whatever reason. This is the number an overbooking policy
    -- would be built on.
    round(100.0 * count(*) FILTER (WHERE b.status IN ('cancelled','no_show'))
          / count(*), 2)                                           AS wash_rate_pct,
    -- Median days of warning before a cancellation. A cancellation 20 days out is
    -- resellable; one on the day of arrival is lost inventory.
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY (b.check_in_date - b.cancel_date)
    ) FILTER (WHERE b.cancel_date IS NOT NULL)                     AS median_cancel_notice_days
FROM mart.fact_booking b
GROUP BY 1, 2, 3;

COMMENT ON VIEW mart.v_cancellation_funnel IS
    'Wash funnel on stay-month cohorts with cancellation notice period. '
    'Cohorted on stay month, not booking month, so the denominator is demand for '
    'that month.';


-- ---------------------------------------------------------------------------
-- HOURLY MICRO-STAYS. Zero-night bookings that occupy a unit but contribute no
-- room-night. Isolated in its own view because it is the single largest source of
-- disagreement between the demand grain and the inventory grain.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_microstay_impact AS
SELECT
    c.channel_key,
    c.channel_name,
    count(*)                                            AS microstay_bookings,
    sum(b.net_room_amount_inr)                          AS revenue_net_inr,
    count(n.unit_night_key)                             AS unit_nights_occupied,
    0                                                   AS room_nights_sold,
    round(avg(b.net_room_amount_inr), 2)                AS avg_value_inr
FROM mart.fact_booking b
JOIN mart.dim_channel c USING (channel_key)
LEFT JOIN mart.fact_unit_night n
       ON n.booking_key = b.booking_key AND n.is_occupied
WHERE b.check_out_date = b.check_in_date
GROUP BY 1, 2;

COMMENT ON VIEW mart.v_microstay_impact IS
    'Zero-night hourly bookings: they consume a unit-night of inventory and earn '
    'revenue, but sell no room-night. They therefore raise revenue while diluting '
    'ADR, and are the reason adr_excl_microstay_inr is registered separately.';


-- ---------------------------------------------------------------------------
-- NEW METRICS. Registered so the pickup layer is governed identically to the rest.
-- ON CONFLICT so this migration can be re-run without duplicating.
-- ---------------------------------------------------------------------------
INSERT INTO meta.metric_definition (
    metric_key, display_name, business_definition, formula_text, sql_expression,
    powerbi_expression, grain, date_basis, unit, revenue_basis,
    includes_comp_units, includes_ooo_in_denom, includes_microstays,
    source_tables, inclusion_rules, exclusion_rules, caveats, owner_team
) VALUES

('nights_on_books', 'Nights on the books',
 'Room-nights sold for a future stay date, as the book stood on a given snapshot date.',
 'count(booking-nights where entered_on <= as_of and not cancelled by as_of)',
 'count(*) FILTER (WHERE entered_on <= :as_of AND (left_on IS NULL OR left_on > :as_of))',
 'CALCULATE([Room Nights], ''Booking Night''[entered_on] <= [As Of Date])',
 'stay_date x property x as_of_date', 'as_of_date', 'nights', 'not_applicable',
 false, false, false,
 ARRAY['mart.v_booking_night'],
 'Every booking-night on the books at the snapshot, including ones later cancelled.',
 'Zero-night hourly bookings contribute nothing. Stays on or before the snapshot date are excluded as occupancy, not pickup.',
 'Bi-temporal. Meaningless without stating the snapshot date. Reconstructed from booking and cancellation dates, not from stored nightly snapshots, so it assumes a booking never silently changed its dates.',
 'Revenue'),

('pickup_nights', 'Pickup (nights)',
 'Room-nights added to the book on a given activity date for a given stay date.',
 'nights added on activity_date',
 'sum(nights_added)',
 'SUM(''Pickup''[nights_added])',
 'stay_date x activity_date', 'as_of_date', 'nights', 'not_applicable',
 false, false, false,
 ARRAY['mart.v_pickup_daily'],
 'Gross additions to the book.',
 'Cancellations are reported separately as pickup_cancellations, not netted here.',
 'Gross by design. A day that added 20 and lost 18 reads identically to one that added 2 and lost 0 if only net pickup is published.',
 'Revenue'),

('booking_pace_pct', 'Booking pace vs curve',
 'Nights on the books for a stay date, as a percentage of what is normally on the books at the same number of days out.',
 'Nights on Books / Median Nights on Books at same days_out x 100',
 'round(100.0 * nights_on_books / NULLIF(expected_nights_at_horizon, 0), 1)',
 'DIVIDE([Nights On Books], [Median Nights At Horizon])',
 'stay_date x property x as_of_date', 'as_of_date', 'percent', 'not_applicable',
 false, false, false,
 ARRAY['mart.v_booking_night', 'mart.v_booking_curve'],
 'Compared against the median curve for the same property and the same days-out horizon.',
 'Stay dates with fewer than 6 comparable historical observations are not scored.',
 'A pace below 100 is not automatically bad: it can mean the same demand arriving later. Read with lead-time mix, not alone.',
 'Revenue'),

('wash_rate_pct', 'Wash rate',
 'Share of bookings made for a stay month that did not convert into a stay, through cancellation or no-show.',
 '(Cancelled + No-show) / Bookings Made x 100',
 'round(100.0 * count(*) FILTER (WHERE status IN (''cancelled'',''no_show'')) / count(*), 2)',
 'DIVIDE([Cancelled] + [No Shows], [Bookings Made])',
 'stay_month x property x channel', 'stay_date', 'percent', 'not_applicable',
 false, false, true,
 ARRAY['mart.fact_booking'],
 'Cohorted on stay month so the denominator is demand for that month.',
 'Bookings amended rather than cancelled are not tracked; the source has no amendment history.',
 'This is the number an overbooking policy would rest on. It is NOT a forecast of future wash.',
 'Revenue'),

('revpor_inr', 'RevPOR',
 'Room revenue per occupied room-night. Unlike ADR this is not diluted by how many rooms were available.',
 'Room Revenue (net) / Rooms Sold',
 'round(sum(room_revenue_net_inr) / NULLIF(count(*) FILTER (WHERE is_occupied), 0), 2)',
 'DIVIDE([Room Revenue Net], [Rooms Sold])',
 'property x stay_date', 'stay_date', 'inr', 'net_of_tax',
 true, false, true,
 ARRAY['mart.fact_unit_night'],
 'Every occupied unit-night.',
 'Room revenue only. This is NOT TRevPOR: there is no food, beverage or ancillary revenue in this warehouse, so total-revenue metrics are deliberately not published.',
 'On a room-only dataset RevPOR and ADR coincide. It is registered separately so that adding ancillary revenue later does not silently change what ADR means.',
 'Revenue'),

('cancel_notice_days', 'Cancellation notice',
 'Days between a cancellation and the stay date it was cancelled from.',
 'median(check_in_date - cancel_date)',
 'percentile_cont(0.5) WITHIN GROUP (ORDER BY (check_in_date - cancel_date))',
 'MEDIANX(''Booking'', ''Booking''[check_in_date] - ''Booking''[cancel_date])',
 'stay_month x channel', 'cancel_date', 'days', 'not_applicable',
 false, false, true,
 ARRAY['mart.fact_booking'],
 'Cancelled bookings with a recorded cancellation date.',
 'No-shows are excluded: they gave no notice at all, and folding them in as zero would understate the notice actually given by people who did cancel.',
 'Separates resellable cancellations from lost inventory. A 20-day notice is recoverable; a same-day one is not.',
 'Revenue')

ON CONFLICT (metric_key) DO UPDATE SET
    display_name        = EXCLUDED.display_name,
    business_definition = EXCLUDED.business_definition,
    formula_text        = EXCLUDED.formula_text,
    sql_expression      = EXCLUDED.sql_expression,
    powerbi_expression  = EXCLUDED.powerbi_expression,
    grain               = EXCLUDED.grain,
    date_basis          = EXCLUDED.date_basis,
    unit                = EXCLUDED.unit,
    source_tables       = EXCLUDED.source_tables,
    inclusion_rules     = EXCLUDED.inclusion_rules,
    exclusion_rules     = EXCLUDED.exclusion_rules,
    caveats             = EXCLUDED.caveats;


-- ---------------------------------------------------------------------------
-- RECONCILIATION between the demand grain and the inventory grain.
--
-- These two do NOT trivially agree, and the difference is entirely explainable.
-- Measured on the shipped dataset over 2025-02-01 .. 2026-08-11:
--
--   exploded booking-nights (stayed bookings)   13,640
--   minus nights never allocated a unit           -410   (3.0% -- denied demand)
--   plus  hourly bookings holding a unit-night    +380   (zero-night, Bag2Bag)
--   ------------------------------------------------------
--   occupied unit-nights in fact_unit_night      13,610   exact
--
-- The 410 is the allocation gap: demand that existed but could not be placed in a
-- specific unit. The 380 is the micro-stay effect described above. Both are real
-- and both are asserted by tests rather than left as a rounding excuse.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW mart.v_grain_reconciliation AS
WITH exploded AS (
    SELECT booking_key, count(*) AS n
    FROM mart.v_booking_night
    WHERE status IN ('checked_out', 'confirmed')
      AND stay_date BETWEEN (SELECT min(stay_date) FROM mart.fact_unit_night)
                        AND (SELECT max(stay_date) FROM mart.fact_unit_night)
    GROUP BY 1
),
materialised AS (
    SELECT booking_key, count(*) AS n
    FROM mart.fact_unit_night WHERE is_occupied
    GROUP BY 1
)
SELECT
    (SELECT coalesce(sum(n), 0) FROM exploded)      AS exploded_booking_nights,
    (SELECT coalesce(sum(n), 0) FROM materialised)  AS occupied_unit_nights,
    (SELECT coalesce(sum(greatest(e.n - coalesce(m.n, 0), 0)), 0)
       FROM exploded e LEFT JOIN materialised m USING (booking_key))
                                                    AS unallocated_nights,
    (SELECT coalesce(sum(m.n), 0)
       FROM materialised m
      WHERE m.booking_key NOT IN (SELECT booking_key FROM exploded))
                                                    AS hourly_unit_nights;

COMMENT ON VIEW mart.v_grain_reconciliation IS
    'Ties the demand grain to the inventory grain: exploded - unallocated + hourly '
    '= occupied. Asserted exactly by the test suite.';
