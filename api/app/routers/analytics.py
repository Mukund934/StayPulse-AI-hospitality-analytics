"""Revenue, occupancy, rate and property performance."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Path, Query

from api.app import services

router = APIRouter(prefix="/api", tags=["analytics"])


@router.get("/kpis/overview", summary="Portfolio KPIs with a like-for-like comparison")
def kpis_overview(
    days: int | None = Query(
        None, ge=1, le=365,
        description="Trailing window in days. The comparison period is the "
                    "immediately preceding window of the SAME length, so a 30-day "
                    "period is never compared against a 31-day one."),
) -> dict:
    return services.kpi_overview(days)


@router.get("/revenue/trends", summary="Revenue, occupancy, ADR and RevPAR over time")
def revenue_trends(
    grain: str = Query("month", pattern="^(month|day)$",
                       description="month or day"),
) -> dict:
    return {"grain": grain, "date_basis": "stay_date",
            "series": services.revenue_trend(grain)}


@router.get("/revenue/channels",
            summary="Channel economics, net of commission and GST on commission")
def revenue_channels() -> dict:
    return {
        "note": ("commission is charged on the pre-tax room rate, then 18% GST is "
                 "charged on the commission itself — gross-to-net is two steps, so "
                 "ranking channels on gross revenue flatters the OTAs"),
        "channels": services.revenue_channels(),
    }


@router.get("/properties", summary="Properties in the portfolio")
def list_properties() -> dict:
    return {"properties": services.properties()}


@router.get("/properties/{property_key}/performance",
            summary="Revenue and operational performance for one property")
def property_performance(
    property_key: int = Path(..., ge=1, description="Surrogate key from /api/properties"),
) -> dict:
    result = services.property_performance(property_key)
    if result is None:
        raise HTTPException(status_code=404, detail="Property not found")
    return result
