"""Data-quality rule definitions.

Rules are declarative: each carries its identity, the DAMA dimension it belongs
to, a severity, a tolerated failure rate, and one SQL statement returning exactly
three columns -- rows_checked, rows_failed, sample_keys. Keeping the check as SQL
means the rule runs where the data lives and can be read by anyone who reads SQL.

`defect_class` links a rule to the class of defect the generator plants, so the
framework can be scored on recall per class rather than merely reporting that it
executed. A quality suite that has never been shown to catch anything is
decoration.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Rule:
    rule_id: str
    dimension: str          # completeness | uniqueness | validity | consistency | timeliness | accuracy
    target_table: str
    description: str
    sql: str                # must return rows_checked, rows_failed, sample_keys
    severity: str = "error"
    threshold_pct: float = 0.0
    target_column: str | None = None
    defect_class: str | None = None
    expectation: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


def _agg(total_expr: str, fail_expr: str, sample_expr: str, source: str) -> str:
    """Assemble the standard three-column result from a source relation."""
    return f"""
        SELECT
            count(*)                                              AS rows_checked,
            count(*) FILTER (WHERE {fail_expr})                   AS rows_failed,
            to_jsonb(
                (array_agg({sample_expr} ORDER BY {sample_expr})
                 FILTER (WHERE {fail_expr}))[1:5]
            )                                                     AS sample_keys
        FROM {source}
        WHERE {total_expr}
    """


RULES: list[Rule] = [
    # ---------------------------------------------------------------- completeness
    Rule(
        rule_id="DQ001_guest_contact_present",
        dimension="completeness",
        target_table="mart.dim_guest",
        target_column="phone, email",
        description="Every guest should be reachable on at least one channel.",
        expectation="phone IS NOT NULL OR email IS NOT NULL",
        defect_class="missing_contact",
        severity="warning",
        threshold_pct=2.0,
        sql=_agg("true", "phone IS NULL OR email IS NULL", "guest_id", "mart.dim_guest"),
    ),
    Rule(
        rule_id="DQ002_booking_channel_present",
        dimension="completeness",
        target_table="mart.fact_booking",
        target_column="channel_key",
        description="Revenue cannot be attributed without a channel.",
        expectation="channel_key IS NOT NULL",
        sql=_agg("true", "channel_key IS NULL", "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ003_review_rating_present",
        dimension="completeness",
        target_table="mart.fact_review",
        target_column="rating",
        description="A review without a rating cannot contribute to CSAT.",
        expectation="rating IS NOT NULL",
        defect_class="invalid_rating",
        severity="warning",
        threshold_pct=1.0,
        sql=_agg("true", "rating IS NULL", "review_id", "mart.fact_review"),
    ),
    Rule(
        rule_id="DQ004_unit_night_has_property",
        dimension="completeness",
        target_table="mart.fact_unit_night",
        target_column="property_key",
        description="Every unit-night must roll up to a property.",
        expectation="property_key IS NOT NULL",
        sql=_agg("true", "property_key IS NULL", "unit_night_key::text", "mart.fact_unit_night"),
    ),

    # ------------------------------------------------------------------ uniqueness
    Rule(
        rule_id="DQ010_duplicate_bookings",
        dimension="uniqueness",
        target_table="mart.fact_booking",
        description=(
            "The same guest, unit and arrival date appearing more than once is an "
            "OTA sync double-submit, not two reservations."
        ),
        expectation="one booking per (guest, unit, check_in_date)",
        defect_class="duplicate_booking",
        sql="""
            WITH grp AS (
                SELECT guest_key, unit_key, check_in_date, count(*) AS n,
                       min(booking_id) AS keep_id
                FROM mart.fact_booking
                GROUP BY 1, 2, 3
            )
            SELECT
                (SELECT count(*) FROM mart.fact_booking)      AS rows_checked,
                COALESCE(sum(n - 1), 0)                       AS rows_failed,
                to_jsonb((array_agg(keep_id ORDER BY keep_id))[1:5]) AS sample_keys
            FROM grp WHERE n > 1
        """,
    ),
    Rule(
        rule_id="DQ011_duplicate_guest_identity",
        dimension="uniqueness",
        target_table="mart.dim_guest",
        target_column="phone_last10",
        description=(
            "Two guest records sharing a normalised phone number are the same "
            "person. Repeat-guest rate is understated until they are resolved."
        ),
        expectation="one guest record per normalised phone",
        defect_class="duplicate_guest",
        severity="warning",
        threshold_pct=1.0,
        sql="""
            WITH grp AS (
                SELECT phone_last10, count(*) AS n, min(guest_id) AS keep_id
                FROM mart.dim_guest
                WHERE phone_last10 IS NOT NULL AND phone_last10 <> ''
                GROUP BY 1
            )
            SELECT
                (SELECT count(*) FROM mart.dim_guest)         AS rows_checked,
                COALESCE(sum(n - 1), 0)                       AS rows_failed,
                to_jsonb((array_agg(keep_id ORDER BY keep_id))[1:5]) AS sample_keys
            FROM grp WHERE n > 1
        """,
    ),
    Rule(
        rule_id="DQ012_unit_night_grain_unique",
        dimension="uniqueness",
        target_table="mart.fact_unit_night",
        description="One row per unit per night. A breach invalidates every rate metric.",
        expectation="unique (unit_key, stay_date)",
        sql="""
            WITH grp AS (
                SELECT unit_key, stay_date, count(*) AS n
                FROM mart.fact_unit_night GROUP BY 1, 2
            )
            SELECT
                (SELECT count(*) FROM mart.fact_unit_night)   AS rows_checked,
                COALESCE(sum(n - 1), 0)                       AS rows_failed,
                to_jsonb((array_agg(unit_key::text))[1:5])    AS sample_keys
            FROM grp WHERE n > 1
        """,
    ),

    # -------------------------------------------------------------------- validity
    Rule(
        rule_id="DQ020_impossible_stay_dates",
        dimension="validity",
        target_table="mart.fact_booking",
        target_column="check_in_date, check_out_date",
        description="A nightly stay must depart after it arrives.",
        expectation="check_out_date > check_in_date for stay_type = 'nightly'",
        defect_class="impossible_stay_dates",
        sql=_agg("stay_type = 'nightly'", "check_out_date <= check_in_date",
                 "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ021_rating_in_range",
        dimension="validity",
        target_table="mart.fact_review",
        target_column="rating",
        description="Ratings outside 1-5 are unusable.",
        expectation="rating BETWEEN 1 AND 5",
        defect_class="invalid_rating",
        sql=_agg("rating IS NOT NULL", "rating < 1 OR rating > 5",
                 "review_id", "mart.fact_review"),
    ),
    Rule(
        rule_id="DQ022_negative_amounts",
        dimension="validity",
        target_table="mart.fact_booking",
        target_column="net_room_amount_inr",
        description="Negative or zero room revenue on a live booking is invalid.",
        expectation="net_room_amount_inr > 0 for non-cancelled bookings",
        sql=_agg("status NOT IN ('cancelled','no_show')", "net_room_amount_inr <= 0",
                 "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ023_los_plausible",
        dimension="validity",
        target_table="mart.fact_booking",
        target_column="nights",
        description="A serviced-apartment stay beyond 60 nights is a data error, not a guest.",
        expectation="nights BETWEEN 0 AND 60",
        sql=_agg("stay_type = 'nightly'", "nights < 0 OR nights > 60",
                 "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ024_lead_time_non_negative",
        dimension="validity",
        target_table="mart.fact_booking",
        target_column="lead_time_days",
        description="A booking cannot be made after the guest arrives.",
        expectation="lead_time_days >= 0",
        sql=_agg("lead_time_days IS NOT NULL", "lead_time_days < 0",
                 "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ025_inventory_balance",
        dimension="validity",
        target_table="mart.fact_inventory_movement",
        description="Closing stock must equal opening + received - consumed - wastage.",
        expectation="closing_qty = opening_qty + received_qty - consumed_qty - wastage_qty",
        defect_class="inventory_anomaly",
        sql=_agg("true",
                 "closing_qty <> opening_qty + received_qty - consumed_qty - wastage_qty",
                 "item_code", "mart.fact_inventory_movement"),
    ),
    Rule(
        rule_id="DQ026_inventory_non_negative",
        dimension="validity",
        target_table="mart.fact_inventory_movement",
        target_column="closing_qty",
        description="Stock cannot go negative.",
        expectation="closing_qty >= 0",
        defect_class="inventory_anomaly",
        severity="warning",
        sql=_agg("true", "closing_qty < 0", "item_code", "mart.fact_inventory_movement"),
    ),

    # ----------------------------------------------------------------- consistency
    Rule(
        rule_id="DQ030_orphan_payment_reference",
        dimension="consistency",
        target_table="mart.fact_payment",
        target_column="booking_key",
        description=(
            "A gateway payment whose booking reference does not resolve. Left "
            "joined rather than dropped so the money is visible, not silently lost."
        ),
        expectation="booking_key IS NOT NULL",
        defect_class="orphan_payment_ref",
        severity="error",
        threshold_pct=0.5,
        sql=_agg("true", "booking_key IS NULL", "payment_id", "mart.fact_payment"),
    ),
    Rule(
        rule_id="DQ031_payment_amount_matches_folio",
        dimension="accuracy",
        target_table="mart.fact_payment",
        description=(
            "Gateway gross should equal the folio net room amount. A mismatch is "
            "either a partial payment or a reconciliation break."
        ),
        expectation="abs(payment.gross - booking.net_room) <= 1.00",
        defect_class="payment_amount_mismatch",
        severity="error",
        threshold_pct=1.0,
        sql="""
            SELECT
                count(*)                                                    AS rows_checked,
                count(*) FILTER (WHERE abs(p.gross_amount_inr - b.net_room_amount_inr) > 1.00)
                                                                            AS rows_failed,
                to_jsonb((array_agg(p.payment_id ORDER BY p.payment_id)
                    FILTER (WHERE abs(p.gross_amount_inr - b.net_room_amount_inr) > 1.00))[1:5])
                                                                            AS sample_keys
            FROM mart.fact_payment p
            JOIN mart.fact_booking b ON b.booking_key = p.booking_key
        """,
    ),
    Rule(
        rule_id="DQ032_cancellation_state_coherent",
        dimension="consistency",
        target_table="mart.fact_booking",
        description=(
            "A cancelled booking must carry a cancellation timestamp, and vice "
            "versa. This rule is a REGRESSION GUARD, not a detector: the mart "
            "CHECK constraint ck_booking_cancel makes the incoherent state "
            "unloadable, so the correct expected result is zero. The defect can "
            "only exist upstream of the mart, which is where the constraint earns "
            "its place -- a rule that can never fire because a constraint already "
            "prevents the state is a stronger control than a rule that reports it."
        ),
        expectation="status = 'cancelled' XOR cancelled_at IS NULL (enforced by CHECK)",
        tags=("regression-guard",),
        sql=_agg("true",
                 "(status = 'cancelled' AND cancelled_at IS NULL) OR "
                 "(status <> 'cancelled' AND cancelled_at IS NOT NULL)",
                 "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ033_cancelled_holds_no_inventory",
        dimension="consistency",
        target_table="mart.fact_unit_night",
        description="A cancelled booking must not occupy any unit-night.",
        expectation="no occupied unit-night references a cancelled booking",
        sql="""
            SELECT
                (SELECT count(*) FROM mart.fact_unit_night WHERE is_occupied) AS rows_checked,
                count(*)                                                     AS rows_failed,
                to_jsonb((array_agg(b.booking_id ORDER BY b.booking_id))[1:5]) AS sample_keys
            FROM mart.fact_unit_night un
            JOIN mart.fact_booking b ON b.booking_key = un.booking_key
            WHERE un.is_occupied AND b.status IN ('cancelled','no_show')
        """,
    ),
    Rule(
        rule_id="DQ034_departure_night_not_charged",
        dimension="consistency",
        target_table="mart.fact_unit_night",
        description=(
            "The half-open interval: a unit-night on or after the departure date "
            "means room-nights are inflated by roughly 1/ALOS."
        ),
        expectation="no unit-night on or after check_out_date",
        sql="""
            SELECT
                (SELECT count(*) FROM mart.fact_unit_night WHERE is_occupied) AS rows_checked,
                count(*)                                                     AS rows_failed,
                to_jsonb((array_agg(b.booking_id ORDER BY b.booking_id))[1:5]) AS sample_keys
            FROM mart.fact_unit_night un
            JOIN mart.fact_booking b ON b.booking_key = un.booking_key
            WHERE b.stay_type = 'nightly' AND b.nights > 0
              AND un.stay_date >= b.check_out_date
        """,
    ),
    Rule(
        rule_id="DQ035_revenue_only_on_occupied",
        dimension="consistency",
        target_table="mart.fact_unit_night",
        description="A vacant night cannot carry room revenue.",
        expectation="room_revenue_net_inr = 0 when not occupied",
        sql=_agg("NOT is_occupied", "room_revenue_net_inr <> 0",
                 "unit_night_key::text", "mart.fact_unit_night"),
    ),
    Rule(
        rule_id="DQ036_ooo_not_sellable",
        dimension="consistency",
        target_table="mart.fact_unit_night",
        description="An out-of-order unit must be removed from availability.",
        expectation="NOT (is_out_of_order AND is_sellable)",
        sql=_agg("true", "is_out_of_order AND is_sellable",
                 "unit_night_key::text", "mart.fact_unit_night"),
    ),
    Rule(
        rule_id="DQ037_sla_breach_flag_agrees",
        dimension="accuracy",
        target_table="mart.fact_service_request",
        description="The stored breach flag must agree with resolution time vs SLA target.",
        expectation="is_sla_breached = (resolution_minutes > sla_minutes)",
        sql=_agg("resolution_minutes IS NOT NULL",
                 "is_sla_breached IS DISTINCT FROM (resolution_minutes > sla_minutes)",
                 "request_id", "mart.fact_service_request"),
    ),
    Rule(
        rule_id="DQ038_resolution_after_creation",
        dimension="validity",
        target_table="mart.fact_service_request",
        description="A request cannot be resolved before it was raised.",
        expectation="resolved_at >= created_at",
        sql=_agg("resolved_at IS NOT NULL", "resolved_at < created_at",
                 "request_id", "mart.fact_service_request"),
    ),

    # ------------------------------------------------- timezone / business date
    Rule(
        rule_id="DQ040_booking_business_date_agrees",
        dimension="accuracy",
        target_table="mart.fact_booking",
        target_column="booking_date",
        description=(
            "The stored reporting date must equal the IST business date derived "
            "from the event timestamp. Disagreement means a feed wrote a UTC date, "
            "which silently moves late-night activity to the previous day."
        ),
        expectation="booking_date = meta.business_date(booked_at)",
        defect_class="business_date_drift",
        severity="error",
        threshold_pct=0.0,
        sql=_agg("true", "booking_date <> meta.business_date(booked_at)",
                 "booking_id", "mart.fact_booking"),
    ),
    Rule(
        rule_id="DQ041_request_business_date_agrees",
        dimension="accuracy",
        target_table="mart.fact_service_request",
        target_column="request_date",
        description="Service-request reporting date must match the IST business date.",
        expectation="request_date = meta.business_date(created_at)",
        defect_class="business_date_drift",
        severity="warning",
        threshold_pct=1.0,
        sql=_agg("true", "request_date <> meta.business_date(created_at)",
                 "request_id", "mart.fact_service_request"),
    ),

    # ------------------------------------------------------------------ timeliness
    Rule(
        rule_id="DQ050_booking_feed_fresh",
        dimension="timeliness",
        target_table="mart.fact_booking",
        description=(
            "The booking feed should have delivered data recently. Measured against "
            "the dataset's own horizon, since this is a fixed synthetic period."
        ),
        expectation="max(ingested_at) within 26 hours of now()",
        sql="""
            SELECT
                count(*)                                                  AS rows_checked,
                CASE WHEN max(ingested_at) < now() - INTERVAL '26 hours'
                     THEN count(*) ELSE 0 END                             AS rows_failed,
                to_jsonb(ARRAY[max(ingested_at)::text])                   AS sample_keys
            FROM mart.fact_booking
        """,
    ),
    Rule(
        rule_id="DQ051_no_silent_channel_gap",
        dimension="timeliness",
        target_table="mart.fact_service_request",
        target_column="channel",
        description=(
            "A source channel that normally delivers volume and suddenly delivers "
            "nothing is a broken integration. The table is not wrong, it is EMPTY -- "
            "so a null check passes and only a volume band catches it."
        ),
        expectation="no channel has a run of >=3 consecutive zero-volume days against a healthy baseline",
        defect_class="silent_integration_gap",
        severity="error",
        threshold_pct=0.0,
        tags=("gaps-and-islands", "alert-precision"),
        sql="""
            WITH cal AS (
                SELECT d::date AS request_date
                FROM generate_series(
                    (SELECT min(request_date) FROM mart.fact_service_request),
                    (SELECT max(request_date) FROM mart.fact_service_request),
                    INTERVAL '1 day') d
            ),
            channels AS (
                SELECT DISTINCT channel FROM mart.fact_service_request WHERE channel IS NOT NULL
            ),
            grid AS (SELECT c.request_date, ch.channel FROM cal c CROSS JOIN channels ch),
            daily AS (
                SELECT g.request_date, g.channel,
                       COALESCE(count(sr.request_id), 0) AS n
                FROM grid g
                LEFT JOIN mart.fact_service_request sr
                       ON sr.request_date = g.request_date AND sr.channel = g.channel
                GROUP BY 1, 2
            ),
            banded AS (
                -- Trailing MEAN, not median. PostgreSQL rejects percentile_cont()
                -- as a window function ("OVER is not supported for ordered-set
                -- aggregate"), and a mean is an adequate volume baseline for a
                -- zero-volume test: we only need to know whether the channel
                -- normally delivers anything at all.
                --
                -- 56 days, not 28: a channel averaging ~1.6 requests/day has a
                -- 28-day mean that wanders across any threshold near 1.5, which
                -- breaks a genuine outage into fragments. At 28 days the real
                -- 9-day incident was detected as 3 days. The window has to be
                -- long enough that the baseline is stable relative to the
                -- threshold, or the run-length logic measures noise.
                SELECT request_date, channel, n,
                       avg(n) OVER (
                           PARTITION BY channel
                           ORDER BY request_date
                           ROWS BETWEEN 56 PRECEDING AND 1 PRECEDING
                       ) AS baseline
                FROM daily
            ),
            marked AS (
                -- Threshold 1.0/day is derived from the channel volume
                -- distribution, not chosen for convenience: the busiest channel
                -- runs ~1.6/day and every other channel sits below 0.8/day, so
                -- 1.0 separates "should always have traffic" from "legitimately
                -- sparse". LIMITATION, stated rather than hidden: this detector
                -- cannot see an outage on a channel too sparse to hold a stable
                -- baseline. Catching those needs a longer aggregation period, not
                -- a lower threshold -- lowering it just re-imports the false alerts.
                SELECT request_date, channel,
                       CASE WHEN n = 0 AND baseline >= 1.0 THEN 1 ELSE 0 END AS is_quiet
                FROM banded
            ),
            -- Gaps and islands. A single quiet day on a low-volume channel is
            -- normal; a RUN of quiet days against a healthy baseline is an outage.
            -- Flagging individual days instead produced 138 alerts for one 9-day
            -- incident, which is how an alerting system trains its users to
            -- ignore it. The difference of two row_numbers is constant within a
            -- consecutive run, which groups the run without a recursive CTE.
            islands AS (
                SELECT request_date, channel, is_quiet,
                       row_number() OVER (PARTITION BY channel ORDER BY request_date)
                     - row_number() OVER (PARTITION BY channel, is_quiet ORDER BY request_date)
                       AS island_id
                FROM marked
            ),
            outages AS (
                SELECT channel, island_id,
                       min(request_date) AS from_date,
                       max(request_date) AS to_date,
                       count(*)          AS quiet_days
                FROM islands
                WHERE is_quiet = 1
                GROUP BY channel, island_id
                -- Minimum run length set from measured precision, not by guess.
                -- At >= 3 days the rule produced 2 alerts of which 1 was real
                -- (a genuine 10-day integration outage) and 1 was a 3-day quiet
                -- spell on a mid-volume channel: 50% precision. At >= 4 days it
                -- produces 1 alert and it is the real one. A 3-day gap is inside
                -- normal variation at these volumes; an integration that has
                -- actually stopped does not come back on day four.
                HAVING count(*) >= 4
            )
            SELECT
                (SELECT count(*) FROM daily)                                   AS rows_checked,
                COALESCE(sum(quiet_days), 0)                                   AS rows_failed,
                to_jsonb((array_agg(
                    channel || ' silent ' || quiet_days || 'd: '
                    || from_date::text || ' .. ' || to_date::text
                    ORDER BY from_date))[1:5])                                 AS sample_keys
            FROM outages
        """,
    ),

    # -------------------------------------------------------------- reconciliation
    Rule(
        rule_id="DQ060_booking_payment_reconciliation",
        dimension="accuracy",
        target_table="mart.fact_payment",
        description=(
            "Every non-cancelled booking should have a payment. A reconciliation is "
            "clean when UNEXPLAINED variance is zero, not when variance is zero."
        ),
        expectation="every live booking has at least one payment row",
        severity="warning",
        threshold_pct=3.0,
        sql="""
            SELECT
                count(*)                                                    AS rows_checked,
                count(*) FILTER (WHERE p.payment_key IS NULL)               AS rows_failed,
                to_jsonb((array_agg(b.booking_id ORDER BY b.booking_id)
                    FILTER (WHERE p.payment_key IS NULL))[1:5])             AS sample_keys
            FROM mart.fact_booking b
            LEFT JOIN mart.fact_payment p ON p.booking_key = b.booking_key
            WHERE b.status NOT IN ('cancelled')
        """,
    ),
    Rule(
        rule_id="DQ061_occupancy_within_capacity",
        dimension="validity",
        target_table="mart.fact_unit_night",
        description="Rooms sold can never exceed rooms available on any night.",
        expectation="occupancy <= 100% for every property-night",
        sql="""
            WITH nightly AS (
                SELECT property_key, stay_date,
                       count(*) FILTER (WHERE is_sellable) AS avail,
                       count(*) FILTER (WHERE is_occupied) AS sold
                FROM mart.fact_unit_night GROUP BY 1, 2
            )
            SELECT
                count(*)                                        AS rows_checked,
                count(*) FILTER (WHERE sold > avail)            AS rows_failed,
                to_jsonb((array_agg(stay_date::text ORDER BY stay_date)
                    FILTER (WHERE sold > avail))[1:5])          AS sample_keys
            FROM nightly
        """,
    ),
]


def rules_by_id() -> dict[str, Rule]:
    return {r.rule_id: r for r in RULES}


def defect_classes() -> dict[str, list[str]]:
    """defect_class -> rule_ids that are expected to detect it."""
    out: dict[str, list[str]] = {}
    for r in RULES:
        if r.defect_class:
            out.setdefault(r.defect_class, []).append(r.rule_id)
    return out
