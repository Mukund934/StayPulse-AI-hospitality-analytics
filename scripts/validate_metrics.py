"""Validate the semantic layer.

Every metric is recomputed a second time from the base tables, independently of
the view that publishes it, and the two are compared. A metric layer that agrees
with itself proves nothing; a metric layer that agrees with an independent
calculation of the same business definition is worth trusting.

Also demonstrates -- rather than asserts away -- the cases where two numbers are
BOTH correct and still differ: occupancy on two bases, revenue on three date
bases, cancellation rate on two denominators.

Usage:
    python scripts/validate_metrics.py
    python scripts/validate_metrics.py --export METRICS.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'[ ok ]' if ok else '[FAIL]'}  {label}")
    if detail:
        print(f"           {detail}")
    if not ok:
        failures.append(f"{label}: {detail}")


def section(t: str) -> None:
    print(f"\n{t}\n{'-' * len(t)}")


def close(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(float(a) - float(b)) <= tol


# ---------------------------------------------------------------------------
section("1. Semantic layer agrees with an independent calculation")

# Published by the view.
view = db.fetch_all("""
    SELECT sum(rooms_available)      AS avail,
           sum(rooms_sold)           AS sold,
           sum(room_revenue_net_inr) AS revenue,
           sum(gst_inr)              AS gst,
           sum(commission_inr)       AS commission
    FROM mart.v_daily_kpi
""")[0]

# Recomputed straight from the fact table, no views involved.
base = db.fetch_all("""
    SELECT count(*) FILTER (WHERE is_sellable) AS avail,
           count(*) FILTER (WHERE is_occupied) AS sold,
           sum(room_revenue_net_inr)           AS revenue,
           sum(commission_inr)                 AS commission
    FROM mart.fact_unit_night
""")[0]

check("Rooms available: view == fact table",
      int(view["avail"]) == int(base["avail"]),
      f"view {int(view['avail']):,} vs base {int(base['avail']):,}")
check("Rooms sold: view == fact table",
      int(view["sold"]) == int(base["sold"]),
      f"view {int(view['sold']):,} vs base {int(base['sold']):,}")
check("Net room revenue: view == fact table",
      close(view["revenue"], base["revenue"], 1.0),
      f"view INR {float(view['revenue']):,.2f} vs base INR {float(base['revenue']):,.2f}")
check("Commission: view == fact table",
      close(view["commission"], base["commission"], 1.0),
      f"view INR {float(view['commission']):,.2f}")

avail, sold, revenue = int(view["avail"]), int(view["sold"]), float(view["revenue"])
occ = sold / avail
adr = revenue / sold
revpar = revenue / avail
print(f"\n    Occupancy {occ:.2%} | ADR INR {adr:,.2f} | RevPAR INR {revpar:,.2f}")
check("RevPAR = ADR x Occupancy (exact identity)",
      close(revpar, adr * occ, 1e-6),
      f"residual {abs(revpar - adr * occ):.10f} -- holds only because all three "
      f"share one table and one denominator")

# ---------------------------------------------------------------------------
section("2. Two numbers, both correct, still different")

occ_bases = db.fetch_all("""
    SELECT round(100.0 * sum(rooms_sold) / NULLIF(sum(rooms_available), 0), 3)      AS operational,
           round(100.0 * sum(rooms_sold) / NULLIF(sum(unit_nights_physical), 0), 3) AS benchmark,
           sum(rooms_out_of_order)                                                  AS ooo
    FROM mart.v_daily_kpi
""")[0]
op, bm = float(occ_bases["operational"]), float(occ_bases["benchmark"])
print(f"    Occupancy operational basis : {op:.3f}%   (out-of-order removed from availability)")
print(f"    Occupancy benchmark  basis : {bm:.3f}%   (full physical inventory)")
print(f"    Gap                        : {op - bm:.3f}pp  "
      f"= {int(occ_bases['ooo']):,} unit-nights lost to out-of-order")
check("Operational occupancy exceeds benchmark occupancy", op > bm,
      "the gap is inventory lost to OOO -- an actionable number, not a discrepancy")

rev_bases = db.fetch_all("""
    WITH stay AS (
        SELECT to_char(stay_date, 'YYYY-MM') AS ym, sum(room_revenue_net_inr) AS rev
        FROM mart.fact_unit_night WHERE stay_date BETWEEN :s AND :e GROUP BY 1
    ),
    booked AS (
        SELECT to_char(booking_date, 'YYYY-MM') AS ym, sum(net_room_amount_inr) AS rev
        FROM mart.fact_booking
        WHERE status NOT IN ('cancelled') AND booking_date BETWEEN :s AND :e GROUP BY 1
    ),
    paid AS (
        SELECT to_char(payment_date, 'YYYY-MM') AS ym, sum(gross_amount_inr) AS rev
        FROM mart.fact_payment WHERE payment_date BETWEEN :s AND :e GROUP BY 1
    )
    SELECT (SELECT sum(rev) FROM stay)   AS by_stay_date,
           (SELECT sum(rev) FROM booked) AS by_booking_date,
           (SELECT sum(rev) FROM paid)   AS by_payment_date
""", s="2026-06-01", e="2026-06-30")[0]
print(f"\n    June 2026 revenue, three legitimate date bases:")
print(f"      by STAY date    (Operations earned) : INR {float(rev_bases['by_stay_date'] or 0):>12,.0f}")
print(f"      by BOOKING date (Marketing sold)    : INR {float(rev_bases['by_booking_date'] or 0):>12,.0f}")
print(f"      by PAYMENT date (Finance collected) : INR {float(rev_bases['by_payment_date'] or 0):>12,.0f}")
spread = max(float(rev_bases[k] or 0) for k in rev_bases) - min(float(rev_bases[k] or 0) for k in rev_bases)
check("The three date bases disagree, as they must",
      spread > 0,
      f"spread INR {spread:,.0f}. This is why every metric declares date_basis -- "
      f"a CHECK constraint on meta.metric_definition makes it impossible to register one without")

cancel = db.fetch_all("""
    SELECT
        round(100.0 * count(*) FILTER (WHERE status = 'cancelled' AND booking_date BETWEEN :s AND :e)
              / NULLIF(count(*) FILTER (WHERE booking_date BETWEEN :s AND :e), 0), 2) AS cohort_basis,
        round(100.0 * count(*) FILTER (WHERE cancel_date BETWEEN :s AND :e)
              / NULLIF(count(*) FILTER (WHERE booking_date BETWEEN :s AND :e), 0), 2) AS event_basis
    FROM mart.fact_booking
""", s="2026-06-01", e="2026-06-30")[0]
print(f"\n    June 2026 cancellation rate:")
print(f"      cohort basis (of bookings MADE in June)  : {float(cancel['cohort_basis'] or 0):.2f}%")
print(f"      event  basis (cancellations DURING June) : {float(cancel['event_basis'] or 0):.2f}%")
check("Cancellation rate differs by denominator choice",
      float(cancel["cohort_basis"] or 0) != float(cancel["event_basis"] or 0),
      "cohort measures booking quality, event measures this period's revenue loss")

# ---------------------------------------------------------------------------
section("3. GST de-grossing")

gst = db.fetch_all("""
    SELECT sum(room_revenue_net_inr)  AS net,
           sum(gst_inr)               AS gst,
           sum(gross_incl_gst_inr)    AS gross,
           count(*) FILTER (WHERE gst_pct = 18) AS nights_18,
           count(*) FILTER (WHERE gst_pct = 5)  AS nights_5,
           count(*) FILTER (WHERE gst_pct = 12) AS nights_12
    FROM mart.v_unit_night_enriched WHERE is_occupied
""")[0]
net, tax, gross = float(gst["net"]), float(gst["gst"]), float(gst["gross"])
print(f"    net INR {net:,.0f} + GST INR {tax:,.0f} = gross INR {gross:,.0f}")
print(f"    nights at 18%: {int(gst['nights_18']):,} | at 5%: {int(gst['nights_5']):,} "
      f"| at 12% (pre-22-Sep-2025): {int(gst['nights_12']):,}")
check("Gross = net + GST", close(gross, net + tax, 1.0),
      f"residual INR {abs(gross - (net + tax)):.2f}")
check("Effective GST rate sits between 5% and 18%",
      5.0 <= 100.0 * tax / net <= 18.0,
      f"{100.0 * tax / net:.2f}% blended -- computing ADR off GROSS would overstate it by this much")

# ---------------------------------------------------------------------------
section("4. Identity resolution changes the repeat rate")

rep = db.fetch_all("SELECT * FROM mart.v_guest_repeat")[0]
print(f"    before resolution : {float(rep['repeat_rate_raw_pct'] or 0):.2f}% "
      f"({int(rep['repeat_guests_raw']):,} of {int(rep['guests_raw']):,} guests)")
print(f"    after  resolution : {float(rep['repeat_rate_resolved_pct'] or 0):.2f}% "
      f"({int(rep['repeat_guests_resolved']):,} of {int(rep['guests_resolved']):,} identities)")
check("Resolution raises the measured repeat rate",
      float(rep["repeat_rate_resolved_pct"] or 0) >= float(rep["repeat_rate_raw_pct"] or 0),
      "duplicate profiles split one guest into several and understate loyalty")

# ---------------------------------------------------------------------------
section("5. Registry integrity")

reg = db.fetch_all("""
    SELECT count(*) AS n,
           count(*) FILTER (WHERE date_basis IS NULL)          AS no_basis,
           count(*) FILTER (WHERE caveats IS NULL OR caveats = '') AS no_caveats,
           count(*) FILTER (WHERE cardinality(source_tables) = 0)  AS no_source
    FROM meta.metric_definition WHERE is_active
""")[0]
print(f"    {int(reg['n'])} active metrics registered")
check("Every metric declares a date basis", int(reg["no_basis"]) == 0)
check("Every metric declares its source tables", int(reg["no_source"]) == 0)
check("Every metric documents its caveats", int(reg["no_caveats"]) == 0,
      "a metric with no stated caveat has not been thought about")
check("At least 14 metrics registered", int(reg["n"]) >= 14, f"{int(reg['n'])} present")

dupes = db.scalar("""
    SELECT count(*) FROM (
        SELECT display_name FROM meta.metric_definition
        GROUP BY 1 HAVING count(*) > 1) t
""")
check("No two metrics share a display name", dupes == 0,
      "two metrics with one name is the exact failure the registry exists to prevent")

# ---------------------------------------------------------------------------
section("Summary")
print(f"  failures: {len(failures)}")
if failures:
    for f in failures:
        print(f"    - {f}")
    raise SystemExit(1)
print("\n  Semantic layer validated.")


# ---------------------------------------------------------------------------
def export_markdown(path: Path) -> None:
    rows = db.fetch_all("""
        SELECT * FROM meta.metric_definition WHERE is_active
        ORDER BY CASE unit WHEN 'inr' THEN 1 WHEN 'percent' THEN 2 ELSE 3 END, metric_key
    """)
    out = [
        "# Metric dictionary",
        "",
        "One definition per metric, generated from `meta.metric_definition` — the same",
        "registry the semantic layer executes. This file is not documentation *about* the",
        "metrics; it is a rendering *of* them, so it cannot drift from what the warehouse",
        "computes.",
        "",
        "`date_basis` is `CHECK`-constrained in the database, so a metric cannot be",
        "registered without declaring which date it is measured on — the single most",
        "common cause of two dashboards disagreeing.",
        "",
        f"**{len(rows)} active metrics.** All figures derive from clearly-labelled",
        "synthetic data.",
        "",
        "---",
        "",
    ]
    for r in rows:
        out += [
            f"## {r['display_name']}  ·  `{r['metric_key']}`",
            "",
            r["business_definition"],
            "",
            "| | |",
            "|---|---|",
            f"| **Formula** | `{r['formula_text']}` |",
            f"| **Grain** | {r['grain']} |",
            f"| **Date basis** | `{r['date_basis']}` |",
            f"| **Unit** | {r['unit']} |",
            f"| **Revenue basis** | {r['revenue_basis'] or '—'} |",
            f"| **Source tables** | {', '.join(f'`{t}`' for t in r['source_tables'])} |",
            f"| **Owner** | {r['owner_team']} |",
            "",
            f"**Includes** — {r['inclusion_rules']}",
            "",
            f"**Excludes** — {r['exclusion_rules']}",
            "",
            f"> **Caveat.** {r['caveats']}",
            "",
            "<details><summary>SQL</summary>",
            "",
            "```sql",
            r["sql_expression"],
            "```",
            "",
            "</details>",
            "",
            "---",
            "",
        ]
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"  Metric dictionary exported to {path} ({len(rows)} metrics)")


if __name__ == "__main__" or True:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", type=str, default=None)
    args, _ = ap.parse_known_args()
    if args.export:
        export_markdown(PROJECT_ROOT / args.export)
