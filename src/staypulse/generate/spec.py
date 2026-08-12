"""Generation parameters — the documented assumptions behind the synthetic data.

Everything the generator does is driven from this file, so the assumptions are
inspectable in one place rather than scattered through generation code. If a
number here is wrong, the dataset is wrong in a knowable way.

Business shape, from public research:
  - Serviced aparthotels in Bengaluru (BTM Layout, Koramangala, HSR), run from a
    back office ~1,200 km away in Raipur.
  - ~40 units total. No restaurant, no banquet, no meeting rooms.
  - Weekday-heavy corporate demand: the tech corridor, relocation and project
    stays, not weekend leisure.
  - Listed on OTAs and on an hourly-booking platform, so microstays share
    inventory with nightly stays.

NOTHING HERE IS REAL COMPANY DATA. These are plausible parameters chosen to make
the analytics meaningful, not observations of any business.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# Reproducibility. Every random draw in the generator derives from this.
RANDOM_SEED = 20260812

# 18 months ending the day before "today" in the project's frame of reference.
PERIOD_START = dt.date(2025, 2, 1)
PERIOD_END = dt.date(2026, 8, 11)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PropertySpec:
    code: str
    name: str
    area: str
    opened_on: dt.date
    unit_mix: dict[str, int]          # unit_type -> count
    demand_index: float               # multiplier on baseline occupancy
    ooo_rate: float                   # share of unit-nights out of order


PROPERTIES: list[PropertySpec] = [
    PropertySpec(
        code="BLR-BTM",
        name="StayPulse Residences BTM Layout",
        area="BTM Layout",
        opened_on=dt.date(2023, 6, 1),
        unit_mix={"Studio": 4, "1BHK": 6, "1BHK Large": 4, "2BHK": 2},
        demand_index=1.00,
        ooo_rate=0.018,
    ),
    PropertySpec(
        code="BLR-KOR",
        name="StayPulse Residences Koramangala",
        area="Koramangala",
        opened_on=dt.date(2024, 9, 1),
        unit_mix={"Studio": 3, "1BHK": 5, "1BHK Large": 3, "2BHK": 2, "2BHK Premium": 1},
        demand_index=1.12,             # closer to the startup belt, prices harder
        ooo_rate=0.022,
    ),
    PropertySpec(
        # Opens mid-series. A ramping property is a realistic reason for a
        # portfolio-level average to move without any existing property changing.
        code="BLR-HSR",
        name="StayPulse Residences HSR Layout",
        area="HSR Layout",
        opened_on=dt.date(2026, 3, 1),
        unit_mix={"Studio": 3, "1BHK": 4, "1BHK Large": 3},
        demand_index=0.78,
        ooo_rate=0.035,                # new build, snagging issues
    ),
]

# Pre-tax nightly list rates. 2BHK Premium sits deliberately just ABOVE the
# INR 7,500 GST threshold: at 7,599 the guest pays 18% GST and an out-the-door
# price of 8,966.82, where 7,499 would attract 5% and cost the guest 7,873.95.
# Giving up 100 of pre-tax revenue (-1.3%) cuts the guest's price by 1,092.87
# (-12.2%). That is a real pricing question the analytics should surface.
UNIT_TYPE_SPECS: dict[str, dict] = {
    "Studio":        {"bedrooms": 0, "max_occupancy": 2, "sqft": 320, "base_rate": 3599},
    "1BHK":          {"bedrooms": 1, "max_occupancy": 3, "sqft": 450, "base_rate": 4299},
    "1BHK Large":    {"bedrooms": 1, "max_occupancy": 4, "sqft": 530, "base_rate": 5199},
    "2BHK":          {"bedrooms": 2, "max_occupancy": 5, "sqft": 780, "base_rate": 6999},
    "2BHK Premium":  {"bedrooms": 2, "max_occupancy": 6, "sqft": 860, "base_rate": 7599},
}

GST_THRESHOLD_INR = 7500.0


# ---------------------------------------------------------------------------
# Channels. Commission is charged on the pre-tax room rate; GST is then charged
# on the commission itself, so gross-to-net is two steps, not one.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChannelSpec:
    code: str
    name: str
    type: str
    commission_pct: float
    settlement_days: int
    share: float                    # share of bookings
    mean_lead_days: float
    cancel_rate: float
    mean_los: float


CHANNELS: list[ChannelSpec] = [
    ChannelSpec("MMT",     "MakeMyTrip",   "ota",       20.0, 15, 0.20,  9.0, 0.170, 2.2),
    ChannelSpec("BDC",     "Booking.com",  "ota",       17.0,  9, 0.18, 12.0, 0.205, 2.4),
    ChannelSpec("AGODA",   "Agoda",        "ota",       18.0, 30, 0.08, 14.0, 0.190, 2.3),
    ChannelSpec("AIRBNB",  "Airbnb",       "ota",       15.0,  1, 0.07, 16.0, 0.120, 4.1),
    ChannelSpec("DIRECT",  "Direct",       "direct",     0.0,  2, 0.17,  6.0, 0.085, 3.0),
    ChannelSpec("CORP",    "Corporate",    "corporate",  0.0, 30, 0.19,  4.0, 0.055, 6.5),
    ChannelSpec("B2B-HR",  "Bag2Bag",      "hourly",    12.0,  7, 0.09,  0.4, 0.130, 0.0),
    ChannelSpec("WALKIN",  "Walk-in",      "walk_in",    0.0,  0, 0.02,  0.0, 0.030, 1.6),
]

# Indian booking windows are short: 7-21 days against a ~40-day global average.
# Lead time is drawn lognormal per channel around mean_lead_days.
LEAD_TIME_SIGMA = 0.85

# Hour-of-day weights for when a booking is placed, in IST. Bookings peak in the
# evening, dip in the small hours, and have a real late-night mobile tail --
# roughly 8% land between 00:00 and 05:59.
#
# That tail is load-bearing, not decoration. A booking at 02:00 IST is 20:30 UTC
# on the PREVIOUS day, so the late-night band is the only place where a UTC date
# and an IST business date can disagree. A tight evening-only distribution makes
# the band empty and the timezone defect becomes unobservable -- which is exactly
# the trap that a naive normal(19.5, 4.2) falls into.
BOOKING_HOUR_WEIGHTS_IST = [
    3.0, 2.0, 1.2, 0.8, 0.7, 1.0,   # 00-05  late-night mobile tail
    1.8, 2.8, 3.6, 4.5, 5.5, 6.0,   # 06-11  morning ramp
    5.6, 5.0, 5.4, 5.8, 6.2, 6.5,   # 12-17  afternoon
    7.0, 7.5, 7.2, 6.4, 5.0, 4.0,   # 18-23  evening peak
]


# ---------------------------------------------------------------------------
# Demand shape
# ---------------------------------------------------------------------------
BASE_OCCUPANCY = 0.72

# Mon..Sun. Corporate aparthotels fill midweek and empty at the weekend, which is
# the inverse of a leisure hotel. Getting this backwards is an instant tell.
DOW_MULTIPLIER = [1.14, 1.16, 1.15, 1.10, 0.92, 0.74, 0.79]

# Jan..Dec. Bengaluru has mild seasonality; the real dips are the summer lull and
# the festival fortnight when corporate travel stops.
MONTH_MULTIPLIER = {
    1: 1.02, 2: 1.05, 3: 1.04, 4: 0.93, 5: 0.88, 6: 0.96,
    7: 1.01, 8: 1.03, 9: 1.05, 10: 0.97, 11: 1.06, 12: 0.90,
}

# Festival windows suppress corporate demand rather than raising it.
FESTIVAL_WINDOWS: list[tuple[dt.date, dt.date, float, str]] = [
    (dt.date(2025, 10, 18), dt.date(2025, 10, 25), 0.62, "Diwali"),
    (dt.date(2026, 11,  6), dt.date(2026, 11, 13), 0.62, "Diwali"),
    (dt.date(2025, 12, 24), dt.date(2026,  1,  2), 0.70, "Year end"),
    (dt.date(2026,  3,  3), dt.date(2026,  3,  5), 0.80, "Holi"),
]

# Gentle growth over the period.
ANNUAL_GROWTH = 0.11


# ---------------------------------------------------------------------------
# Service operations
# ---------------------------------------------------------------------------
REQUEST_TYPES: list[dict] = [
    {"category": "Housekeeping", "subcategory": "Room not cleaned",   "priority": "P2", "sla_minutes": 60,  "team": "housekeeping"},
    {"category": "Housekeeping", "subcategory": "Extra towels/linen", "priority": "P3", "sla_minutes": 45,  "team": "housekeeping"},
    {"category": "Housekeeping", "subcategory": "Late checkout clean","priority": "P2", "sla_minutes": 90,  "team": "housekeeping"},
    {"category": "Maintenance",  "subcategory": "AC not cooling",     "priority": "P1", "sla_minutes": 120, "team": "maintenance"},
    {"category": "Maintenance",  "subcategory": "Hot water",          "priority": "P1", "sla_minutes": 90,  "team": "maintenance"},
    {"category": "Maintenance",  "subcategory": "Wi-Fi down",         "priority": "P1", "sla_minutes": 60,  "team": "maintenance"},
    {"category": "Maintenance",  "subcategory": "Plumbing",           "priority": "P2", "sla_minutes": 180, "team": "maintenance"},
    {"category": "Front Desk",   "subcategory": "Late check-in",      "priority": "P2", "sla_minutes": 30,  "team": "front_office"},
    {"category": "Front Desk",   "subcategory": "Key/access issue",   "priority": "P1", "sla_minutes": 30,  "team": "front_office"},
    {"category": "Billing",      "subcategory": "Incorrect folio",    "priority": "P2", "sla_minutes": 240, "team": "front_office"},
]

REQUESTS_PER_OCCUPIED_NIGHT = 0.14
CSAT_RESPONSE_RATE = 0.34
REVIEW_RATE = 0.22               # share of completed stays leaving a review

# ---------------------------------------------------------------------------
# Guest loyalty shape
# ---------------------------------------------------------------------------
# Repeat behaviour is long-tailed, not uniform: most guests stay once and a small
# cohort returns repeatedly. Drawing guests uniformly from a small pool produced a
# 61% repeat rate -- roughly triple what a serviced-apartment operator sees -- and
# repeat rate is a headline KPI, so an implausible value discredits the dashboard.
# GUEST_POOL is sized against expected completed stays so the modal guest appears
# once.
# The pool must be substantially LARGER than the number of stays, or the modal
# guest appears twice by arithmetic alone. Sizing: with ~4,400 completed stays and
# a 9,000-guest pool, the non-loyal arrival rate is ~0.34 stays/guest, so most
# guests who appear at all appear exactly once. The loyal cohort is small and
# heavily weighted, which reproduces the real shape -- a long single-stay tail
# plus a minority of frequent returners.
#
# The result lands near 25-30% repeat. That is higher than a transient city hotel
# and appropriate here: corporate project stays and relocation guests genuinely
# return to the same serviced apartment.
GUEST_POOL = 9000
LOYAL_GUEST_SHARE = 0.04     # share of the pool that books repeatedly
LOYAL_GUEST_WEIGHT = 12.0    # relative selection weight for that cohort


# ---------------------------------------------------------------------------
# Planted findings.
#
# These are patterns the ANALYTICS must discover; none of them are written into
# any dashboard as a constant. Each is designed to be invisible in the blended
# top line and visible only when segmented — which is the whole point.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PlantedFinding:
    key: str
    description: str
    detection_hint: str
    window: tuple[dt.date, dt.date]
    params: dict = field(default_factory=dict)


PLANTED_FINDINGS: list[PlantedFinding] = [
    PlantedFinding(
        key="F1_KOR_SLA_DEGRADATION",
        description=(
            "Koramangala housekeeping resolution time degrades after an evening-shift "
            "staffing change. Portfolio-wide SLA breach rate barely moves because BTM "
            "improves slightly over the same window."
        ),
        detection_hint=(
            "Segment SLA breach rate by property AND by hour-of-day. The signal is "
            "concentrated in the 18:00-23:00 IST block at BLR-KOR."
        ),
        window=(dt.date(2026, 3, 9), PERIOD_END),
        params={"property": "BLR-KOR", "team": "housekeeping",
                "evening_slowdown_factor": 2.35, "offsetting_btm_improvement": 0.88},
    ),
    PlantedFinding(
        key="F2_NIGHT_AUDIT_CUTOFF",
        description=(
            "For a nine-week window the booking feed stamps late-night reservations "
            "with the UTC date rather than the IST business date, moving 00:00-03:00 "
            "IST bookings to the previous day and manufacturing a phantom one-day "
            "revenue dip that repeats weekly."
        ),
        detection_hint=(
            "Compare COUNT(*) by meta.business_date(booked_at) against a naive "
            "booked_at::date. The gap is exactly the 18:30-24:00 UTC band."
        ),
        window=(dt.date(2026, 1, 12), dt.date(2026, 3, 15)),
        params={"affected_share": 1.0},
    ),
    PlantedFinding(
        key="F3_WHATSAPP_SILENT_GAP",
        description=(
            "The WhatsApp service-request integration stops writing for nine days. "
            "The table is not wrong, it is empty — so null checks pass and totals "
            "merely look seasonal."
        ),
        detection_hint=(
            "Row-count band and freshness check per source channel, not a null check. "
            "WhatsApp request volume goes to zero while phone and app volume rise."
        ),
        window=(dt.date(2025, 11, 14), dt.date(2025, 11, 22)),
        params={"channel": "whatsapp"},
    ),
]

# Decoy: a real, explainable movement that looks like a problem and is not.
# Included so that "found a pattern" is not automatically "found a fault".
DECOY = PlantedFinding(
    key="D1_CHANNEL_MIX_SHIFT",
    description=(
        "Portfolio ADR falls ~6% over two months. It is not a rate cut: corporate "
        "long-stay mix rises, and corporate books lower nightly rates for longer "
        "stays. Revenue per available unit is flat to up."
    ),
    detection_hint=(
        "Decompose the ADR change into rate effect and mix effect. The rate effect "
        "is near zero; the mix effect explains it. RevPAR does not fall."
    ),
    window=(dt.date(2026, 5, 1), dt.date(2026, 6, 30)),
    params={"corporate_share_uplift": 0.16},
)


# ---------------------------------------------------------------------------
# Seeded defects. Realistic operational mess, injected at known rates so the
# data-quality layer can be scored on recall per class rather than "it ran".
# ---------------------------------------------------------------------------
DEFECT_RATES: dict[str, float] = {
    "duplicate_booking":       0.011,   # OTA sync double-submits the same reservation
    "duplicate_guest":         0.040,   # same person, different email/phone formatting
    "payment_amount_mismatch": 0.024,   # gateway amount disagrees with folio
    "orphan_payment_ref":      0.009,   # payment references a booking id that never resolves
    "missing_contact":         0.061,   # no phone or no email on the guest record
    "impossible_stay_dates":   0.0025,  # checkout on or before checkin
    "invalid_rating":          0.004,   # rating outside 1-5 or null on a reviewed stay
    "inventory_balance_error": 0.009,   # physical count disagrees with the computed balance
}
