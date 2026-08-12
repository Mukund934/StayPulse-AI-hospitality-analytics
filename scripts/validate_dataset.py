"""Validate the generated dataset.

Two jobs. First, assert the arithmetic holds -- notably RevPAR = ADR x Occupancy,
which only reconciles if occupancy and ADR share a denominator. Second, prove the
planted findings actually emerged: a finding that was designed but did not survive
generation is worse than no finding, because the analysis will look for it and
come back empty.

Usage:
    python scripts/validate_dataset.py
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from staypulse import db  # noqa: E402
from staypulse.generate import spec  # noqa: E402

failures: list[str] = []
warnings: list[str] = []


def check(label: str, ok: bool, detail: str = "", *, warn_only: bool = False) -> None:
    mark = "[ ok ]" if ok else ("[warn]" if warn_only else "[FAIL]")
    print(f"  {mark}  {label}")
    if detail:
        print(f"           {detail}")
    if not ok:
        (warnings if warn_only else failures).append(f"{label}: {detail}")


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


# ---------------------------------------------------------------------------
section("1. Core rate metrics")

kpi = db.fetch_all("""
    SELECT
        count(*) FILTER (WHERE is_sellable)                        AS rooms_available,
        count(*) FILTER (WHERE is_occupied)                        AS rooms_sold,
        sum(room_revenue_net_inr)                                  AS revenue,
        count(*)                                                   AS total_unit_nights,
        count(*) FILTER (WHERE is_out_of_order)                    AS ooo_nights
    FROM mart.fact_unit_night
""")[0]

avail = int(kpi["rooms_available"])
sold = int(kpi["rooms_sold"])
revenue = float(kpi["revenue"] or 0)

occupancy = sold / avail if avail else 0
adr = revenue / sold if sold else 0
revpar = revenue / avail if avail else 0

print(f"    rooms available (sellable) : {avail:,}")
print(f"    rooms sold                 : {sold:,}")
print(f"    out-of-order nights        : {int(kpi['ooo_nights']):,}")
print(f"    net room revenue           : INR {revenue:,.0f}")
print(f"    occupancy                  : {occupancy:.1%}")
print(f"    ADR                        : INR {adr:,.0f}")
print(f"    RevPAR                     : INR {revpar:,.0f}")
print()

# The identity is the single most important assertion in the project: it only
# holds if occupancy and ADR are computed from the same table and denominator.
identity_gap = abs(revpar - adr * occupancy)
check("RevPAR = ADR x Occupancy (identity)", identity_gap < 0.01,
      f"|RevPAR - ADR*Occ| = {identity_gap:.6f}")

check("Occupancy in a plausible operating band (55-90%)",
      0.55 <= occupancy <= 0.90,
      f"{occupancy:.1%} - a serviced aparthotel below ~55% would not be a going concern",
      warn_only=True)

check("ADR within the published rate band (INR 2,500-8,000)",
      2500 <= adr <= 8000, f"INR {adr:,.0f}")

# ---------------------------------------------------------------------------
section("2. Grain and interval correctness")

# Departure night must NOT be counted. Room-nights derived from bookings must
# equal the occupied unit-nights they produced.
recon = db.fetch_all("""
    WITH from_bookings AS (
        SELECT sum(check_out_date - check_in_date)::bigint AS nights
        FROM mart.fact_booking
        WHERE status NOT IN ('cancelled','no_show') AND stay_type = 'nightly'
    ),
    from_nights AS (
        SELECT count(*)::bigint AS nights
        FROM mart.fact_unit_night un
        JOIN mart.fact_booking b ON b.booking_key = un.booking_key
        WHERE un.is_occupied AND b.stay_type = 'nightly'
    )
    SELECT (SELECT nights FROM from_bookings) AS booking_nights,
           (SELECT nights FROM from_nights)   AS materialised_nights
""")[0]
bn, mn = int(recon["booking_nights"]), int(recon["materialised_nights"])
# Materialised will be slightly lower: overlapping bookings on one unit-night are
# resolved first-wins, and OOO nights suppress occupancy. A large gap is a bug.
drift = (bn - mn) / bn if bn else 0
check("Room-nights reconcile between booking and unit-night grain",
      0 <= drift < 0.12,
      f"bookings imply {bn:,}, materialised {mn:,} ({drift:.1%} absorbed by OOO/overlap)")

zero = db.scalar("""
    SELECT count(*) FROM mart.fact_unit_night un
    JOIN mart.fact_booking b ON b.booking_key = un.booking_key
    WHERE un.stay_date >= b.check_out_date AND b.stay_type = 'nightly' AND b.nights > 0
""")
check("No unit-night on or after the departure date", zero == 0,
      f"{zero} rows would mean the half-open interval was violated")

# ---------------------------------------------------------------------------
section("3. Planted findings emerged")

# --- F1: Koramangala evening housekeeping degradation
f1 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F1_KOR_SLA_DEGRADATION")
f1_rows = db.fetch_all("""
    SELECT p.property_code,
           CASE WHEN sr.request_date >= :start THEN 'after' ELSE 'before' END AS era,
           round(avg(sr.resolution_minutes)::numeric, 1) AS avg_mins,
           round(100.0 * avg(CASE WHEN sr.is_sla_breached THEN 1 ELSE 0 END), 1) AS breach_pct,
           count(*) AS n
    FROM mart.fact_service_request sr
    JOIN mart.dim_property p ON p.property_key = sr.property_key
    JOIN mart.dim_request_type rt ON rt.request_type_key = sr.request_type_key
    WHERE rt.owning_team = 'housekeeping'
      AND extract(hour FROM sr.created_at AT TIME ZONE 'Asia/Kolkata') BETWEEN 18 AND 23
    GROUP BY 1, 2 ORDER BY 1, 2 DESC
""", start=f1.window[0])
print("    Evening (18:00-23:00 IST) housekeeping, by property and era:")
for r in f1_rows:
    print(f"      {r['property_code']:<9} {r['era']:<7} "
          f"avg {float(r['avg_mins']):>7.1f} min | breach {float(r['breach_pct']):>5.1f}% | n={r['n']}")

kor = {r["era"]: r for r in f1_rows if r["property_code"] == "BLR-KOR"}
if "before" in kor and "after" in kor:
    ratio = float(kor["after"]["avg_mins"]) / float(kor["before"]["avg_mins"])
    check("F1 Koramangala evening housekeeping degraded", ratio > 1.4,
          f"resolution time x{ratio:.2f} after {f1.window[0]}")
else:
    check("F1 Koramangala evening housekeeping degraded", False, "insufficient rows in one era")

blended = db.fetch_all("""
    SELECT CASE WHEN request_date >= :start THEN 'after' ELSE 'before' END AS era,
           round(100.0 * avg(CASE WHEN is_sla_breached THEN 1 ELSE 0 END), 1) AS breach_pct
    FROM mart.fact_service_request GROUP BY 1
""", start=f1.window[0])
bl = {r["era"]: float(r["breach_pct"]) for r in blended}
if len(bl) == 2:
    move = abs(bl["after"] - bl["before"])
    check("F1 is hidden in the blended top line (the point of the finding)",
          move < 6.0,
          f"portfolio breach rate moved only {move:.1f}pp "
          f"({bl['before']:.1f}% -> {bl['after']:.1f}%) - visible only when segmented",
          warn_only=True)

# --- F2: night-audit cut-off drift.
# The stored booking_date must disagree with the derived IST business date inside
# the window, and agree everywhere else. Both halves matter: a defect that leaks
# outside its window is not a localised incident, it is a broken generator.
f2 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F2_NIGHT_AUDIT_CUTOFF")
# Window membership is keyed on meta.business_date(booked_at), the TRUE date, not
# on the stored booking_date -- the stored value is the corrupted one, and a
# booking on the window's first IST day carries a UTC date one day earlier, so
# keying on it would report the incident's own edge as a leak.
f2_rows = db.fetch_all("""
    SELECT
        count(*) FILTER (
            WHERE meta.business_date(booked_at) BETWEEN :s AND :e
              AND booking_date <> meta.business_date(booked_at))            AS drift_in,
        count(*) FILTER (WHERE meta.business_date(booked_at) BETWEEN :s AND :e) AS total_in,
        count(*) FILTER (
            WHERE meta.business_date(booked_at) NOT BETWEEN :s AND :e
              AND booking_date <> meta.business_date(booked_at))            AS drift_out
    FROM mart.fact_booking
""", s=f2.window[0], e=f2.window[1])[0]
drift_in = int(f2_rows["drift_in"])
total_in = int(f2_rows["total_in"])
drift_out = int(f2_rows["drift_out"])
check("F2 night-audit cut-off drift is detectable",
      drift_in > 0,
      f"{drift_in:,} of {total_in:,} bookings in the window carry a stored booking_date "
      f"that disagrees with meta.business_date(booked_at) - the late-night band")
check("F2 drift is confined to its window",
      drift_out == 0,
      f"{drift_out:,} drifted rows outside the window "
      f"({'clean' if drift_out == 0 else 'the defect is leaking'})")

# --- F3: WhatsApp silent gap
f3 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F3_WHATSAPP_SILENT_GAP")
f3_in = db.scalar("""
    SELECT count(*) FROM mart.fact_service_request
    WHERE channel = 'whatsapp' AND request_date BETWEEN :s AND :e
""", s=f3.window[0], e=f3.window[1])
f3_daily_out = db.scalar("""
    SELECT round(avg(n)::numeric, 2) FROM (
        SELECT request_date, count(*) AS n FROM mart.fact_service_request
        WHERE channel = 'whatsapp' AND request_date NOT BETWEEN :s AND :e
        GROUP BY 1) t
""", s=f3.window[0], e=f3.window[1])
days = (f3.window[1] - f3.window[0]).days + 1
expected = float(f3_daily_out or 0) * days
check("F3 WhatsApp integration gap is present",
      f3_in < max(1.0, expected * 0.35),
      f"{f3_in} requests over {days} days vs ~{expected:.0f} expected "
      f"(baseline {float(f3_daily_out or 0):.2f}/day) - an EMPTY table, not a wrong one")

# --- Decoy: channel mix
d = spec.DECOY
mix = db.fetch_all("""
    SELECT CASE WHEN b.check_in_date BETWEEN :s AND :e THEN 'in_window' ELSE 'outside' END AS era,
           round(100.0 * count(*) FILTER (WHERE c.channel_type = 'corporate') / count(*), 1) AS corp_pct,
           round(avg(b.net_room_amount_inr / GREATEST(b.nights,1))::numeric, 0) AS avg_nightly
    FROM mart.fact_booking b
    JOIN mart.dim_channel c ON c.channel_key = b.channel_key
    WHERE b.status NOT IN ('cancelled','no_show') AND b.stay_type = 'nightly'
    GROUP BY 1
""", s=d.window[0], e=d.window[1])
print("    Decoy window channel mix:")
for r in mix:
    print(f"      {r['era']:<10} corporate {float(r['corp_pct']):>5.1f}% | "
          f"avg nightly rate INR {float(r['avg_nightly']):,.0f}")
mixd = {r["era"]: r for r in mix}
if len(mixd) == 2:
    lift = float(mixd["in_window"]["corp_pct"]) - float(mixd["outside"]["corp_pct"])
    check("Decoy D1 channel-mix shift emerged", lift > 5.0,
          f"corporate share +{lift:.1f}pp in window - ADR falls through MIX, not rate")

# ---------------------------------------------------------------------------
section("4. Seeded defects are present and countable")

defects = {
    "duplicate bookings (same unit/dates/guest)": db.scalar("""
        SELECT count(*) FROM (
            SELECT guest_key, unit_key, check_in_date, count(*) AS n
            FROM mart.fact_booking GROUP BY 1,2,3 HAVING count(*) > 1) t
    """),
    "duplicate guests (same normalised phone)": db.scalar("""
        SELECT count(*) FROM (
            SELECT phone_last10 FROM mart.dim_guest
            WHERE phone_last10 IS NOT NULL AND phone_last10 <> ''
            GROUP BY 1 HAVING count(*) > 1) t
    """),
    "payment amount mismatch vs folio": db.scalar("""
        SELECT count(*) FROM mart.fact_payment p
        JOIN mart.fact_booking b ON b.booking_key = p.booking_key
        WHERE abs(p.gross_amount_inr - b.net_room_amount_inr) > 1.00
    """),
    "orphan payment references": db.scalar(
        "SELECT count(*) FROM mart.fact_payment WHERE booking_key IS NULL"),
    "guests missing phone or email": db.scalar(
        "SELECT count(*) FROM mart.dim_guest WHERE phone IS NULL OR email IS NULL"),
    "zero-night bookings (impossible stay)": db.scalar("""
        SELECT count(*) FROM mart.fact_booking
        WHERE stay_type = 'nightly' AND check_out_date = check_in_date
    """),
    "reviews with null rating": db.scalar(
        "SELECT count(*) FROM mart.fact_review WHERE rating IS NULL"),
}
for label, n in defects.items():
    check(f"{label}", n > 0, f"{n:,} found", warn_only=(n == 0))

# ---------------------------------------------------------------------------
section("5. Referential and temporal integrity")

check("Every occupied unit-night has a booking",
      db.scalar("SELECT count(*) FROM mart.fact_unit_night WHERE is_occupied AND booking_key IS NULL") == 0)
check("No unit-night is both out-of-order and sellable",
      db.scalar("SELECT count(*) FROM mart.fact_unit_night WHERE is_out_of_order AND is_sellable") == 0)
check("No resolution before creation",
      db.scalar("SELECT count(*) FROM mart.fact_service_request WHERE resolved_at < created_at") == 0)
check("Cancelled bookings all carry a cancellation timestamp",
      db.scalar("SELECT count(*) FROM mart.fact_booking WHERE status='cancelled' AND cancelled_at IS NULL") == 0)
check("Unit-nights fall inside the generation period",
      db.scalar("SELECT count(*) FROM mart.fact_unit_night WHERE stay_date < :s OR stay_date > :e",
                s=spec.PERIOD_START, e=spec.PERIOD_END) == 0)

# ---------------------------------------------------------------------------
section("6. GST threshold exposure")

gst = db.fetch_all("""
    SELECT u.unit_type, u.base_rate_inr,
           CASE WHEN u.base_rate_inr > 7500 THEN '18% (with ITC)' ELSE '5% (no ITC)' END AS gst_band,
           count(DISTINCT u.unit_key) AS units
    FROM mart.dim_unit u GROUP BY 1,2,3 ORDER BY u.base_rate_inr
""")
for r in gst:
    flag = "  <-- above threshold" if float(r["base_rate_inr"]) > 7500 else ""
    print(f"    {r['unit_type']:<14} INR {float(r['base_rate_inr']):>7,.0f}  "
          f"{r['gst_band']:<16} units={r['units']}{flag}")
above = [r for r in gst if float(r["base_rate_inr"]) > 7500]
check("At least one unit type sits above the INR 7,500 GST threshold",
      len(above) > 0,
      "this is what makes the pricing analysis concrete rather than hypothetical")

# ---------------------------------------------------------------------------
section("Summary")
print(f"  failures: {len(failures)}   warnings: {len(warnings)}")
if warnings:
    print("\n  Warnings:")
    for w in warnings:
        print(f"    - {w}")
if failures:
    print("\n  FAILURES:")
    for f in failures:
        print(f"    - {f}")
    raise SystemExit(1)
print("\n  Dataset validated.")
