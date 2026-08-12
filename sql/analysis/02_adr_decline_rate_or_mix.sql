-- BUSINESS QUESTION
--   "ADR fell. Did we cut prices, or did the mix of business change?"
--
-- WHY IT MATTERS
--   These demand opposite responses. A rate cut is a pricing decision to reverse.
--   A mix shift toward longer-staying, lower-rate corporate business can be
--   healthy -- RevPAR may be flat or up, and cost per occupied unit falls.
--   Treating a mix shift as a pricing failure is one of the most common revenue
--   mistakes.
--
-- METHOD
--   Decompose the ADR change into:
--     within-channel rate effect  = SUM( share_before x (rate_after - rate_before) )
--     between-channel mix effect  = SUM( (share_after - share_before) x rate_before )
--   The two sum to the total ADR change, so the answer reconciles.
--
-- DATE BASIS: stay_date.

WITH params AS (
    SELECT DATE '2026-05-01' AS window_start,
           DATE '2026-06-30' AS window_end
),
tagged AS (
    SELECT
        c.channel_code,
        c.channel_type,
        CASE WHEN e.stay_date BETWEEN p.window_start AND p.window_end
             THEN 'after' ELSE 'before' END                      AS era,
        e.room_revenue_net_inr,
        e.is_occupied
    FROM mart.v_unit_night_enriched e
    JOIN mart.dim_channel c ON c.channel_key = e.channel_key
    CROSS JOIN params p
    WHERE e.is_occupied
      AND e.stay_date BETWEEN p.window_start - INTERVAL '60 days' AND p.window_end
),
by_channel AS (
    SELECT
        channel_code,
        channel_type,
        era,
        count(*)                                                 AS room_nights,
        sum(room_revenue_net_inr) / count(*)                     AS adr
    FROM tagged
    GROUP BY 1, 2, 3
),
shares AS (
    SELECT
        channel_code,
        channel_type,
        era,
        room_nights,
        adr,
        room_nights::numeric / SUM(room_nights) OVER (PARTITION BY era) AS share
    FROM by_channel
),
pivoted AS (
    SELECT
        channel_code,
        channel_type,
        MAX(share)      FILTER (WHERE era = 'before') AS share_before,
        MAX(share)      FILTER (WHERE era = 'after')  AS share_after,
        MAX(adr)        FILTER (WHERE era = 'before') AS adr_before,
        MAX(adr)        FILTER (WHERE era = 'after')  AS adr_after,
        MAX(room_nights) FILTER (WHERE era = 'after') AS nights_after
    FROM shares
    GROUP BY 1, 2
)
SELECT
    channel_code,
    channel_type,
    round(100 * COALESCE(share_before, 0), 2)                        AS share_before_pct,
    round(100 * COALESCE(share_after, 0), 2)                         AS share_after_pct,
    round(100 * (COALESCE(share_after, 0) - COALESCE(share_before, 0)), 2)
                                                                     AS share_change_pp,
    round(adr_before, 2)                                             AS adr_before_inr,
    round(adr_after, 2)                                              AS adr_after_inr,
    -- Rate effect: this channel charged a different price for the same mix.
    round(COALESCE(share_before, 0) * (COALESCE(adr_after, 0) - COALESCE(adr_before, 0)), 2)
                                                                     AS rate_effect_inr,
    -- Mix effect: this channel became a bigger or smaller share of the business.
    round((COALESCE(share_after, 0) - COALESCE(share_before, 0)) * COALESCE(adr_before, 0), 2)
                                                                     AS mix_effect_inr
FROM pivoted
UNION ALL
SELECT
    'TOTAL', '',
    round(100 * SUM(COALESCE(share_before, 0)), 2),
    round(100 * SUM(COALESCE(share_after, 0)), 2),
    round(100 * SUM(COALESCE(share_after, 0) - COALESCE(share_before, 0)), 2),
    NULL, NULL,
    round(SUM(COALESCE(share_before, 0) * (COALESCE(adr_after, 0) - COALESCE(adr_before, 0))), 2),
    round(SUM((COALESCE(share_after, 0) - COALESCE(share_before, 0)) * COALESCE(adr_before, 0)), 2)
FROM pivoted
ORDER BY mix_effect_inr NULLS LAST;
