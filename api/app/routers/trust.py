"""Data quality, metric definitions and pipeline observability.

This router is the reason the API is worth having rather than a chart feed: it lets a
consumer ask *why* a number should be believed, not just what it is.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from api.app import services

router = APIRouter(prefix="/api", tags=["trust"])


@router.get("/data-quality/overview", summary="Severity-weighted quality score")
def dq_overview() -> dict:
    return services.data_quality_overview()


@router.get("/data-quality/rules", summary="Every rule with its latest result")
def dq_rules() -> dict:
    rules = services.data_quality_rules()
    return {
        "count": len(rules),
        "failing": sum(1 for r in rules if not r["passed"]),
        "note": ("failures are expected - the dataset carries deliberate planted "
                 "defects and all 10 planted classes are detected. A clean scorecard "
                 "would mean the checks were decorative."),
        "rules": rules,
    }


@router.get("/metrics", summary="The metric registry the semantic layer executes")
def metrics() -> dict:
    return {
        "note": ("date_basis is CHECK-constrained in the database, so a metric cannot "
                 "be registered without declaring which date it is measured on - the "
                 "single most common cause of two dashboards disagreeing"),
        "metrics": services.metric_definitions(),
    }


@router.get("/pipeline-runs", summary="ETL, quality and AI run history")
def runs(limit: int = Query(20, ge=1, le=100)) -> dict:
    return {"runs": services.pipeline_runs(limit)}
