-- BUSINESS QUESTION
--   "SLA breaches are up slightly across the portfolio. Where is it actually
--    coming from, and is it worth acting on?"
--
-- WHY THIS SHAPE
--   A blended rate hides everything. The portfolio breach rate can move less than
--   a percentage point while one property in one shift more than doubles its
--   resolution time. Segmenting by property AND day-part is what makes the signal
--   visible; either dimension alone flattens it back out.
--
--   RANK() is used rather than ROW_NUMBER() because ties are meaningful here --
--   two properties genuinely tied on breach rate should share a rank.
--
-- DATE BASIS: request_date (IST business date of the request).

WITH segmented AS (
    SELECT
        property_code,
        owning_team,
        day_part_ist,
        count(*)                                                          AS requests,
        count(*) FILTER (WHERE is_sla_breached)                           AS breaches,
        round(avg(resolution_minutes)::numeric, 1)                        AS avg_tat_min,
        round(avg(sla_minutes)::numeric, 1)                               AS avg_sla_min,
        round(avg(csat_score)::numeric, 2)                                AS avg_csat
    FROM mart.v_service_kpi
    WHERE resolution_minutes IS NOT NULL
    GROUP BY 1, 2, 3
    HAVING count(*) >= 20          -- suppress segments too small to conclude from
),
scored AS (
    SELECT
        *,
        round(100.0 * breaches / requests, 1)                             AS breach_pct,
        -- Portfolio baseline for the same team, so the comparison is like-for-like.
        round(100.0 * SUM(breaches) OVER (PARTITION BY owning_team)
                    / SUM(requests) OVER (PARTITION BY owning_team), 1)   AS team_baseline_pct,
        RANK() OVER (ORDER BY 1.0 * breaches / requests DESC)             AS breach_rank
    FROM segmented
)
SELECT
    breach_rank,
    property_code,
    owning_team,
    day_part_ist,
    requests,
    breaches,
    breach_pct,
    team_baseline_pct,
    round(breach_pct - team_baseline_pct, 1)                              AS vs_baseline_pp,
    avg_tat_min,
    avg_sla_min,
    avg_csat,
    -- An excursion is only worth a page if it is both large and material.
    CASE
        WHEN breach_pct - team_baseline_pct >= 15 AND requests >= 40 THEN 'ACT'
        WHEN breach_pct - team_baseline_pct >= 15                    THEN 'WATCH (small n)'
        ELSE 'within normal range'
    END                                                                   AS verdict
FROM scored
ORDER BY breach_rank
LIMIT 15;
