"""Liveness and readiness.

Split deliberately. Render pings /health constantly to decide whether the instance
is alive; if that endpoint touched the database, a slow pooler would get the service
restarted. /health is therefore pure and instant, and database reachability lives in
a separate readiness probe that a human calls.
"""
from __future__ import annotations

from fastapi import APIRouter

from api.app import services

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness — no I/O, always instant")
def health() -> dict:
    return {"status": "ok"}


@router.get("/health/readiness", summary="Readiness — verifies the database is reachable")
def readiness() -> dict:
    r = services.health_readiness()
    return {"status": "ok" if r.get("database") == "reachable" else "degraded", **r}
