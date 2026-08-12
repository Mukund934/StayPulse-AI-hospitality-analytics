-- BUSINESS QUESTION
--   "Which guests come back, how long do they take, and which channel acquires
--    the ones worth having?"
--
-- WHY THIS SHAPE
--   Repeat rate as a single number is nearly useless for a decision. What an
--   operator can act on is: which acquisition channel produces guests who return,
--   and how long the gap is (which sets the re-marketing window).
--
--   Identity is resolved deterministically -- normalised phone, then normalised
--   email -- BEFORE cohorting. Without that step duplicate profiles split one
--   guest into several and the repeat rate is understated.
--
-- DATE BASIS: stay_date (first completed stay defines the cohort).

WITH resolved AS (
    SELECT
        COALESCE(NULLIF(g.phone_last10, ''), g.email_normalised, g.guest_key::text)
                                                                     AS identity_key,
        b.booking_key,
        b.check_in_date,
        b.net_room_amount_inr,
        b.nights,
        c.channel_code,
        c.channel_type,
        g.guest_segment
    FROM mart.fact_booking b
    JOIN mart.dim_guest   g ON g.guest_key   = b.guest_key
    JOIN mart.dim_channel c ON c.channel_key = b.channel_key
    WHERE b.status IN ('checked_out', 'checked_in')
),
sequenced AS (
    SELECT
        *,
        ROW_NUMBER() OVER (PARTITION BY identity_key ORDER BY check_in_date, booking_key)
                                                                     AS stay_seq,
        COUNT(*)    OVER (PARTITION BY identity_key)                 AS total_stays,
        -- Days until the NEXT stay. LAG/LEAD must be partitioned by identity or it
        -- silently reads the previous guest's date.
        LEAD(check_in_date) OVER (PARTITION BY identity_key ORDER BY check_in_date)
            - check_in_date                                          AS days_to_next_stay,
        SUM(net_room_amount_inr) OVER (PARTITION BY identity_key)    AS lifetime_revenue_inr
    FROM resolved
),
first_stay AS (
    -- The acquiring channel is the channel of the FIRST stay, not the latest.
    -- Attributing loyalty to the most recent channel credits the wrong one.
    SELECT
        identity_key,
        channel_code                                                 AS acquiring_channel,
        channel_type                                                 AS acquiring_channel_type,
        guest_segment,
        total_stays,
        lifetime_revenue_inr,
        check_in_date                                                AS first_stay_date
    FROM sequenced
    WHERE stay_seq = 1
),
gaps AS (
    SELECT identity_key, round(avg(days_to_next_stay)::numeric, 0) AS avg_gap_days
    FROM sequenced
    WHERE days_to_next_stay IS NOT NULL
    GROUP BY 1
)
SELECT
    f.acquiring_channel,
    f.acquiring_channel_type,
    count(*)                                                         AS guests_acquired,
    count(*) FILTER (WHERE f.total_stays >= 2)                       AS returned,
    round(100.0 * count(*) FILTER (WHERE f.total_stays >= 2) / count(*), 1)
                                                                     AS repeat_rate_pct,
    round(avg(f.total_stays)::numeric, 2)                            AS avg_stays_per_guest,
    round(avg(g.avg_gap_days)::numeric, 0)                           AS avg_days_between_stays,
    round(avg(f.lifetime_revenue_inr)::numeric, 0)                   AS avg_lifetime_revenue_inr,
    -- Value quartile of this channel's guests against the whole book.
    NTILE(4) OVER (ORDER BY avg(f.lifetime_revenue_inr))             AS lifetime_value_quartile
FROM first_stay f
LEFT JOIN gaps g ON g.identity_key = f.identity_key
GROUP BY 1, 2
HAVING count(*) >= 30
ORDER BY repeat_rate_pct DESC;
