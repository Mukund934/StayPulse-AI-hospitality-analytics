-- BUSINESS QUESTION
--   "The PMS says one number, the gateway says another, the bank says a third.
--    Which is right?"
--
-- THE THESIS
--   All three are right. A reconciliation is clean when UNEXPLAINED variance is
--   zero, NOT when variance is zero. Every rupee of difference must be assigned a
--   typed reason; whatever cannot be typed is the finding.
--
--   Worked example of the expected chain on a INR 1,000 sale:
--     folio                      1,000.00
--     less gateway MDR @ 2%        -20.00
--     less GST on MDR @ 18%         -3.60
--     bank credits                 976.40   at T+n
--   The PMS says 1,000, the bank says 976.40, and neither is wrong.
--
-- DATE BASIS: payment_date (when money moved), NOT stay_date.

WITH typed AS (
    SELECT
        p.payment_id,
        p.payment_date,
        p.method,
        p.booking_key,
        p.booking_id_raw,
        p.gross_amount_inr,
        p.gateway_fee_inr,
        p.gst_on_fee_inr,
        p.net_credited_inr,
        b.net_room_amount_inr                                        AS folio_amount_inr,
        b.booking_id                                                 AS resolved_booking_id,
        CASE
            WHEN p.booking_key IS NULL
                THEN 'UNRESOLVED_REFERENCE'
            WHEN abs(p.gross_amount_inr - b.net_room_amount_inr) > 1.00
                THEN 'AMOUNT_MISMATCH'
            WHEN p.settlement_date IS NULL
                THEN 'TIMING_UNSETTLED'
            ELSE 'MATCHED'
        END                                                          AS variance_code,
        CASE WHEN p.booking_key IS NOT NULL
             THEN round(p.gross_amount_inr - b.net_room_amount_inr, 2) END
                                                                     AS unexplained_inr
    FROM mart.fact_payment p
    LEFT JOIN mart.fact_booking b ON b.booking_key = p.booking_key
)
SELECT
    variance_code,
    count(*)                                                         AS payments,
    round(100.0 * count(*) / SUM(count(*)) OVER (), 2)               AS pct_of_payments,
    round(sum(gross_amount_inr))                                     AS gateway_gross_inr,
    round(sum(folio_amount_inr))                                     AS folio_gross_inr,
    round(sum(gateway_fee_inr))                                      AS mdr_inr,
    round(sum(gst_on_fee_inr))                                       AS gst_on_mdr_inr,
    round(sum(net_credited_inr))                                     AS bank_credited_inr,
    -- The only number that should ever be zero.
    round(COALESCE(sum(unexplained_inr), 0))                         AS unexplained_variance_inr,
    CASE variance_code
        WHEN 'MATCHED'              THEN 'explained: MDR + GST on MDR + settlement timing'
        WHEN 'AMOUNT_MISMATCH'      THEN 'NOT explained -- investigate: partial payment or folio adjustment'
        WHEN 'UNRESOLVED_REFERENCE' THEN 'NOT explained -- money received against a booking id that does not exist'
        WHEN 'TIMING_UNSETTLED'     THEN 'explained: in flight, not yet credited'
    END                                                              AS interpretation
FROM typed
GROUP BY variance_code
ORDER BY unexplained_variance_inr DESC NULLS LAST;
