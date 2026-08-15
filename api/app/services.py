"""Analytics services — the API's only route to data.

Every function here reads the **semantic layer** (`mart.v_*` views and
`meta.metric_definition`), never raw fact tables and never its own re-derived
arithmetic. That is the point: a route handler that computes occupancy itself
becomes a second definition of occupancy, and the whole project exists to prevent
exactly that.

All queries are aggregate. Nothing here returns thousands of raw rows to a browser,
and nothing calls an LLM on a page request — AI results are read from storage where
the batch pipeline already validated them.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from staypulse import db
from staypulse.analytics import forecast as _fc
from staypulse.analytics import revenue as _rv
from staypulse.analytics import rootcause as _rc

# Supabase free tier sleeps and the pooler recycles connections, so keep result
# sets small and queries cheap. These caps are deliberate, not arbitrary.
MAX_ROWS = 500


def _period() -> dict[str, Any]:
    r = db.fetch_all("""
        SELECT min(stay_date) AS first_date, max(stay_date) AS last_date,
               count(DISTINCT stay_date) AS days
        FROM mart.v_daily_kpi
    """)[0]
    return {"from": r["first_date"], "to": r["last_date"], "days": int(r["days"])}


# ---------------------------------------------------------------------------
def kpi_overview(days: int | None = None) -> dict[str, Any]:
    """Portfolio KPIs with a like-for-like comparison window.

    When `days` is given, the comparison is the immediately preceding window of the
    same length — not "last month", which would compare a 30-day period against a
    31-day one and manufacture a difference.
    """
    if days:
        cur = db.fetch_all("""
            SELECT sum(rooms_available) av, sum(rooms_sold) sold,
                   sum(room_revenue_net_inr) rev, sum(rooms_out_of_order) ooo
            FROM mart.v_daily_kpi
            WHERE stay_date > (SELECT max(stay_date) - :d FROM mart.v_daily_kpi)
        """, d=days)[0]
        prev = db.fetch_all("""
            SELECT sum(rooms_available) av, sum(rooms_sold) sold,
                   sum(room_revenue_net_inr) rev
            FROM mart.v_daily_kpi
            WHERE stay_date > (SELECT max(stay_date) - :d2 FROM mart.v_daily_kpi)
              AND stay_date <= (SELECT max(stay_date) - :d FROM mart.v_daily_kpi)
        """, d=days, d2=days * 2)[0]
    else:
        cur = db.fetch_all("""
            SELECT sum(rooms_available) av, sum(rooms_sold) sold,
                   sum(room_revenue_net_inr) rev, sum(rooms_out_of_order) ooo
            FROM mart.v_daily_kpi
        """)[0]
        prev = {"av": None, "sold": None, "rev": None}

    bk = db.fetch_all("""
        SELECT count(*) n, count(*) FILTER (WHERE is_cancelled) cancelled,
               round(avg(lead_time_days)::numeric, 1) lead_days
        FROM mart.v_booking_kpi
    """)[0]

    av, sold = int(cur["av"] or 0), int(cur["sold"] or 0)
    rev = float(cur["rev"] or 0)
    occ = 100.0 * sold / av if av else 0.0
    adr = rev / sold if sold else 0.0
    revpar = rev / av if av else 0.0

    comparison = None
    if prev.get("av"):
        pav, psold = int(prev["av"]), int(prev["sold"])
        prev_rev = float(prev["rev"] or 0)
        pocc = 100.0 * psold / pav if pav else 0.0
        padr = prev_rev / psold if psold else 0.0
        comparison = {
            "revenue_inr": round(prev_rev, 2),
            "occupancy_pct": round(pocc, 2),
            "adr_inr": round(padr, 2),
            "revpar_inr": round(prev_rev / pav, 2) if pav else 0.0,
            "revenue_delta_pct": round(100.0 * (rev - prev_rev) / prev_rev, 2) if prev_rev else None,
            "occupancy_delta_pp": round(occ - pocc, 2),
            "adr_delta_inr": round(adr - padr, 2),
        }

    return {
        "period": _period() if not days else {"trailing_days": days},
        "revenue_inr": round(rev, 2),
        "occupancy_pct": round(occ, 2),
        "adr_inr": round(adr, 2),
        "revpar_inr": round(revpar, 2),
        "rooms_sold": sold,
        "rooms_available": av,
        "rooms_out_of_order": int(cur.get("ooo") or 0),
        "bookings": int(bk["n"]),
        "cancellation_rate_pct": round(100.0 * int(bk["cancelled"]) / int(bk["n"]), 2)
                                 if int(bk["n"]) else 0.0,
        "avg_lead_time_days": float(bk["lead_days"] or 0),
        "comparison": comparison,
        "date_basis": "stay_date",
        "is_synthetic": True,
    }


def revenue_trend(grain: str = "month") -> list[dict]:
    col = "d.year_month" if grain == "month" else "k.stay_date::text"
    return db.fetch_all(f"""
        SELECT {col} AS period,
               round(sum(k.room_revenue_net_inr), 2)                            AS revenue_inr,
               round(100.0*sum(k.rooms_sold)/NULLIF(sum(k.rooms_available),0),2) AS occupancy_pct,
               round(sum(k.room_revenue_net_inr)/NULLIF(sum(k.rooms_sold),0),2)  AS adr_inr,
               round(sum(k.room_revenue_net_inr)/NULLIF(sum(k.rooms_available),0),2) AS revpar_inr,
               sum(k.rooms_sold)                                                 AS rooms_sold
        FROM mart.v_daily_kpi k
        JOIN mart.dim_date d ON d.full_date = k.stay_date
        GROUP BY 1 ORDER BY 1
        LIMIT {MAX_ROWS}
    """)


def revenue_channels() -> list[dict]:
    """Channel economics net of commission AND the 18% GST charged on commission."""
    return db.fetch_all("""
        SELECT c.channel_code, c.channel_name, c.channel_type,
               count(*)                                                   AS room_nights,
               round(100.0*count(*)/SUM(count(*)) OVER (), 2)             AS room_night_share_pct,
               round(sum(e.room_revenue_net_inr), 2)                      AS revenue_net_inr,
               round(sum(e.room_revenue_net_inr)/count(*), 2)             AS adr_inr,
               round(sum(e.commission_inr), 2)                            AS commission_inr,
               round(sum(e.commission_inr)*0.18, 2)                       AS gst_on_commission_inr,
               round((sum(e.room_revenue_net_inr) - sum(e.commission_inr)*1.18)
                     / count(*), 2)                                        AS net_per_room_night_inr
        FROM mart.v_unit_night_enriched e
        JOIN mart.dim_channel c ON c.channel_key = e.channel_key
        WHERE e.is_occupied
        GROUP BY 1,2,3 ORDER BY net_per_room_night_inr DESC
    """)


def properties() -> list[dict]:
    return db.fetch_all("""
        SELECT p.property_key, p.property_code, p.property_name, p.area, p.city,
               p.unit_count, p.opened_on, p.is_active,
               count(u.unit_key) AS units_modelled
        FROM mart.dim_property p
        LEFT JOIN mart.dim_unit u ON u.property_key = p.property_key
        GROUP BY 1,2,3,4,5,6,7,8 ORDER BY p.property_code
    """)


def property_performance(property_key: int) -> dict[str, Any] | None:
    rows = db.fetch_all("""
        SELECT p.property_key, p.property_code, p.property_name,
               sum(k.rooms_available) av, sum(k.rooms_sold) sold,
               sum(k.rooms_out_of_order) ooo,
               sum(k.room_revenue_net_inr) rev
        FROM mart.v_daily_kpi k
        JOIN mart.dim_property p ON p.property_key = k.property_key
        WHERE k.property_key = :pk
        GROUP BY 1,2,3
    """, pk=property_key)
    if not rows:
        return None
    r = rows[0]
    av, sold, rev = int(r["av"]), int(r["sold"]), float(r["rev"])
    ops = db.fetch_all("""
        SELECT count(*) requests,
               count(*) FILTER (WHERE is_sla_breached) breaches,
               round(avg(resolution_minutes)::numeric,1) avg_tat_min,
               round(avg(csat_score)::numeric,2) csat
        FROM mart.v_service_kpi
        WHERE property_key = :pk AND resolution_minutes IS NOT NULL
    """, pk=property_key)[0]
    return {
        "property_key": int(r["property_key"]),
        "property_code": r["property_code"],
        "property_name": r["property_name"],
        "rooms_available": av,
        "rooms_sold": sold,
        "rooms_out_of_order": int(r["ooo"]),
        "revenue_inr": round(rev, 2),
        "occupancy_pct": round(100.0 * sold / av, 2) if av else 0.0,
        "adr_inr": round(rev / sold, 2) if sold else 0.0,
        "revpar_inr": round(rev / av, 2) if av else 0.0,
        "service_requests": int(ops["requests"]),
        "sla_breaches": int(ops["breaches"]),
        "sla_breach_rate_pct": round(100.0 * int(ops["breaches"]) / int(ops["requests"]), 2)
                               if int(ops["requests"]) else 0.0,
        "avg_resolution_minutes": float(ops["avg_tat_min"] or 0),
        "csat": float(ops["csat"] or 0),
    }


# ---------------------------------------------------------------------------
def operations_overview() -> dict[str, Any]:
    tot = db.fetch_all("""
        SELECT count(*) requests,
               count(*) FILTER (WHERE is_sla_breached) breaches,
               round(avg(resolution_minutes)::numeric,1) avg_tat,
               round(avg(first_response_minutes)::numeric,1) avg_first_response,
               round(avg(csat_score)::numeric,2) csat,
               count(csat_score) csat_responses
        FROM mart.v_service_kpi WHERE resolution_minutes IS NOT NULL
    """)[0]
    n = int(tot["requests"])
    return {
        "service_requests": n,
        "sla_breaches": int(tot["breaches"]),
        "sla_breach_rate_pct": round(100.0 * int(tot["breaches"]) / n, 2) if n else 0.0,
        "avg_resolution_minutes": float(tot["avg_tat"] or 0),
        "avg_first_response_minutes": float(tot["avg_first_response"] or 0),
        "csat": float(tot["csat"] or 0),
        # Published beside CSAT on purpose: about a third respond, so the score
        # carries selection bias and a bare number would overstate its authority.
        "csat_response_rate_pct": round(100.0 * int(tot["csat_responses"]) / n, 2) if n else 0.0,
        "sla_clock": "wall-clock minutes from request creation, not business hours",
    }


def operations_sla_matrix() -> list[dict]:
    """Property x day-part. The segmentation that makes F1 visible at all."""
    return db.fetch_all("""
        SELECT property_code, day_part_ist, owning_team,
               count(*) requests,
               count(*) FILTER (WHERE is_sla_breached) breaches,
               round(100.0*count(*) FILTER (WHERE is_sla_breached)/count(*),1) breach_pct,
               round(avg(resolution_minutes)::numeric,0) avg_tat_min
        FROM mart.v_service_kpi
        WHERE resolution_minutes IS NOT NULL
        GROUP BY 1,2,3 HAVING count(*) >= 5
        ORDER BY breach_pct DESC LIMIT 40
    """)


def service_requests_by_category() -> list[dict]:
    return db.fetch_all("""
        SELECT category, subcategory, owning_team,
               count(*) requests,
               count(*) FILTER (WHERE is_sla_breached) breaches,
               round(avg(resolution_minutes)::numeric,0) avg_tat_min,
               round(avg(sla_minutes)::numeric,0) sla_target_min
        FROM mart.v_service_kpi
        WHERE resolution_minutes IS NOT NULL
        GROUP BY 1,2,3 ORDER BY requests DESC LIMIT 40
    """)


# ---------------------------------------------------------------------------
def guest_intelligence_overview() -> dict[str, Any]:
    """Reads STORED, already-validated AI output. No model call on a page request."""
    tot = db.fetch_all("""
        SELECT count(*) aspects,
               count(DISTINCT review_key) reviews,
               count(*) FILTER (WHERE polarity='negative') negative,
               count(*) FILTER (WHERE severity IN ('severe','moderate')) serious,
               count(DISTINCT model) models
        FROM mart.fact_review_aspect WHERE evidence_verified
    """)[0]
    buried = db.scalar("SELECT count(*) FROM mart.v_buried_complaints")
    quarantined = db.scalar("SELECT count(*) FROM meta.absa_quarantine")
    ratings = db.fetch_all("""
        SELECT round(avg(rating)::numeric,2) avg_rating,
               round(100.0*count(*) FILTER (WHERE rating >= 4.0)/count(*),1) pct_4plus
        FROM mart.fact_review WHERE rating IS NOT NULL
    """)[0]
    model = db.scalar("SELECT model FROM mart.fact_review_aspect LIMIT 1")
    return {
        "aspects_extracted": int(tot["aspects"]),
        "reviews_analysed": int(tot["reviews"]),
        "negative_aspects": int(tot["negative"]),
        "serious_aspects": int(tot["serious"]),
        "buried_complaints": int(buried),
        "quarantined_extractions": int(quarantined),
        "avg_review_rating": float(ratings["avg_rating"] or 0),
        "pct_reviews_4_or_higher": float(ratings["pct_4plus"] or 0),
        "model": model,
        "method": "aspect-based extraction over a closed 13-category taxonomy",
        "validation": ("every published aspect carries a verbatim evidence span "
                       "asserted to be a literal substring of its source review; "
                       "failures are quarantined, not flagged"),
        "why_not_sentiment": (f"{float(ratings['pct_4plus'] or 0)}% of rated reviews are 4.0+, "
                              f"so a document-level classifier returns positive for "
                              f"almost all of them and surfaces none of the "
                              f"{int(buried)} operational problems hiding inside them"),
    }


def guest_aspects() -> list[dict]:
    return db.fetch_all("""
        SELECT category, polarity,
               count(*) mentions,
               count(*) FILTER (WHERE severity='severe') severe,
               count(*) FILTER (WHERE severity='moderate') moderate,
               min(actionable_by) routes_to
        FROM mart.fact_review_aspect
        WHERE evidence_verified
        GROUP BY 1,2 ORDER BY mentions DESC LIMIT 60
    """)


def guest_issues(limit: int = 25) -> list[dict]:
    """Buried complaints: negative aspects inside reviews rated 4.0 or higher."""
    return db.fetch_all("""
        SELECT review_id, property_code, review_date, rating, language,
               category, severity, actionable_by, confidence, evidence_span
        FROM mart.v_buried_complaints
        LIMIT :n
    """, n=min(limit, 100))


def ai_benchmark() -> list[dict]:
    return db.fetch_all("""
        SELECT method, model, scope, scope_value, n_gold,
               true_positive, false_positive, false_negative,
               precision_pct, recall_pct, f1_pct, notes
        FROM meta.ai_eval_result
        WHERE scope = 'overall'
        ORDER BY run_at DESC, method LIMIT 20
    """)


# ---------------------------------------------------------------------------
def data_quality_overview() -> dict[str, Any]:
    r = db.fetch_all("""
        WITH latest AS (
            SELECT res.*, ru.severity, ru.dimension
            FROM meta.dq_result res
            JOIN meta.dq_rule ru ON ru.rule_id = res.rule_id
            WHERE res.result_id IN (SELECT max(result_id) FROM meta.dq_result GROUP BY rule_id)
        )
        SELECT count(*) rules, count(*) FILTER (WHERE passed) passed,
               sum(rows_failed) rows_failed, max(checked_at) checked_at,
               round(100.0 * sum(CASE WHEN passed THEN
                        CASE severity WHEN 'error' THEN 3 WHEN 'warning' THEN 2 ELSE 1 END
                     ELSE 0 END)::numeric
                   / NULLIF(sum(CASE severity WHEN 'error' THEN 3
                                              WHEN 'warning' THEN 2 ELSE 1 END), 0), 2) score
        FROM latest
    """)[0]
    return {
        "quality_score": float(r["score"] or 0),
        "scoring": "severity-weighted pass rate (error 3 / warning 2 / info 1)",
        "rules_total": int(r["rules"]),
        "rules_passed": int(r["passed"]),
        "rules_failed": int(r["rules"]) - int(r["passed"]),
        "rows_affected": int(r["rows_failed"] or 0),
        "last_checked_at": r["checked_at"],
        "note": ("the score is intentionally below 100: the dataset carries deliberate "
                 "planted defects, and a clean score would mean the checks were decorative"),
    }


def data_quality_rules() -> list[dict]:
    return db.fetch_all("""
        SELECT ru.rule_id, ru.dimension, ru.target_table, ru.severity, ru.description,
               res.rows_checked, res.rows_failed, res.failure_pct, res.passed,
               ru.threshold_pct, res.checked_at
        FROM meta.dq_result res
        JOIN meta.dq_rule ru ON ru.rule_id = res.rule_id
        WHERE res.result_id IN (SELECT max(result_id) FROM meta.dq_result GROUP BY rule_id)
        ORDER BY res.passed, ru.severity, ru.rule_id
    """)


def metric_definitions() -> list[dict]:
    return db.fetch_all("""
        SELECT metric_key, display_name, business_definition, formula_text,
               grain, date_basis, unit, revenue_basis, owner_team, caveats,
               source_tables
        FROM meta.metric_definition WHERE is_active ORDER BY metric_key
    """)


def pipeline_runs(limit: int = 20) -> list[dict]:
    return db.fetch_all("""
        SELECT run_id, pipeline, started_at, finished_at, status,
               rows_out, rows_rejected, notes
        FROM meta.pipeline_run ORDER BY started_at DESC LIMIT :n
    """, n=min(limit, 100))


# ---------------------------------------------------------------------------
def health_readiness() -> dict[str, Any]:
    """Database reachability only. No credentials, no host names, no LLM call."""
    started = dt.datetime.now(dt.UTC)
    try:
        version = db.scalar("SELECT version()")
        tables = db.scalar("""
            SELECT count(*) FROM information_schema.tables
            WHERE table_schema IN ('mart','meta')
        """)
        latency = (dt.datetime.now(dt.UTC) - started).total_seconds() * 1000
        return {
            "database": "reachable",
            "engine": (version or "").split(" on ")[0],
            "analytical_tables": int(tables),
            "latency_ms": round(latency, 1),
        }
    except Exception as exc:  # noqa: BLE001
        # Type name only. A driver message can contain the host and user.
        return {"database": "unreachable", "error_type": type(exc).__name__}


# ---------------------------------------------------------------------------
# Revenue management.
#
# These wrap `staypulse.analytics.revenue`, `.forecast` and `.rootcause` rather than
# re-implementing any of it, for the same reason the rest of this module reads the
# semantic layer: a second implementation of pickup living inside a route handler
# would be a second definition of pickup.
#
# The forecast backtest is NOT run on the request path. Five models over forty
# origins is about twelve seconds of work -- fine for a scheduled job, unacceptable
# for a page load -- so accuracy is read from the stored evaluation.
# ---------------------------------------------------------------------------
_FORECAST_EVAL = Path(__file__).resolve().parents[2] / "reports" / "forecast_accuracy.json"


def data_bounds() -> dict[str, dt.date]:
    r = db.fetch_all(
        "SELECT min(stay_date) a, max(stay_date) b FROM mart.fact_unit_night"
    )[0]
    return {"first": r["a"], "last": r["b"]}


def default_as_of() -> dt.date:
    """Last date at which a full 30-day forward book exists in this dataset.

    No reservations exist for arrivals beyond the inventory horizon, so an as-of date
    at the horizon itself would show only continuing stays. Anchored to the data
    rather than to the wall clock, so the endpoint cannot silently go empty with time.
    """
    return data_bounds()["last"] - dt.timedelta(days=30)


def rm_overview(as_of: dt.date) -> dict[str, Any]:
    return _rv.summary(as_of)


def rm_on_the_books(as_of: dt.date, horizon_days: int) -> dict[str, Any]:
    rows = _rv.on_the_books(as_of, horizon_days)
    return {
        "as_of": as_of.isoformat(),
        "horizon_days": horizon_days,
        "total_nights_on_books": sum(int(r["nights_on_books"]) for r in rows),
        "total_revenue_on_books_inr": round(
            sum(float(r["revenue_otb_inr"] or 0) for r in rows), 2
        ),
        "by_stay_date": [
            {
                "stay_date": r["stay_date"].isoformat(),
                "property": r["property_name"],
                "days_out": int(r["days_out"]),
                "nights_on_books": int(r["nights_on_books"]),
                "revenue_on_books_inr": float(r["revenue_otb_inr"] or 0),
            }
            for r in rows[:MAX_ROWS]
        ],
    }


def rm_pickup(as_of: dt.date, lookback_days: int) -> list[dict[str, Any]]:
    return [
        {
            "activity_date": r["activity_date"].isoformat(),
            "nights_added": int(r["nights_added"] or 0),
            "nights_cancelled": int(r["nights_cancelled"] or 0),
            "nights_net": int(r["nights_net"] or 0),
            "revenue_added_inr": float(r["revenue_added_inr"] or 0),
            "revenue_cancelled_inr": float(r["revenue_cancelled_inr"] or 0),
        }
        for r in _rv.pickup(as_of, lookback_days)
    ]


def _pace_row(p: _rv.PaceRow) -> dict[str, Any]:
    return {
        "stay_date": p.stay_date.isoformat(),
        "property": p.property_name,
        "days_out": p.days_out,
        "nights_on_books": p.nights_on_books,
        "expected_nights": p.expected_nights,
        "usual_range": [p.p25_nights, p.p75_nights],
        "gap_nights": p.gap_nights,
        "pace_pct": p.pace_pct,
        "status": p.status,
        "support": p.support,
        "revenue_on_books_inr": round(p.revenue_otb_inr, 2),
    }


def rm_pace(as_of: dt.date) -> dict[str, Any]:
    rows = _rv.pace(as_of)
    return {
        "as_of": as_of.isoformat(),
        "method": (
            "Absolute nights on the books against the median for the same property, "
            "same weekday and same days-out horizon, from the last "
            f"{_rv.BENCHMARK_WINDOW} comparable dates before the snapshot. A date is "
            "flagged only if it is BOTH outside the p25-p75 band AND at least "
            f"{_rv.MATERIAL_NIGHTS:.0f} room-nights from the median."
        ),
        "counts": {
            "scored": len(rows),
            "behind": sum(1 for r in rows if r.status == "behind"),
            "on_track": sum(1 for r in rows if r.status == "on_track"),
            "ahead": sum(1 for r in rows if r.status == "ahead"),
        },
        "stay_dates": [_pace_row(p) for p in rows[:MAX_ROWS]],
    }


def rm_signals(as_of: dt.date, limit: int) -> list[dict[str, Any]]:
    return [s.as_dict() for s in _rv.opportunity_signals(as_of, limit)]


def rm_booking_curve() -> dict[str, Any]:
    rows = db.fetch_all("""
        SELECT c.days_out, p.property_name, c.stay_dates,
               c.avg_nights_on_books, c.median_pct_sold, c.p25_pct_sold, c.p75_pct_sold
        FROM mart.v_booking_curve c
        JOIN mart.dim_property p USING (property_key)
        WHERE c.days_out <= 45
        ORDER BY p.property_name, c.days_out
    """)
    return {
        "note": ("Built from completed stay dates only. A stay date still in the "
                 "future has an incomplete final total and would drag the curve down "
                 "at every horizon."),
        "curve": [
            {
                "property": r["property_name"],
                "days_out": int(r["days_out"]),
                "stay_dates": int(r["stay_dates"]),
                "avg_nights_on_books": float(r["avg_nights_on_books"]),
                "median_pct_sold": float(r["median_pct_sold"] or 0),
                "p25_pct_sold": float(r["p25_pct_sold"] or 0),
                "p75_pct_sold": float(r["p75_pct_sold"] or 0),
            }
            for r in rows
        ],
    }


def rm_lead_time() -> list[dict[str, Any]]:
    return [
        {
            "channel": r["channel_name"],
            "channel_type": r["channel_type"],
            "bookings": int(r["bookings"]),
            "mean_days": float(r["mean_days"]),
            "median_days": float(r["median_days"]),
            "p25_days": float(r["p25_days"]),
            "p75_days": float(r["p75_days"]),
            "p90_days": float(r["p90_days"]),
            "pct_same_day": float(r["pct_same_day"]),
            "pct_30d_plus": float(r["pct_30d_plus"]),
            "cancel_rate_pct": float(r["cancel_rate_pct"]),
        }
        for r in db.fetch_all(
            "SELECT * FROM mart.v_lead_time_profile ORDER BY bookings DESC"
        )
    ]


def rm_wash() -> dict[str, Any]:
    rows = db.fetch_all("""
        SELECT c.channel_name,
               sum(f.bookings_made)      AS made,
               sum(f.bookings_cancelled) AS cancelled,
               sum(f.bookings_no_show)   AS no_show,
               sum(f.bookings_stayed)    AS stayed,
               round(100.0 * sum(f.bookings_cancelled)
                     / NULLIF(sum(f.bookings_made), 0), 2) AS cancel_rate_pct,
               round(100.0 * sum(f.bookings_no_show)
                     / NULLIF(sum(f.bookings_made), 0), 2) AS no_show_rate_pct,
               round(100.0 * (sum(f.bookings_cancelled) + sum(f.bookings_no_show))
                     / NULLIF(sum(f.bookings_made), 0), 2) AS wash_rate_pct
        FROM mart.v_cancellation_funnel f
        JOIN mart.dim_channel c USING (channel_key)
        GROUP BY 1 ORDER BY wash_rate_pct DESC
    """)
    return {
        "note": ("Cohorted on stay month, not booking month, so the denominator is "
                 "demand for that month. This is the number an overbooking policy "
                 "would rest on; it is not a forecast of future wash."),
        "by_channel": [
            {
                "channel": r["channel_name"],
                "bookings_made": int(r["made"]),
                "cancelled": int(r["cancelled"]),
                "no_show": int(r["no_show"]),
                "stayed": int(r["stayed"]),
                "cancel_rate_pct": float(r["cancel_rate_pct"]),
                "no_show_rate_pct": float(r["no_show_rate_pct"]),
                "wash_rate_pct": float(r["wash_rate_pct"]),
            }
            for r in rows
        ],
    }


def rm_grain_reconciliation() -> dict[str, Any]:
    r = db.fetch_all("SELECT * FROM mart.v_grain_reconciliation")[0]
    exploded = int(r["exploded_booking_nights"])
    unalloc = int(r["unallocated_nights"])
    hourly = int(r["hourly_unit_nights"])
    occupied = int(r["occupied_unit_nights"])
    return {
        "explanation": (
            "The demand grain (booking-nights) and the inventory grain (unit-nights) "
            "do not trivially agree. Two structural differences explain the gap "
            "exactly, and both are asserted by the test suite."
        ),
        "exploded_booking_nights": exploded,
        "less_unallocated_nights": unalloc,
        "plus_hourly_unit_nights": hourly,
        "equals_occupied_unit_nights": occupied,
        "identity_holds": exploded - unalloc + hourly == occupied,
        "unallocated_pct": round(100.0 * unalloc / exploded, 2) if exploded else None,
        "notes": [
            f"{unalloc} booked nights were never allocated a specific unit - denied "
            "demand, the allocation gap.",
            f"{hourly} hourly bookings occupy a unit-night while selling no "
            "room-night, because nights are half-open [check_in, check_out) and these "
            "check out on the day they check in.",
        ],
    }


def rm_forecast(horizon_days: int, model: str) -> dict[str, Any]:
    return {
        "model": model,
        "horizon_days": horizon_days,
        "target": "daily occupied room-nights, portfolio total",
        "caveat": (
            "The pickup model is the default because it wins the backtest at 1, 7 and "
            "14 days. It LOSES at 30 days to a day-of-week moving average - see "
            "/forecast/accuracy before relying on a long horizon."
        ),
        "forecast": _fc.forward(horizon=horizon_days, model=model),
    }


def rm_forecast_accuracy() -> dict[str, Any]:
    """Served from the stored evaluation, not recomputed per request."""
    if not _FORECAST_EVAL.exists():
        raise RuntimeError("Forecast evaluation has not been generated yet.")
    stored = json.loads(_FORECAST_EVAL.read_text(encoding="utf-8"))
    stored["source"] = "stored evaluation, regenerated by scripts/run_forecast_eval.py"
    return stored


def rm_why(end: dt.date, days: int) -> dict[str, Any]:
    start = end - dt.timedelta(days=days - 1)
    return _rc.explain_revpar(start, end).as_dict()
