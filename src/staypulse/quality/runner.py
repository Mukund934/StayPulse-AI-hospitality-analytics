"""Data-quality execution engine.

Registers the declared rules, runs each against the warehouse, persists every
result to `meta.dq_result` so quality can be trended rather than only observed
once, and scores the framework on recall per planted defect class.

The scoring choice matters: a weighted pass rate is defensible, an unweighted one
is not. A failing `error` rule about unresolvable money should not be cancelled
out by a passing `info` rule about a nullable label.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text

from staypulse import db
from staypulse.quality.rules import RULES, Rule, defect_classes

# Severity weights for the composite score. Errors dominate deliberately.
SEVERITY_WEIGHT = {"error": 3.0, "warning": 2.0, "info": 1.0}


@dataclass
class RuleResult:
    rule: Rule
    rows_checked: int
    rows_failed: int
    failure_pct: float
    passed: bool
    sample_keys: list | None
    error: str | None = None

    @property
    def weight(self) -> float:
        return SEVERITY_WEIGHT.get(self.rule.severity, 1.0)


def register_rules() -> int:
    """Upsert rule definitions so `meta.dq_result` always has a parent to point at."""
    with db.connect() as conn:
        for r in RULES:
            conn.execute(text("""
                INSERT INTO meta.dq_rule
                    (rule_id, dimension, target_table, target_column, description,
                     severity, threshold_pct, is_active)
                VALUES (:id, :dim, :tbl, :col, :desc, :sev, :thr, true)
                ON CONFLICT (rule_id) DO UPDATE SET
                    dimension     = EXCLUDED.dimension,
                    target_table  = EXCLUDED.target_table,
                    target_column = EXCLUDED.target_column,
                    description   = EXCLUDED.description,
                    severity      = EXCLUDED.severity,
                    threshold_pct = EXCLUDED.threshold_pct,
                    is_active     = true
            """), {
                "id": r.rule_id, "dim": r.dimension, "tbl": r.target_table,
                "col": r.target_column, "desc": r.description,
                "sev": r.severity, "thr": r.threshold_pct,
            })
    return len(RULES)


def run_rule(rule: Rule) -> RuleResult:
    """Execute one rule. A rule that errors is a FAILED rule, not a skipped one."""
    try:
        rows = db.fetch_all(rule.sql)
        if not rows:
            return RuleResult(rule, 0, 0, 0.0, True, None,
                              error="query returned no rows")
        row = rows[0]
        checked = int(row.get("rows_checked") or 0)
        failed = int(row.get("rows_failed") or 0)
        pct = (100.0 * failed / checked) if checked else 0.0
        passed = pct <= rule.threshold_pct
        return RuleResult(rule, checked, failed, pct, passed, row.get("sample_keys"))
    except Exception as exc:  # noqa: BLE001
        # A broken check must not silently register as healthy.
        return RuleResult(rule, 0, 0, 100.0, False, None,
                          error=f"{type(exc).__name__}: {exc}"[:500])


def persist(results: list[RuleResult], run_id: int | None) -> None:
    import json

    with db.connect() as conn:
        for r in results:
            conn.execute(text("""
                INSERT INTO meta.dq_result
                    (rule_id, run_id, rows_checked, rows_failed, passed, sample_keys, notes)
                VALUES (:rid, :run, :checked, :failed, :passed, CAST(:sample AS jsonb), :notes)
            """), {
                "rid": r.rule.rule_id,
                "run": run_id,
                "checked": r.rows_checked,
                "failed": r.rows_failed,
                "passed": r.passed,
                "sample": json.dumps(r.sample_keys) if r.sample_keys is not None else None,
                "notes": r.error,
            })


def quality_score(results: list[RuleResult]) -> float:
    """Severity-weighted pass rate, 0-100."""
    total = sum(r.weight for r in results)
    if not total:
        return 0.0
    earned = sum(r.weight for r in results if r.passed)
    return round(100.0 * earned / total, 2)


def defect_recall(results: list[RuleResult]) -> list[dict]:
    """Per planted defect class: did any rule actually catch it?

    This is the difference between a quality suite that runs and one that works.
    A class with zero detections is reported as a MISS rather than omitted.
    """
    by_id = {r.rule.rule_id: r for r in results}
    out = []
    for klass, rule_ids in sorted(defect_classes().items()):
        detections = [by_id[rid] for rid in rule_ids if rid in by_id]
        caught = sum(r.rows_failed for r in detections)
        out.append({
            "defect_class": klass,
            "rules": rule_ids,
            "rows_detected": caught,
            "detected": caught > 0,
        })
    return out


def freshness() -> list[dict]:
    return db.fetch_all("""
        SELECT 'fact_booking'         AS source, max(ingested_at) AS last_seen,
               round(extract(epoch FROM now() - max(ingested_at)) / 3600.0, 1) AS hours_old
        FROM mart.fact_booking
        UNION ALL
        SELECT 'fact_unit_night', max(stay_date)::timestamptz,
               round(extract(epoch FROM now() - max(stay_date)::timestamptz) / 3600.0, 1)
        FROM mart.fact_unit_night
        UNION ALL
        SELECT 'fact_service_request', max(created_at),
               round(extract(epoch FROM now() - max(created_at)) / 3600.0, 1)
        FROM mart.fact_service_request
        UNION ALL
        SELECT 'fact_review', max(reviewed_at),
               round(extract(epoch FROM now() - max(reviewed_at)) / 3600.0, 1)
        FROM mart.fact_review
    """)


def run_all(*, persist_results: bool = True) -> dict:
    """Register, execute and score every active rule."""
    register_rules()

    run_id = None
    if persist_results:
        with db.connect() as conn:
            run_id = conn.execute(text(
                "INSERT INTO meta.pipeline_run (pipeline, notes) "
                "VALUES ('data_quality', :n) RETURNING run_id"
            ), {"n": f"{len(RULES)} rules"}).scalar_one()

    results = [run_rule(r) for r in RULES]

    if persist_results:
        persist(results, run_id)
        failed_n = sum(1 for r in results if not r.passed)
        with db.connect() as conn:
            conn.execute(text(
                "UPDATE meta.pipeline_run SET finished_at = now(), "
                "status = :s, rows_rejected = :rej WHERE run_id = :id"
            ), {
                "s": "success" if failed_n == 0 else "partial",
                "rej": sum(r.rows_failed for r in results),
                "id": run_id,
            })

    return {
        "run_id": run_id,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "results": results,
        "total_rules": len(results),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "errored": sum(1 for r in results if r.error),
        "rows_affected": sum(r.rows_failed for r in results),
        "quality_score": quality_score(results),
        "defect_recall": defect_recall(results),
        "freshness": freshness(),
    }
