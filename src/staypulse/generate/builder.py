"""Seeded synthetic hospitality data generator.

Produces a dataset whose *shape* is defensible: arrivals follow a demand curve
built from weekday, month and festival effects rather than uniform randomness;
cancellation probability depends on lead time and channel; rate varies with
demand and day of week. Three business findings and one decoy are planted so the
analytics has something real to discover, and seven classes of operational defect
are injected at known rates so the data-quality layer can be scored.

All parameters live in `spec.py`. Same seed in, same dataset out.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from staypulse.generate import spec

# Deterministic name pools. Faker is seeded too, but a fixed pool keeps guest
# identity stable across runs so duplicate-detection results are reproducible.
_FIRST = [
    "Aarav", "Vivaan", "Aditya", "Arjun", "Rohan", "Karthik", "Siddharth", "Rahul",
    "Ananya", "Diya", "Ishita", "Kavya", "Meera", "Priya", "Sneha", "Divya",
    "Rajesh", "Suresh", "Manoj", "Anil", "Deepak", "Vikram", "Sanjay", "Nikhil",
    "Pooja", "Neha", "Shruti", "Anjali", "Lakshmi", "Ritu", "Swati", "Nandini",
]
_LAST = [
    "Sharma", "Verma", "Reddy", "Nair", "Iyer", "Menon", "Rao", "Kulkarni",
    "Desai", "Joshi", "Patel", "Shetty", "Gowda", "Pillai", "Bhat", "Kamath",
    "Chatterjee", "Banerjee", "Mukherjee", "Das", "Ghosh", "Sinha", "Mishra", "Tiwari",
]
_CITIES = [
    "Mumbai", "Delhi", "Hyderabad", "Chennai", "Pune", "Kolkata", "Ahmedabad",
    "Jaipur", "Kochi", "Raipur", "Indore", "Lucknow", "Coimbatore", "Nagpur",
]


def _festival_multiplier(day: dt.date) -> float:
    for start, end, mult, _name in spec.FESTIVAL_WINDOWS:
        if start <= day <= end:
            return mult
    return 1.0


def _demand_multiplier(day: dt.date, property_index: float) -> float:
    """Combined demand shape for a single night at a single property."""
    dow = spec.DOW_MULTIPLIER[day.weekday()]
    month = spec.MONTH_MULTIPLIER[day.month]
    festival = _festival_multiplier(day)
    elapsed_years = (day - spec.PERIOD_START).days / 365.25
    growth = (1.0 + spec.ANNUAL_GROWTH) ** elapsed_years
    return dow * month * festival * growth * property_index


@dataclass
class GeneratedData:
    properties: pd.DataFrame
    units: pd.DataFrame
    channels: pd.DataFrame
    request_types: pd.DataFrame
    staff: pd.DataFrame
    guests: pd.DataFrame
    bookings: pd.DataFrame
    unit_nights: pd.DataFrame
    payments: pd.DataFrame
    service_requests: pd.DataFrame
    reviews: pd.DataFrame
    inventory: pd.DataFrame

    def summary(self) -> dict[str, int]:
        return {
            name: len(getattr(self, name))
            for name in (
                "properties", "units", "channels", "request_types", "staff",
                "guests", "bookings", "unit_nights", "payments",
                "service_requests", "reviews", "inventory",
            )
        }


class Generator:
    def __init__(self, seed: int = spec.RANDOM_SEED) -> None:
        self.rng = np.random.default_rng(seed)
        self.seed = seed
        # Arrivals refused because no unit was free for the whole span. Reported
        # rather than hidden: denied demand is a genuine revenue-management signal.
        self.denied_demand = 0

    # -- dimensions ---------------------------------------------------------
    def build_properties(self) -> pd.DataFrame:
        rows = [
            {
                "property_code": p.code,
                "property_name": p.name,
                "area": p.area,
                "city": "Bengaluru",
                "state": "Karnataka",
                "unit_count": sum(p.unit_mix.values()),
                "opened_on": p.opened_on,
                "closed_on": None,
                "is_active": True,
                "has_restaurant": False,
            }
            for p in spec.PROPERTIES
        ]
        return pd.DataFrame(rows)

    def build_units(self) -> pd.DataFrame:
        rows = []
        for p in spec.PROPERTIES:
            n = 0
            for unit_type, count in p.unit_mix.items():
                meta = spec.UNIT_TYPE_SPECS[unit_type]
                for _ in range(count):
                    n += 1
                    rows.append({
                        "unit_code": f"{p.code}-{n:03d}",
                        "property_code": p.code,
                        "unit_type": unit_type,
                        "bedrooms": meta["bedrooms"],
                        "max_occupancy": meta["max_occupancy"],
                        "sqft": meta["sqft"],
                        "floor": (n % 5) + 1,
                        "base_rate_inr": float(meta["base_rate"]),
                        "is_sellable": True,
                    })
        return pd.DataFrame(rows)

    def build_channels(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "channel_code": c.code,
            "channel_name": c.name,
            "channel_type": c.type,
            "commission_pct": c.commission_pct,
            "settlement_days": c.settlement_days,
            "is_active": True,
        } for c in spec.CHANNELS])

    def build_request_types(self) -> pd.DataFrame:
        return pd.DataFrame([{
            "category": r["category"],
            "subcategory": r["subcategory"],
            "default_priority": r["priority"],
            "sla_minutes": r["sla_minutes"],
            "owning_team": r["team"],
        } for r in spec.REQUEST_TYPES])

    def build_staff(self) -> pd.DataFrame:
        rows = []
        idx = 0
        for p in spec.PROPERTIES:
            for role, count in (("housekeeping", 4), ("maintenance", 2),
                                ("front_office", 3), ("ops_manager", 1)):
                for _ in range(count):
                    idx += 1
                    shift = self.rng.choice(["morning", "evening", "night"], p=[0.45, 0.40, 0.15])
                    rows.append({
                        "staff_id": f"EMP{idx:04d}",
                        "full_name": f"{self.rng.choice(_FIRST)} {self.rng.choice(_LAST)}",
                        "role": role,
                        "property_code": p.code,
                        "shift": str(shift),
                        "joined_on": p.opened_on + dt.timedelta(days=int(self.rng.integers(0, 200))),
                        "left_on": None,
                        "is_active": True,
                    })
        return pd.DataFrame(rows)

    # -- guests -------------------------------------------------------------
    def build_guests(self, n: int) -> pd.DataFrame:
        rows = []
        for i in range(1, n + 1):
            first = str(self.rng.choice(_FIRST))
            last = str(self.rng.choice(_LAST))
            name = f"{first} {last}"
            # Indian mobile numbers are exactly 10 digits. This matters: the
            # duplicate-guest defect writes the same number as "+91XXXXXXXXXX"
            # and "0XXXXXXXXXX", and identity resolution matches on the last 10
            # digits. Generate 9 digits and the two variants normalise to
            # DIFFERENT keys, so the planted duplicates become undetectable.
            digits = int(self.rng.integers(6000000000, 9999999999))
            phone = f"+91{digits}"
            email = f"{first.lower()}.{last.lower()}{i}@example.com"
            rows.append({
                "guest_id": f"G{i:06d}",
                "full_name": name,
                "email": email,
                "phone": phone,
                "home_city": str(self.rng.choice(_CITIES)),
                "guest_segment": str(self.rng.choice(
                    ["corporate", "leisure", "relocation", "unknown"],
                    p=[0.52, 0.26, 0.16, 0.06])),
            })
        df = pd.DataFrame(rows)

        # DEFECT: duplicate guests. The same person re-books and the source system
        # creates a second profile because the email is cased differently and the
        # phone is written without the country code. Repeat rate is wrong until
        # these are resolved -- which is itself the finding.
        n_dupes = int(len(df) * spec.DEFECT_RATES["duplicate_guest"])
        dupe_idx = self.rng.choice(len(df), size=n_dupes, replace=False)
        dupes = df.iloc[dupe_idx].copy()
        dupes["guest_id"] = [f"G9{i:05d}" for i in range(len(dupes))]
        dupes["email"] = dupes["email"].str.upper()
        dupes["phone"] = dupes["phone"].str.replace("+91", "0", regex=False)
        dupes["full_name"] = dupes["full_name"].str.upper()
        df = pd.concat([df, dupes], ignore_index=True)

        # DEFECT: missing contact details.
        n_missing = int(len(df) * spec.DEFECT_RATES["missing_contact"])
        miss_idx = self.rng.choice(len(df), size=n_missing, replace=False)
        for j, ix in enumerate(miss_idx):
            if j % 2 == 0:
                df.iat[ix, df.columns.get_loc("phone")] = None
            else:
                df.iat[ix, df.columns.get_loc("email")] = None
        return df

    # -- bookings -----------------------------------------------------------
    def build_bookings(self, units: pd.DataFrame, guests: pd.DataFrame) -> pd.DataFrame:
        channels = spec.CHANNELS
        ch_codes = [c.code for c in channels]
        ch_by_code = {c.code: c for c in channels}
        base_shares = np.array([c.share for c in channels], dtype=float)

        prop_index = {p.code: p.demand_index for p in spec.PROPERTIES}
        prop_open = {p.code: p.opened_on for p in spec.PROPERTIES}
        units_by_prop = {code: g for code, g in units.groupby("property_code")}

        # Inventory allocation state. A real PMS assigns a reservation to a unit
        # that is actually free for the whole stay; picking a unit at random
        # produces overlapping bookings whose nights then collide in
        # fact_unit_night, silently discarding a third of all room-nights.
        # unit_code -> set of dates already committed.
        busy: dict[str, set[dt.date]] = {c: set() for c in units["unit_code"]}
        unit_codes_by_prop = {
            code: g["unit_code"].tolist() for code, g in units.groupby("property_code")
        }
        unit_row = {r.unit_code: r for r in units.itertuples(index=False)}

        # Portfolio-average length of stay, share-weighted. Microstays count as
        # one night of inventory even though they bill as zero nights.
        avg_los = sum(c.share * max(c.mean_los, 1.0) for c in channels)

        # Generate more arrivals than target occupancy implies, because roughly a
        # sixth cancel and some arrivals find no free unit. Allocation refuses
        # what will not fit, so this self-regulates at capacity rather than
        # overshooting -- a refused arrival is denied demand, which is real.
        arrival_scale = 1.32

        hour_weights = np.array(spec.BOOKING_HOUR_WEIGHTS_IST, dtype=float)
        hour_weights = hour_weights / hour_weights.sum()

        guest_ids = guests["guest_id"].tolist()
        rows = []
        denied = 0
        seq = 0
        day = spec.PERIOD_START
        while day <= spec.PERIOD_END:
            for p in spec.PROPERTIES:
                if day < prop_open[p.code]:
                    continue
                pu = units_by_prop[p.code]
                capacity = len(pu)
                # Ramp a newly opened property rather than switching it on at full tilt.
                days_open = (day - prop_open[p.code]).days
                ramp = min(1.0, 0.35 + 0.65 * days_open / 120.0)

                target_occ = spec.BASE_OCCUPANCY * _demand_multiplier(day, prop_index[p.code]) * ramp
                target_occ = float(np.clip(target_occ, 0.05, 0.97))

                # DECOY: corporate mix rises in the decoy window, which lowers ADR
                # through mix, not through any rate decision.
                shares = base_shares.copy()
                d_start, d_end = spec.DECOY.window
                if d_start <= day <= d_end:
                    corp_i = ch_codes.index("CORP")
                    uplift = spec.DECOY.params["corporate_share_uplift"]
                    shares[corp_i] += uplift
                    others = [i for i in range(len(shares)) if i != corp_i]
                    shares[others] -= uplift * shares[others] / shares[others].sum()
                shares = np.clip(shares, 0.001, None)
                shares = shares / shares.sum()

                n_arrivals = self.rng.poisson(capacity * target_occ * arrival_scale / avg_los)
                for _ in range(int(n_arrivals)):
                    ch = ch_by_code[str(self.rng.choice(ch_codes, p=shares))]

                    if ch.type == "hourly":
                        stay_type, nights = "microstay", 0
                    else:
                        stay_type = "nightly"
                        nights = max(1, int(self.rng.geometric(1.0 / max(ch.mean_los, 1.05))))
                        nights = min(nights, 21)

                    check_in = day
                    check_out = day + dt.timedelta(days=nights)

                    # Nights this reservation would physically hold. A microstay
                    # holds the arrival date only.
                    span = [check_in + dt.timedelta(days=i) for i in range(max(nights, 1))]

                    lead = float(self.rng.lognormal(math.log(max(ch.mean_lead_days, 0.5)),
                                                    spec.LEAD_TIME_SIGMA))
                    lead_days = int(np.clip(round(lead), 0, 120))
                    booked_date = check_in - dt.timedelta(days=lead_days)

                    # Booking hour drawn from an explicit IST hour-of-day curve.
                    # The 00:00-05:59 tail is what makes the timezone defect
                    # observable at all -- see spec.BOOKING_HOUR_WEIGHTS_IST.
                    hour = int(self.rng.choice(24, p=hour_weights))
                    minute = int(self.rng.integers(0, 60))
                    booked_ist = dt.datetime.combine(booked_date, dt.time(hour, minute))
                    booked_at = booked_ist - dt.timedelta(hours=5, minutes=30)  # -> UTC

                    # Cancellation is decided BEFORE allocation. A cancelled
                    # reservation never holds inventory, so it must not consume a
                    # unit that a live booking could have taken.
                    lead_factor = 1.0 + 0.55 * math.tanh((lead_days - 10) / 14.0)
                    p_cancel = float(np.clip(ch.cancel_rate * lead_factor, 0.01, 0.62))
                    cancelled = bool(self.rng.random() < p_cancel)

                    # Assign a unit that is genuinely free for the whole stay,
                    # the way a PMS does. Random assignment produces overlapping
                    # reservations whose nights then collide on the unique
                    # (unit, stay_date) grain and get silently discarded.
                    candidates = unit_codes_by_prop[p.code]
                    if cancelled:
                        unit_code = candidates[int(self.rng.integers(0, len(candidates)))]
                    else:
                        unit_code = None
                        for cand in self.rng.permutation(candidates):
                            if busy[str(cand)].isdisjoint(span):
                                unit_code = str(cand)
                                break
                        if unit_code is None:
                            # Sold out for this span. Denied demand is real and is
                            # counted rather than quietly forced into inventory.
                            denied += 1
                            continue
                        busy[unit_code].update(span)
                    unit = unit_row[unit_code]

                    seq += 1

                    # Rate: base, adjusted for demand and weekend softness.
                    demand_factor = 0.88 + 0.30 * (target_occ - 0.5)
                    rate = float(unit.base_rate_inr) * demand_factor
                    rate *= float(self.rng.normal(1.0, 0.045))
                    if stay_type == "microstay":
                        rate = rate * 0.28
                    rate = round(max(rate, 700.0), 2)

                    billable_nights = nights if nights > 0 else 1
                    gross = round(rate * billable_nights, 2)
                    discount = round(gross * float(self.rng.choice(
                        [0.0, 0.05, 0.10, 0.15], p=[0.66, 0.18, 0.11, 0.05])), 2)
                    net_room = round(gross - discount, 2)

                    cancelled_at = None
                    status = "checked_out" if check_out <= spec.PERIOD_END else "confirmed"
                    if cancelled:
                        status = "cancelled"
                        gap = max(1, lead_days)
                        cancel_offset = int(self.rng.integers(0, gap + 1))
                        cancelled_ist = dt.datetime.combine(
                            check_in - dt.timedelta(days=cancel_offset),
                            dt.time(int(self.rng.integers(8, 23)), int(self.rng.integers(0, 60))))
                        cancelled_at = cancelled_ist - dt.timedelta(hours=5, minutes=30)
                    elif self.rng.random() < 0.014:
                        status = "no_show"

                    commission = round(net_room * ch.commission_pct / 100.0, 2)

                    rows.append({
                        "booking_id": f"BK{seq:07d}",
                        "guest_id": str(self.rng.choice(guest_ids)),
                        "property_code": p.code,
                        "unit_code": unit.unit_code,
                        "channel_code": ch.code,
                        "booked_at": booked_at,
                        "check_in_date": check_in,
                        "check_out_date": check_out,
                        "cancelled_at": cancelled_at,
                        "stay_type": stay_type,
                        "nights": nights,
                        "adults": int(self.rng.integers(1, min(int(unit.max_occupancy), 4) + 1)),
                        "status": status,
                        "gross_amount_inr": gross,
                        "discount_inr": discount,
                        "net_room_amount_inr": net_room,
                        "commission_inr": commission,
                        "lead_time_days": lead_days,
                        "unit_base_rate": float(unit.base_rate_inr),
                        "nightly_rate": rate,
                    })
            day += dt.timedelta(days=1)

        self.denied_demand = denied
        df = pd.DataFrame(rows)

        # The correct business date for every booking: the IST calendar day the
        # reservation was actually made.
        df["booking_date"] = df["booked_at"].apply(
            lambda t: (t + dt.timedelta(hours=5, minutes=30)).date())

        # PLANTED F2: night-audit cut-off drift.
        #
        # The realistic failure is not a corrupted timestamp -- it is a feed that
        # writes the UTC calendar date into the reported business-date column. The
        # event time stays correct, so nothing looks broken; only the reporting
        # date is wrong, and only for bookings taken after 18:30 UTC (00:00 IST),
        # which is precisely the late-night traffic. The result is a phantom
        # one-day revenue dip that repeats on a weekly rhythm.
        #
        # Detection is therefore a comparison between the STORED booking_date and
        # meta.business_date(booked_at) -- a stored column disagreeing with the
        # derived truth.
        f2 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F2_NIGHT_AUDIT_CUTOFF")
        w_start, w_end = f2.window
        in_window = (df["booking_date"] >= w_start) & (df["booking_date"] <= w_end)
        df.loc[in_window, "booking_date"] = df.loc[in_window, "booked_at"].dt.date

        # DEFECT: duplicate bookings from an OTA sync double-submit.
        n_dupe = int(len(df) * spec.DEFECT_RATES["duplicate_booking"])
        idx = self.rng.choice(len(df), size=n_dupe, replace=False)
        dupes = df.iloc[idx].copy()
        dupes["booking_id"] = [f"BK9{i:06d}" for i in range(len(dupes))]
        dupes["booked_at"] = dupes["booked_at"] + pd.Timedelta(seconds=int(self.rng.integers(3, 90)))
        df = pd.concat([df, dupes], ignore_index=True)

        # DEFECT: impossible stay dates (checkout on or before checkin).
        n_bad = max(1, int(len(df) * spec.DEFECT_RATES["impossible_stay_dates"]))
        bad_idx = self.rng.choice(len(df), size=n_bad, replace=False)
        df.loc[df.index[bad_idx], "check_out_date"] = df.loc[df.index[bad_idx], "check_in_date"]
        df.loc[df.index[bad_idx], "nights"] = 0

        # booking_date is set above, before the F2 injection, and must NOT be
        # recomputed here -- doing so would overwrite the planted defect.
        df["cancel_date"] = df["cancelled_at"].apply(
            lambda t: (t + dt.timedelta(hours=5, minutes=30)).date() if pd.notna(t) else None)
        return df

    # -- unit nights --------------------------------------------------------
    def build_unit_nights(self, bookings: pd.DataFrame, units: pd.DataFrame) -> pd.DataFrame:
        """One row per sellable unit per night across the whole period.

        The half-open interval is applied exactly here: range(check_in, check_out)
        excludes the departure night. Nowhere else in the project re-derives it.
        """
        prop_open = {p.code: p.opened_on for p in spec.PROPERTIES}
        ooo_rate = {p.code: p.ooo_rate for p in spec.PROPERTIES}

        # Occupancy map from confirmed (non-cancelled) nightly stays.
        occupied: dict[tuple[str, dt.date], dict] = {}
        live = bookings[~bookings["status"].isin(["cancelled", "no_show"])]
        for row in live.itertuples(index=False):
            if row.stay_type == "microstay":
                nights_iter = [row.check_in_date]
            else:
                span = (row.check_out_date - row.check_in_date).days
                nights_iter = [row.check_in_date + dt.timedelta(days=i) for i in range(span)]
            if not nights_iter:
                continue
            per_night = round(row.net_room_amount_inr / len(nights_iter), 2)
            per_night_comm = round(row.commission_inr / len(nights_iter), 2)
            for night in nights_iter:
                key = (row.unit_code, night)
                if key in occupied:
                    continue  # first booking wins; the double-book is a DQ finding
                occupied[key] = {
                    "booking_id": row.booking_id,
                    "channel_code": row.channel_code,
                    "revenue": per_night,
                    "commission": per_night_comm,
                }

        rows = []
        total_days = (spec.PERIOD_END - spec.PERIOD_START).days + 1
        for unit in units.itertuples(index=False):
            opened = prop_open[unit.property_code]
            rate_ooo = ooo_rate[unit.property_code]
            for i in range(total_days):
                night = spec.PERIOD_START + dt.timedelta(days=i)
                if night < opened:
                    continue
                is_ooo = bool(self.rng.random() < rate_ooo)
                occ = occupied.get((unit.unit_code, night))
                rows.append({
                    "unit_code": unit.unit_code,
                    "property_code": unit.property_code,
                    "stay_date": night,
                    "date_key": int(night.strftime("%Y%m%d")),
                    "booking_id": occ["booking_id"] if occ and not is_ooo else None,
                    "channel_code": occ["channel_code"] if occ and not is_ooo else None,
                    "is_sellable": not is_ooo,
                    "is_occupied": bool(occ) and not is_ooo,
                    "is_out_of_order": is_ooo,
                    "is_complimentary": False,
                    "room_revenue_net_inr": occ["revenue"] if occ and not is_ooo else 0.0,
                    "commission_inr": occ["commission"] if occ and not is_ooo else 0.0,
                })
        return pd.DataFrame(rows)

    # -- payments -----------------------------------------------------------
    def build_payments(self, bookings: pd.DataFrame) -> pd.DataFrame:
        ch_by_code = {c.code: c for c in spec.CHANNELS}
        rows = []
        seq = 0
        payable = bookings[bookings["status"] != "cancelled"]
        for b in payable.itertuples(index=False):
            seq += 1
            ch = ch_by_code[b.channel_code]
            method = str(self.rng.choice(
                ["upi", "card", "netbanking", "wallet", "cash", "ota_collect"],
                p=[0.34, 0.22, 0.09, 0.05, 0.04, 0.26]))
            paid_at = b.booked_at + dt.timedelta(minutes=int(self.rng.integers(1, 240)))
            gross = float(b.net_room_amount_inr)

            # DEFECT: gateway amount disagrees with the folio.
            if self.rng.random() < spec.DEFECT_RATES["payment_amount_mismatch"]:
                gross = round(gross * float(self.rng.choice([0.9, 0.95, 1.05, 1.1])), 2)

            fee = round(gross * 0.02, 2) if method != "cash" else 0.0
            gst_on_fee = round(fee * 0.18, 2)
            net_credited = round(gross - fee - gst_on_fee, 2)
            settled_at = paid_at + dt.timedelta(days=ch.settlement_days)

            booking_ref = b.booking_id
            # DEFECT: gateway sends a reference that never resolves.
            if self.rng.random() < spec.DEFECT_RATES["orphan_payment_ref"]:
                booking_ref = f"BKX{int(self.rng.integers(100000, 999999))}"

            rows.append({
                "payment_id": f"PAY{seq:07d}",
                "booking_id_raw": booking_ref,
                "paid_at": paid_at,
                "payment_date": (paid_at + dt.timedelta(hours=5, minutes=30)).date(),
                "settled_at": settled_at,
                "settlement_date": (settled_at + dt.timedelta(hours=5, minutes=30)).date(),
                "method": method,
                "gross_amount_inr": gross,
                "gateway_fee_inr": fee,
                "gst_on_fee_inr": gst_on_fee,
                "net_credited_inr": net_credited,
                "is_refund": False,
                "status": "settled" if settled_at.date() <= spec.PERIOD_END else "captured",
            })
        return pd.DataFrame(rows)

    # -- service requests ---------------------------------------------------
    def build_service_requests(self, unit_nights: pd.DataFrame,
                               bookings: pd.DataFrame) -> pd.DataFrame:
        f1 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F1_KOR_SLA_DEGRADATION")
        f3 = next(f for f in spec.PLANTED_FINDINGS if f.key == "F3_WHATSAPP_SILENT_GAP")
        f1_start, f1_end = f1.window
        f3_start, f3_end = f3.window

        occupied = unit_nights[unit_nights["is_occupied"]]
        booking_by_id = bookings.set_index("booking_id")[["guest_id"]].to_dict("index")

        rows = []
        seq = 0
        n_types = len(spec.REQUEST_TYPES)
        for row in occupied.itertuples(index=False):
            if self.rng.random() > spec.REQUESTS_PER_OCCUPIED_NIGHT:
                continue
            seq += 1
            rt = spec.REQUEST_TYPES[int(self.rng.integers(0, n_types))]

            hour = int(np.clip(self.rng.normal(15.0, 5.0), 0, 23))
            created_ist = dt.datetime.combine(
                row.stay_date, dt.time(hour, int(self.rng.integers(0, 60))))
            created_at = created_ist - dt.timedelta(hours=5, minutes=30)

            channel = str(self.rng.choice(
                ["whatsapp", "phone", "front_desk", "app", "email"],
                p=[0.46, 0.20, 0.18, 0.13, 0.03]))

            # PLANTED F3: the WhatsApp feed writes nothing for nine days. Volume
            # redistributes to other channels, so totals merely look seasonal.
            if f3_start <= row.stay_date <= f3_end and channel == "whatsapp":
                if self.rng.random() < 0.94:
                    continue
                channel = "phone"

            sla = rt["sla_minutes"]
            base = float(self.rng.gamma(2.0, sla / 3.2))

            # PLANTED F1: Koramangala evening housekeeping degrades after a
            # staffing change. Blended breach rate barely moves because BTM
            # improves slightly over the same window.
            if f1_start <= row.stay_date <= f1_end:
                if (row.property_code == "BLR-KOR"
                        and rt["team"] == "housekeeping" and 18 <= hour <= 23):
                    base *= f1.params["evening_slowdown_factor"]
                elif row.property_code == "BLR-BTM" and rt["team"] == "housekeeping":
                    base *= f1.params["offsetting_btm_improvement"]

            resolution_minutes = int(np.clip(base, 4, 60 * 26))
            resolved_at = created_at + dt.timedelta(minutes=resolution_minutes)
            first_response = created_at + dt.timedelta(
                minutes=int(np.clip(resolution_minutes * self.rng.uniform(0.08, 0.4), 1, 240)))
            breached = resolution_minutes > sla

            csat = None
            if self.rng.random() < spec.CSAT_RESPONSE_RATE:
                mean = 3.1 if breached else 4.55
                csat = int(np.clip(round(self.rng.normal(mean, 0.85)), 1, 5))

            guest_id = booking_by_id.get(row.booking_id, {}).get("guest_id")
            rows.append({
                "request_id": f"SR{seq:07d}",
                "property_code": row.property_code,
                "unit_code": row.unit_code,
                "booking_id": row.booking_id,
                "guest_id": guest_id,
                "category": rt["category"],
                "subcategory": rt["subcategory"],
                "created_at": created_at,
                "request_date": row.stay_date,
                "first_response_at": first_response,
                "resolved_at": resolved_at,
                "resolved_date": (resolved_at + dt.timedelta(hours=5, minutes=30)).date(),
                "priority": rt["priority"],
                "status": "closed",
                "channel": channel,
                "sla_minutes": sla,
                "resolution_minutes": resolution_minutes,
                "is_sla_breached": breached,
                "reopened_count": int(self.rng.random() < 0.045),
                "csat_score": csat,
            })
        return pd.DataFrame(rows)

    # -- reviews ------------------------------------------------------------
    def build_reviews(self, bookings: pd.DataFrame,
                      requests: pd.DataFrame) -> pd.DataFrame:
        """Structured review rows. Text is attached by the AI phase, not here."""
        breach_by_booking = set(
            requests.loc[requests["is_sla_breached"], "booking_id"].dropna().tolist()
        )
        stayed = bookings[bookings["status"].isin(["checked_out"])]
        rows = []
        seq = 0
        for b in stayed.itertuples(index=False):
            if self.rng.random() > spec.REVIEW_RATE:
                continue
            seq += 1
            had_breach = b.booking_id in breach_by_booking
            # Public ratings for this segment sit at 4.8-4.9, which is exactly why
            # document-level sentiment is analytically useless here.
            mean = 4.15 if had_breach else 4.78
            rating = float(np.clip(round(self.rng.normal(mean, 0.55) * 2) / 2, 1.0, 5.0))
            reviewed_ist = dt.datetime.combine(
                b.check_out_date + dt.timedelta(days=int(self.rng.integers(0, 9))),
                dt.time(int(self.rng.integers(8, 23)), int(self.rng.integers(0, 60))))
            reviewed_at = reviewed_ist - dt.timedelta(hours=5, minutes=30)
            if reviewed_at.date() > spec.PERIOD_END:
                continue
            language = str(self.rng.choice(["en", "hinglish", "hi"], p=[0.72, 0.24, 0.04]))

            # DEFECT: invalid or missing rating.
            if self.rng.random() < spec.DEFECT_RATES["invalid_rating"]:
                rating = None

            rows.append({
                "review_id": f"RV{seq:07d}",
                "property_code": b.property_code,
                "booking_id": b.booking_id,
                "guest_id": b.guest_id,
                "channel_code": b.channel_code,
                "reviewed_at": reviewed_at,
                "review_date": (reviewed_at + dt.timedelta(hours=5, minutes=30)).date(),
                "rating": rating,
                "review_text": None,
                "language": language,
                "is_synthetic_text": True,
                "_had_sla_breach": had_breach,
            })
        return pd.DataFrame(rows)

    # -- inventory ----------------------------------------------------------
    def build_inventory(self, unit_nights: pd.DataFrame) -> pd.DataFrame:
        items = [
            ("HK-TOWEL", "Bath towel", 65.0),
            ("HK-LINEN", "Bed linen set", 180.0),
            ("HK-AMEN", "Bathroom amenity kit", 42.0),
            ("HK-WATER", "Drinking water bottle", 18.0),
            ("HK-TEA", "Tea/coffee sachet pack", 25.0),
        ]
        occ = (unit_nights[unit_nights["is_occupied"]]
               .groupby(["property_code", "stay_date"]).size().reset_index(name="occupied"))
        rows = []
        stock: dict[tuple[str, str], int] = {}
        for r in occ.itertuples(index=False):
            for code, name, cost in items:
                key = (r.property_code, code)
                opening = stock.get(key, int(self.rng.integers(60, 160)))
                consumed = int(r.occupied * self.rng.uniform(0.8, 1.6))
                wastage = int(consumed * self.rng.uniform(0.0, 0.09))
                received = 0
                if opening - consumed - wastage < 25:
                    received = int(self.rng.integers(80, 200))
                closing = opening + received - consumed - wastage
                stock[key] = max(closing, 0)

                # DEFECT: the physical stock count disagrees with the computed
                # balance. This is the most common real inventory defect -- an
                # unrecorded issue, a miscount, or breakage nobody logged -- and it
                # is what makes an inventory balance check worth running at all.
                if self.rng.random() < spec.DEFECT_RATES["inventory_balance_error"]:
                    closing = closing + int(self.rng.choice([-9, -5, -3, 4, 7, 11]))

                rows.append({
                    "property_code": r.property_code,
                    "movement_date": r.stay_date,
                    "item_code": code,
                    "item_name": name,
                    "opening_qty": opening,
                    "received_qty": received,
                    "consumed_qty": consumed,
                    "wastage_qty": wastage,
                    "closing_qty": closing,
                    "unit_cost_inr": cost,
                })
        return pd.DataFrame(rows)

    # -- orchestration ------------------------------------------------------
    def generate(self, n_guests: int = 2600) -> GeneratedData:
        properties = self.build_properties()
        units = self.build_units()
        channels = self.build_channels()
        request_types = self.build_request_types()
        staff = self.build_staff()
        guests = self.build_guests(n_guests)
        bookings = self.build_bookings(units, guests)
        unit_nights = self.build_unit_nights(bookings, units)
        payments = self.build_payments(bookings)
        requests = self.build_service_requests(unit_nights, bookings)
        reviews = self.build_reviews(bookings, requests)
        inventory = self.build_inventory(unit_nights)
        return GeneratedData(
            properties=properties, units=units, channels=channels,
            request_types=request_types, staff=staff, guests=guests,
            bookings=bookings, unit_nights=unit_nights, payments=payments,
            service_requests=requests, reviews=reviews, inventory=inventory,
        )


def dataset_fingerprint(data: GeneratedData) -> str:
    """Stable hash of the dataset shape, for reproducibility assertions."""
    parts = [f"{k}={v}" for k, v in sorted(data.summary().items())]
    parts.append(f"booking_revenue={data.bookings['net_room_amount_inr'].sum():.2f}")
    parts.append(f"unit_night_revenue={data.unit_nights['room_revenue_net_inr'].sum():.2f}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
