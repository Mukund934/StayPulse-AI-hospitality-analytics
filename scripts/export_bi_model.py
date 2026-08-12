"""Export a Power BI / Excel / Zoho-ready star schema as CSV.

Power BI Desktop authors a binary .pbix that cannot be generated from a script, so
the honest deliverable is the MODEL: conformed dimensions, a fact table at the
atomic grain, and a pre-aggregated daily KPI table, plus the DAX for every measure.
Connecting the folder and building the report is then a mechanical step.

Row counts are deliberately budgeted. Zoho Analytics' free tier caps at 10,000 rows
ACCOUNT-WIDE and stops loading silently at the ceiling, so a separate slimmed
extract is written for it rather than discovering the cap after upload.

Usage:
    python scripts/export_bi_model.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

import pandas as pd  # noqa: E402

from staypulse import db  # noqa: E402

OUT = PROJECT_ROOT / "powerbi" / "data"
ZOHO = PROJECT_ROOT / "powerbi" / "zoho_extract"

# The full model for Power BI / Excel. Star schema: dims + one atomic fact +
# pre-aggregated marts that the report can use directly.
TABLES: dict[str, str] = {
    "dim_date": """
        SELECT date_key, full_date, year, quarter, month, month_name, year_month,
               day_of_month, day_of_week, day_name, week_of_year, is_weekend,
               is_business_night, same_day_last_year
        FROM mart.dim_date
        WHERE full_date BETWEEN (SELECT min(stay_date) FROM mart.fact_unit_night)
                            AND (SELECT max(stay_date) FROM mart.fact_unit_night)
    """,
    "dim_property": "SELECT * FROM mart.dim_property",
    "dim_unit": "SELECT * FROM mart.dim_unit",
    "dim_channel": "SELECT * FROM mart.dim_channel",
    "dim_request_type": "SELECT * FROM mart.dim_request_type",
    # Guest PII is not exported. Only the analytical attributes are needed, and
    # shipping names, emails and phone numbers into a BI folder is a habit worth
    # not having even when the data is synthetic.
    "dim_guest": """
        SELECT guest_key, guest_id, home_city, guest_segment,
               (phone_last10 IS NOT NULL AND phone_last10 <> '') AS has_phone,
               (email_normalised IS NOT NULL)                    AS has_email
        FROM mart.dim_guest
    """,
    "fact_daily_kpi": """
        SELECT property_key, property_code, stay_date, date_key, year_month,
               day_name, is_weekend, rooms_available, rooms_out_of_order,
               unit_nights_physical, rooms_sold, rooms_sold_microstay,
               room_revenue_net_inr, gst_inr, room_revenue_gross_inr,
               commission_inr, net_after_commission_inr,
               occupancy_pct, occupancy_pct_benchmark, occupancy_ooo_gap_pp,
               adr_inr, adr_excl_microstay_inr, revpar_inr
        FROM mart.v_daily_kpi
    """,
    "fact_booking": """
        SELECT booking_key, booking_id, guest_key, property_key,
               channel_key, booking_date, check_in_date, check_out_date,
               cancel_date, stay_type, status, is_cancelled, nights,
               lead_time_days, net_room_amount_inr, discount_inr, commission_inr,
               nightly_rate_inr, has_business_date_drift
        FROM mart.v_booking_kpi
    """,
    "fact_service_request": """
        SELECT request_key, request_id, property_key, request_type_key, category,
               subcategory, owning_team, request_date, resolved_date,
               created_hour_ist, day_part_ist, priority, status, channel,
               sla_minutes, resolution_minutes, first_response_minutes,
               is_sla_breached, reopened_count, csat_score
        FROM mart.v_service_kpi
    """,
    "fact_review_aspect": """
        SELECT a.aspect_key, a.review_key, a.property_key, a.review_date,
               a.category, a.polarity, a.severity, a.confidence, a.actionable_by,
               a.evidence_span, a.model, r.rating, r.language
        FROM mart.fact_review_aspect a
        JOIN mart.fact_review r ON r.review_key = a.review_key
        WHERE a.evidence_verified
    """,
    "fact_payment": """
        SELECT payment_key, payment_id, booking_key, payment_date, settlement_date,
               method, gross_amount_inr, gateway_fee_inr, gst_on_fee_inr,
               net_credited_inr, status,
               (booking_key IS NULL) AS is_orphan_reference
        FROM mart.fact_payment
    """,
    "dq_rule_results": """
        SELECT r.rule_id, r.dimension, r.target_table, r.severity, r.description,
               res.rows_checked, res.rows_failed, res.failure_pct, res.passed,
               res.checked_at
        FROM meta.dq_result res
        JOIN meta.dq_rule r ON r.rule_id = res.rule_id
        WHERE res.result_id IN (
            SELECT max(result_id) FROM meta.dq_result GROUP BY rule_id
        )
    """,
    "metric_definition": """
        SELECT metric_key, display_name, business_definition, formula_text,
               grain, date_basis, unit, revenue_basis, owner_team, caveats
        FROM meta.metric_definition WHERE is_active
    """,
}

# Zoho free tier: 10,000 rows ACCOUNT-WIDE. Budget before uploading.
ZOHO_TABLES = {
    "kpi_daily_by_property": ("fact_daily_kpi", None),
    "ops_summary": ("fact_service_request", None),
    "dim_property": ("dim_property", None),
    "dim_channel": ("dim_channel", None),
    "dq_rule_results": ("dq_rule_results", None),
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    ZOHO.mkdir(parents=True, exist_ok=True)

    print("=" * 74)
    print("  StayPulse - BI model export (Power BI / Excel / Zoho)")
    print("=" * 74)

    frames: dict[str, pd.DataFrame] = {}
    total = 0
    for name, sql in TABLES.items():
        df = pd.DataFrame(db.fetch_all(sql))
        frames[name] = df
        df.to_csv(OUT / f"{name}.csv", index=False, encoding="utf-8")
        total += len(df)
        print(f"  {name:<24} {len(df):>8,} rows  {len(df.columns):>3} cols")
    print(f"  {'TOTAL':<24} {total:>8,} rows -> powerbi/data/")

    # ---- Zoho: row-budgeted extract ------------------------------------
    print("\n  Zoho free tier extract (10,000-row account-wide cap):")
    budget = 0
    for out_name, (src, _) in ZOHO_TABLES.items():
        df = frames[src].copy()
        if src == "fact_daily_kpi":
            # Monthly x property instead of daily x property: same story, ~1/30 rows.
            df = (df.groupby(["property_code", "year_month"], as_index=False)
                    .agg(rooms_available=("rooms_available", "sum"),
                         rooms_sold=("rooms_sold", "sum"),
                         room_revenue_net_inr=("room_revenue_net_inr", "sum"),
                         rooms_out_of_order=("rooms_out_of_order", "sum")))
            df["occupancy_pct"] = (100.0 * df["rooms_sold"] / df["rooms_available"]).round(2)
            df["adr_inr"] = (df["room_revenue_net_inr"] / df["rooms_sold"]).round(2)
            df["revpar_inr"] = (df["room_revenue_net_inr"] / df["rooms_available"]).round(2)
        elif src == "fact_service_request":
            df = (df.groupby(["property_key", "category", "day_part_ist"], as_index=False)
                    .agg(requests=("request_key", "count"),
                         breaches=("is_sla_breached", "sum"),
                         avg_tat_min=("resolution_minutes", "mean"),
                         avg_csat=("csat_score", "mean")))
            df["avg_tat_min"] = df["avg_tat_min"].round(1)
            df["avg_csat"] = df["avg_csat"].round(2)
            df["breach_pct"] = (100.0 * df["breaches"] / df["requests"]).round(1)
        df.to_csv(ZOHO / f"{out_name}.csv", index=False, encoding="utf-8")
        budget += len(df)
        print(f"    {out_name:<26} {len(df):>6,} rows")
    headroom = 10000 - budget
    print(f"    {'TOTAL':<26} {budget:>6,} rows   headroom {headroom:,}")
    if budget > 10000:
        print("    WARNING: over the free-tier cap. Zoho stops loading silently.")

    # ---- KPI snapshot for the public site ------------------------------
    k = frames["fact_daily_kpi"]
    snap = {
        "occupancy_pct": round(100.0 * k["rooms_sold"].sum() / k["rooms_available"].sum(), 1),
        "adr_inr": round(k["room_revenue_net_inr"].sum() / k["rooms_sold"].sum()),
        "revpar_inr": round(k["room_revenue_net_inr"].sum() / k["rooms_available"].sum()),
        "revenue_inr": round(k["room_revenue_net_inr"].sum()),
        "rooms_sold": int(k["rooms_sold"].sum()),
        "rooms_available": int(k["rooms_available"].sum()),
        "bookings": len(frames["fact_booking"]),
        "service_requests": len(frames["fact_service_request"]),
        "aspects": len(frames["fact_review_aspect"]),
        "properties": len(frames["dim_property"]),
        "units": len(frames["dim_unit"]),
        "metrics": len(frames["metric_definition"]),
        "dq_rules": len(frames["dq_rule_results"]),
        "total_rows_exported": total,
    }
    print("\n  KPI snapshot:")
    for key, val in snap.items():
        print(f"    {key:<22} {val:,}" if isinstance(val, (int, float)) else f"    {key:<22} {val}")

    import json
    (PROJECT_ROOT / "site" / "kpi_snapshot.json").parent.mkdir(parents=True, exist_ok=True)
    (PROJECT_ROOT / "site" / "kpi_snapshot.json").write_text(
        json.dumps(snap, indent=2), encoding="utf-8")
    print("\n  snapshot -> site/kpi_snapshot.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
