"""Service requests, SLA and CSAT."""
from __future__ import annotations

from fastapi import APIRouter

from api.app import services

router = APIRouter(prefix="/api/operations", tags=["operations"])


@router.get("/overview", summary="Portfolio service performance")
def overview() -> dict:
    return services.operations_overview()


@router.get("/sla", summary="SLA breach rate by property and time of day")
def sla_matrix() -> dict:
    return {
        "note": ("segmented by property AND day-part because either dimension alone "
                 "flattens the signal — a 0.9pp portfolio move concealed a 2.5x "
                 "degradation in one shift at one property. Hours bucketed on IST."),
        "segments": services.operations_sla_matrix(),
    }


@router.get("/service-requests", summary="Volume and turnaround by request category")
def service_requests() -> dict:
    return {"categories": services.service_requests_by_category()}
