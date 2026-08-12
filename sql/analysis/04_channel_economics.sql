-- BUSINESS QUESTION
--   "Which channel is actually worth having? Gross revenue flatters the OTAs."
--
-- WHY IT MATTERS
--   OTA commission is charged on the pre-tax room rate, and then GST is charged on
--   the commission itself -- so gross-to-net is a two-step calculation, not one.
--   Ranking channels on gross room revenue systematically overstates the OTAs and
--   understates direct and corporate business.
--
--   Cancellation rate is measured on a COHORT basis (cancellations OF bookings
--   made in the period) because the question is about booking quality per channel,
--   not about this month's revenue loss.
--
-- DATE BASIS: booking_date for booking-level metrics, stay_date for room-nights.

WITH booking_side AS (
    SELECT
        channel_code,
        channel_name,
        channel_type,
        count(*)                                                     AS bookings_made,
        count(*) FILTER (WHERE is_cancelled)                         AS cancelled,
        round(avg(lead_time_days)::numeric, 1)                       AS avg_lead_days,
        round(avg(nights) FILTER (WHERE stay_type = 'nightly')::numeric, 2) AS avg_los,
        sum(commission_inr)                                          AS commission_inr
    FROM mart.v_booking_kpi
    GROUP BY 1, 2, 3
),
stay_side AS (
    SELECT
        c.channel_code,
        count(*)                                                     AS room_nights,
        sum(e.room_revenue_net_inr)                                  AS revenue_net_inr,
        sum(e.commission_inr)                                        AS commission_on_stays_inr
    FROM mart.v_unit_night_enriched e
    JOIN mart.dim_channel c ON c.channel_key = e.channel_key
    WHERE e.is_occupied
    GROUP BY 1
),
fees AS (
    SELECT
        c.channel_code,
        COALESCE(sum(p.gateway_fee_inr), 0)                          AS gateway_fee_inr,
        COALESCE(sum(p.gst_on_fee_inr), 0)                           AS gst_on_fee_inr
    FROM mart.fact_payment p
    JOIN mart.fact_booking b ON b.booking_key = p.booking_key
    JOIN mart.dim_channel  c ON c.channel_key = b.channel_key
    GROUP BY 1
)
SELECT
    b.channel_code,
    b.channel_name,
    b.channel_type,
    b.bookings_made,
    round(100.0 * b.cancelled / b.bookings_made, 1)                  AS cancellation_pct,
    b.avg_lead_days,
    b.avg_los,
    s.room_nights,
    round(100.0 * s.room_nights / SUM(s.room_nights) OVER (), 1)     AS room_night_share_pct,
    round(s.revenue_net_inr)                                         AS revenue_net_inr,
    round(s.revenue_net_inr / NULLIF(s.room_nights, 0), 2)           AS adr_inr,
    -- Commission, plus GST charged ON the commission (18% intermediary service).
    round(s.commission_on_stays_inr)                                 AS commission_inr,
    round(s.commission_on_stays_inr * 0.18)                          AS gst_on_commission_inr,
    round(COALESCE(f.gateway_fee_inr, 0) + COALESCE(f.gst_on_fee_inr, 0))
                                                                     AS gateway_cost_inr,
    -- What the business actually keeps per room-night.
    round((s.revenue_net_inr
           - s.commission_on_stays_inr
           - s.commission_on_stays_inr * 0.18
           - COALESCE(f.gateway_fee_inr, 0)
           - COALESCE(f.gst_on_fee_inr, 0)) / NULLIF(s.room_nights, 0), 2)
                                                                     AS net_revpan_inr,
    -- Direct acquisition cost per confirmed booking.
    round((s.commission_on_stays_inr * 1.18
           + COALESCE(f.gateway_fee_inr, 0) + COALESCE(f.gst_on_fee_inr, 0))
          / NULLIF(b.bookings_made - b.cancelled, 0), 2)              AS cost_per_booking_inr,
    round(100.0 * (s.commission_on_stays_inr * 1.18
                   + COALESCE(f.gateway_fee_inr, 0) + COALESCE(f.gst_on_fee_inr, 0))
          / NULLIF(s.revenue_net_inr, 0), 1)                          AS total_cost_pct_of_revenue
FROM booking_side b
JOIN stay_side s ON s.channel_code = b.channel_code
LEFT JOIN fees f ON f.channel_code = b.channel_code
ORDER BY net_revpan_inr DESC;
