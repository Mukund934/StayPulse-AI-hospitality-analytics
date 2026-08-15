"""StayPulse analytics API.

A read-only HTTP surface over the validated semantic layer. Deliberately narrow:

  - **No writes.** Every route is a GET over `mart.v_*` views. There is no endpoint
    that can mutate the warehouse.
  - **No user SQL.** No text-to-SQL, no query parameters interpolated into SQL, no
    table names accepted from the client. Every statement is bound-parameterised.
  - **No LLM on the request path.** AI results are read from storage where the batch
    pipeline already validated them. A dashboard page load must not cost API quota
    or introduce nondeterminism.
  - **No secrets in responses.** Driver exceptions are caught and replaced with a
    type name, because a psycopg error message can contain the host and username.

Run locally:
    uvicorn api.app.main:app --reload --port 8000
Docs:
    http://localhost:8000/docs
"""

from __future__ import annotations

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.app.routers import (
    analytics,
    health,
    intelligence,
    operations,
    revenue,
    trust,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("staypulse.api")

DESCRIPTION = """
Read-only analytics API over a governed hospitality warehouse.

**All data is synthetic**, generated from a documented seeded model of Indian
serviced-apartment operations. Rupee figures are arithmetic on stated assumptions,
not measured business outcomes.

Every metric is served from the semantic layer registered in
`meta.metric_definition` — the same definitions the SQL analyses, the Power BI model
and the automated daily brief read. A route handler never computes a KPI itself,
because that would create a second definition of it.

* `date_basis` is stated on every KPI response. Revenue on stay date, booking date
  and payment date are three legitimately different numbers.
* Occupancy is available on two bases: operational (out-of-order units removed from
  availability) and benchmark (full physical inventory).
* AI output is read from storage, already validated. Every aspect carries a verbatim
  evidence span proven to be a literal substring of its source review.
"""

app = FastAPI(
    title="StayPulse Analytics API",
    version="1.0.0",
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_tags=[
        {"name": "health", "description": "Liveness and readiness."},
        {"name": "analytics", "description": "Revenue, occupancy, rate and property performance."},
        {"name": "operations", "description": "Service requests, SLA and CSAT."},
        {"name": "intelligence", "description": "Guest feedback, anomalies and decisions."},
        {"name": "trust", "description": "Data quality, metric definitions and pipeline runs."},
    ],
)

# Explicit origins. A wildcard would let any site read the API and attribute it to
# this deployment; the cost of listing three origins is zero.
ALLOWED_ORIGINS = [
    "https://stay-pulse-ai-hospitality-analytics.vercel.app",
    "http://localhost:4173",
    "http://localhost:8000",
    "http://127.0.0.1:4173",
]
# Vercel preview deployments get generated subdomains, so allow them by regex
# rather than pinning every build URL.
PREVIEW_REGEX = r"https://stay-pulse-ai-hospitality-analytics-[a-z0-9-]+\.vercel\.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_origin_regex=PREVIEW_REGEX,
    allow_credentials=False,   # nothing here is per-user; no cookies to protect
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
    max_age=600,
)


@app.middleware("http")
async def observability(request: Request, call_next):
    """Correlation id + duration on every request. Never logs query values."""
    rid = uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        ms = (time.perf_counter() - started) * 1000
        log.exception("rid=%s %s %s failed after %.0fms",
                      rid, request.method, request.url.path, ms)
        raise
    ms = (time.perf_counter() - started) * 1000
    response.headers["X-Request-Id"] = rid
    response.headers["X-Response-Time-Ms"] = f"{ms:.0f}"
    log.info("rid=%s %s %s -> %s %.0fms",
             rid, request.method, request.url.path, response.status_code, ms)
    return response


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    """Never return a driver message or a traceback to a client.

    A psycopg connection error contains the host and username. Log the detail
    server-side, return a type name.
    """
    log.exception("unhandled on %s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={
            "error": "analytics_unavailable",
            "detail": "The analytics service could not complete this request.",
            "error_type": type(exc).__name__,
        },
    )


app.include_router(health.router)
app.include_router(analytics.router)
app.include_router(revenue.router)
app.include_router(operations.router)
app.include_router(intelligence.router)
app.include_router(trust.router)


def _public_endpoints() -> list[str]:
    """Derive the index from the app's own routes.

    Previously a hand-maintained list, which drifted the moment a router was added:
    twelve live endpoints were missing from it. A generated index cannot go stale,
    and the existing test that GETs every advertised path now covers everything
    rather than only what someone remembered to type.
    """
    paths = {
        route.path
        for route in app.routes
        if getattr(route, "methods", None)
        and "GET" in route.methods
        and (route.path.startswith("/api") or route.path.startswith("/health"))
    }
    return sorted(paths)


@app.get("/", tags=["health"], summary="Service index")
def index() -> dict:
    return {
        "service": "StayPulse Analytics API",
        "version": app.version,
        "docs": "/docs",
        "data": "synthetic — see the repository README",
        "repository": "https://github.com/Mukund934/StayPulse-AI-hospitality-analytics",
        "endpoints": _public_endpoints(),
    }


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("api.app.main:app", host="0.0.0.0",
                port=int(os.environ.get("PORT", 8000)), reload=True)
