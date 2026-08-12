-- BUSINESS QUESTION
--   "Revenue moved month over month. Was it rate, volume, or mix?"
--
-- WHY THIS SHAPE
--   RevPAR = ADR x Occupancy is multiplicative, so a naive two-factor split
--   leaves an unallocated joint term (dADR x dOcc) and the decomposition does not
--   reconcile. Analysts then hand-wave the gap.
--
--   This uses a symmetric (Shapley) allocation, splitting the interaction term
--   evenly between the two factors:
--       rate_effect   = dADR x (Occ0 + Occ1) / 2
--       volume_effect = dOcc x (ADR0 + ADR1) / 2
--   which sums EXACTLY to dRevPAR with zero residual. The residual column is
--   returned so the reconciliation can be seen rather than trusted.
--
-- DATE BASIS: stay_date (revenue as earned by Operations).

WITH monthly AS (
    SELECT
        d.year_month,
        min(k.stay_date)                                            AS month_start,
        sum(k.rooms_available)                                      AS rooms_available,
        sum(k.rooms_sold)                                           AS rooms_sold,
        sum(k.room_revenue_net_inr)                                 AS revenue_net,
        sum(k.rooms_sold)::numeric / NULLIF(sum(k.rooms_available), 0)   AS occupancy,
        sum(k.room_revenue_net_inr) / NULLIF(sum(k.rooms_sold), 0)      AS adr,
        sum(k.room_revenue_net_inr) / NULLIF(sum(k.rooms_available), 0) AS revpar
    FROM mart.v_daily_kpi k
    JOIN mart.dim_date d ON d.full_date = k.stay_date
    GROUP BY d.year_month
),
paired AS (
    SELECT
        year_month,
        month_start,
        rooms_available,
        rooms_sold,
        revenue_net,
        occupancy,
        adr,
        revpar,
        LAG(occupancy) OVER w AS prev_occupancy,
        LAG(adr)       OVER w AS prev_adr,
        LAG(revpar)    OVER w AS prev_revpar,
        LAG(revenue_net) OVER w AS prev_revenue
    FROM monthly
    WINDOW w AS (ORDER BY month_start)
)
SELECT
    year_month,
    round(revenue_net)                                              AS revenue_net_inr,
    round(100 * occupancy, 2)                                       AS occupancy_pct,
    round(adr, 2)                                                   AS adr_inr,
    round(revpar, 2)                                                AS revpar_inr,
    round(revpar - prev_revpar, 2)                                  AS revpar_change_inr,
    -- Shapley split of the joint term: both effects sum to the total change.
    round((adr - prev_adr) * (occupancy + prev_occupancy) / 2, 2)    AS rate_effect_inr,
    round((occupancy - prev_occupancy) * (adr + prev_adr) / 2, 2)    AS volume_effect_inr,
    -- Must be 0.00 (to rounding). If it is not, the decomposition is wrong.
    round((revpar - prev_revpar)
          - ((adr - prev_adr) * (occupancy + prev_occupancy) / 2)
          - ((occupancy - prev_occupancy) * (adr + prev_adr) / 2), 2) AS residual_inr,
    CASE
        WHEN prev_revpar IS NULL THEN 'baseline month'
        WHEN abs((adr - prev_adr) * (occupancy + prev_occupancy) / 2)
           > abs((occupancy - prev_occupancy) * (adr + prev_adr) / 2)
            THEN 'rate-driven'
        ELSE 'volume-driven'
    END                                                             AS primary_driver
FROM paired
ORDER BY month_start;
