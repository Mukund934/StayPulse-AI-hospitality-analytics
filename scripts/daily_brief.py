"""Generate the daily operations brief.

The recurring report the role asks you to own and automate. Runs end to end:
load, quality gate, KPIs, anomaly scan, guest issues, then a written brief with a
recommendation and an explicit confidence.

THE RULE THAT MATTERS: every number in the prose is computed in SQL or pandas and
passed in as a fact. Nothing here asks a language model to calculate, average or
extrapolate. The narration is template-driven for exactly that reason -- the
documented failure mode of LLM narration is inventing growth rates that were never
calculated, and a briefing that quietly fabricates a percentage is worse than no
briefing.

Output is written to reports/briefings/<date>.md and committed by the scheduled
workflow, so the git history becomes the verifiable evidence that the automation
actually ran.

Usage:
    python scripts/daily_brief.py
    python scripts/daily_brief.py --as-of 2026-08-11
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

# The Windows console defaults to cp1252, which cannot encode the rupee sign. The
# brief file itself is written as UTF-8 regardless; this only affects the echo to
# stdout, and without it the script dies AFTER doing all its work.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd  # noqa: E402
from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402
from staypulse.analytics import anomaly as an  # noqa: E402
from staypulse.quality import runner as dq  # noqa: E402


def pct(new: float, old: float) -> str:
    if not old:
        return "n/a"
    return f"{100.0 * (new - old) / old:+.1f}%"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of", type=str, default=None)
    ap.add_argument("--no-quality", action="store_true",
                    help="skip the quality gate (faster local runs)")
    args = ap.parse_args()

    latest = db.scalar("SELECT max(stay_date) FROM mart.v_daily_kpi")
    as_of = dt.date.fromisoformat(args.as_of) if args.as_of else latest
    print(f"StayPulse daily brief · as of {as_of}")

    run_id = None
    with db.connect() as conn:
        run_id = conn.execute(text(
            "INSERT INTO meta.pipeline_run (pipeline, notes) "
            "VALUES ('daily_brief', :n) RETURNING run_id"
        ), {"n": f"as_of={as_of}"}).scalar_one()

    try:
        # ---- 1. KPIs: the day, versus the same weekday a week earlier -----
        # Same weekday, not "yesterday": corporate demand is weekday-shaped, so
        # comparing a Saturday to a Friday manufactures a 20% collapse every week.
        kpi = db.fetch_all("""
            WITH d AS (
                SELECT sum(rooms_available) av, sum(rooms_sold) sold,
                       sum(room_revenue_net_inr) rev, sum(rooms_out_of_order) ooo
                FROM mart.v_daily_kpi WHERE stay_date = :d
            ),
            w AS (
                SELECT sum(rooms_available) av, sum(rooms_sold) sold,
                       sum(room_revenue_net_inr) rev
                FROM mart.v_daily_kpi WHERE stay_date = CAST(:d AS date) - 7
            )
            SELECT (SELECT rev FROM d) rev, (SELECT rev FROM w) rev_lw,
                   (SELECT sold FROM d) sold, (SELECT av FROM d) av,
                   (SELECT ooo FROM d) ooo,
                   (SELECT sold FROM w) sold_lw, (SELECT av FROM w) av_lw
        """, d=as_of)[0]

        rev = float(kpi["rev"] or 0)
        rev_lw = float(kpi["rev_lw"] or 0)
        sold, av = int(kpi["sold"] or 0), int(kpi["av"] or 0)
        sold_lw, av_lw = int(kpi["sold_lw"] or 0), int(kpi["av_lw"] or 0)
        occ = 100.0 * sold / av if av else 0.0
        occ_lw = 100.0 * sold_lw / av_lw if av_lw else 0.0
        adr = rev / sold if sold else 0.0
        revpar = rev / av if av else 0.0

        # ---- 2. Quality gate ---------------------------------------------
        quality = None
        if not args.no_quality:
            quality = dq.run_all(persist_results=True)

        # ---- 3. Anomaly scan ---------------------------------------------
        portfolio = pd.DataFrame(db.fetch_all("""
            SELECT stay_date,
                   sum(room_revenue_net_inr) AS revenue,
                   round(100.0*sum(rooms_sold)/NULLIF(sum(rooms_available),0),2) AS occupancy_pct
            FROM mart.v_daily_kpi WHERE stay_date <= :d GROUP BY 1 ORDER BY 1
        """, d=as_of))
        for c in ("revenue", "occupancy_pct"):
            portfolio[c] = pd.to_numeric(portfolio[c], errors="coerce")
        median_rev = float(portfolio["revenue"].median() or 0)
        alerts = (an.detect(portfolio, metric="revenue", min_abs_change=median_rev * 0.15)
                  + an.detect(portfolio, metric="occupancy_pct", min_abs_change=8.0))
        today_alerts = [a for a in alerts if a.date == as_of.isoformat()]

        # ---- 4. Operations ------------------------------------------------
        ops = db.fetch_all("""
            SELECT property_code, day_part_ist, count(*) requests,
                   count(*) FILTER (WHERE is_sla_breached) breaches,
                   round(avg(resolution_minutes)::numeric,0) tat
            FROM mart.v_service_kpi
            WHERE request_date BETWEEN CAST(:d AS date) - 6 AND :d
              AND resolution_minutes IS NOT NULL
            GROUP BY 1,2 HAVING count(*) >= 3
            ORDER BY count(*) FILTER (WHERE is_sla_breached) DESC,
                     avg(resolution_minutes) DESC LIMIT 3
        """, d=as_of)

        # ---- 5. Guest issues (AI, evidence-verified only) -----------------
        issues = db.fetch_all("""
            SELECT category, count(*) n,
                   count(*) FILTER (WHERE severity IN ('severe','moderate')) serious,
                   min(actionable_by) team
            FROM mart.fact_review_aspect
            WHERE polarity = 'negative' AND evidence_verified
              AND review_date BETWEEN CAST(:d AS date) - 29 AND :d
            GROUP BY 1 ORDER BY 2 DESC LIMIT 3
        """, d=as_of)
        buried = db.scalar("""
            SELECT count(*) FROM mart.v_buried_complaints
            WHERE review_date BETWEEN CAST(:d AS date) - 29 AND :d
        """, d=as_of)

        # ---- 6. Compose ---------------------------------------------------
        top_ops = ops[0] if ops else None
        top_issue = issues[0] if issues else None

        if today_alerts:
            a = max(today_alerts, key=lambda x: abs(x.robust_z))
            anomaly_line = (f"{a.metric} {a.direction} baseline: {a.actual:,.0f} vs "
                            f"{a.baseline:,.0f} ({a.deviation_pct:+.1f}%, z={a.robust_z:.1f})")
            anomaly_conf = a.confidence
        else:
            anomaly_line = "None. All monitored metrics within their day-of-week baseline."
            anomaly_conf = "n/a"

        if top_ops and int(top_ops["breaches"]) > 0:
            recommendation = (
                f"Review {top_ops['property_code']} {top_ops['day_part_ist']} coverage: "
                f"{int(top_ops['breaches'])} of {int(top_ops['requests'])} requests "
                f"breached SLA over the last 7 days at {int(top_ops['tat'])} min average "
                f"resolution.")
            rec_conf = "High" if int(top_ops["breaches"]) >= 5 else "Medium"
            rec_owner = "Operations"
        elif top_issue:
            recommendation = (
                f"Route {top_issue['category']} complaints to "
                f"{top_issue['team']}: {int(top_issue['n'])} negative mentions in 30 days, "
                f"{int(top_issue['serious'])} moderate or severe.")
            rec_conf, rec_owner = "Medium", "Customer Experience"
        else:
            recommendation = "No action required. No SLA breaches or complaint clusters."
            rec_conf, rec_owner = "High", "—"

        q_line = (f"{quality['quality_score']:.1f}/100 "
                  f"({quality['passed']}/{quality['total_rules']} rules passed, "
                  f"{quality['rows_affected']:,} rows affected)"
                  if quality else "not run")

        brief = f"""# StayPulse — Daily Operations Brief

**As of {as_of.isoformat()}**  ·  generated {dt.datetime.now().strftime('%Y-%m-%d %H:%M')}
·  *synthetic data*

## Performance — versus the same weekday last week

| Metric | Today | Same weekday last week | Change |
|---|---|---|---|
| Net room revenue | ₹{rev:,.0f} | ₹{rev_lw:,.0f} | **{pct(rev, rev_lw)}** |
| Occupancy | {occ:.1f}% | {occ_lw:.1f}% | **{occ - occ_lw:+.1f}pp** |
| ADR | ₹{adr:,.0f} | — | — |
| RevPAR | ₹{revpar:,.0f} | — | — |
| Units sold / available | {sold} / {av} | {sold_lw} / {av_lw} | — |
| Out of order | {int(kpi['ooo'] or 0)} | — | — |

Compared against the same weekday, not the previous day: corporate demand is
weekday-shaped, so a Saturday-versus-Friday comparison manufactures a collapse every
week.

## Anomaly scan

{anomaly_line}

**Confidence:** {anomaly_conf}

## Operational exceptions — last 7 days

| Property | Day part | Requests | SLA breaches | Avg resolution |
|---|---|---|---|---|
""" + ("\n".join(
            f"| {o['property_code']} | {o['day_part_ist']} | {int(o['requests'])} | "
            f"**{int(o['breaches'])}** | {int(o['tat'])} min |" for o in ops
        ) if ops else "| — | — | — | — | — |") + f"""

## Guest issues — last 30 days, aspect-level

| Issue | Mentions | Moderate/severe | Route to |
|---|---|---|---|
""" + ("\n".join(
            f"| `{i['category']}` | {int(i['n'])} | {int(i['serious'])} | {i['team']} |"
            for i in issues
        ) if issues else "| — | — | — | — |") + f"""

**{buried} negative aspects sat inside reviews rated 4.0 or higher.** Document-level
sentiment would have surfaced none of them.

## Recommended action

> {recommendation}

**Confidence:** {rec_conf}  ·  **Owner:** {rec_owner}

## Data trust

Quality score: **{q_line}**

---

*Every figure above is computed in SQL or pandas and passed to the template as a
fact. No language model calculates, averages or extrapolates any number in this
brief. Synthetic data throughout.*
"""

        out_dir = PROJECT_ROOT / "reports" / "briefings"
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{as_of.isoformat()}.md").write_text(brief, encoding="utf-8")
        (PROJECT_ROOT / "reports" / "LATEST_BRIEF.md").write_text(brief, encoding="utf-8")

        # Freshness stamp: what the staleness watchdog reads.
        import json
        (PROJECT_ROOT / "reports" / "_freshness.json").write_text(json.dumps({
            "generated_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "as_of": as_of.isoformat(),
            "revenue_inr": round(rev, 2),
            "occupancy_pct": round(occ, 2),
            "quality_score": quality["quality_score"] if quality else None,
            "alerts_today": len(today_alerts),
        }, indent=2), encoding="utf-8")

        with db.connect() as conn:
            conn.execute(text(
                "UPDATE meta.pipeline_run SET finished_at=now(), status='success', "
                "rows_out=1 WHERE run_id=:id"), {"id": run_id})

        print(brief)
        print(f"\nWritten to reports/briefings/{as_of.isoformat()}.md")
        return 0

    except Exception as exc:  # noqa: BLE001
        # Failures must be visible in the run log, not swallowed.
        with db.connect() as conn:
            conn.execute(text(
                "UPDATE meta.pipeline_run SET finished_at=now(), status='failed', "
                "error_message=:e WHERE run_id=:id"
            ), {"e": f"{type(exc).__name__}: {exc}"[:2000], "id": run_id})
        raise


if __name__ == "__main__":
    raise SystemExit(main())
