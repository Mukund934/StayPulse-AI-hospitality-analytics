# Revenue management

_Generated 2026-08-15 10:24 UTC. As-of date **2026-07-12**._

## Why the as-of date is not today

This warehouse holds no reservations for arrivals after its inventory
horizon (2026-08-11), so a snapshot taken at the
horizon would show only continuing stays. Every forward view is therefore
anchored 30 days back, where a complete forward book exists **and** the
outcome is already known — which is what makes the pace baseline testable
rather than merely plausible.

## Forward position

| | |
|---|---:|
| Nights on the books, next 30 days | 476 |
| Revenue on the books | ₹2,152,984 |
| Pickup, trailing 14 days (added) | 624 |
| Cancelled in the same window | 91 |
| Net pickup | 533 |
| Stay dates scored | 52 |
| Behind pace / on track / ahead | 1 / 41 / 10 |

## How pace is measured

Absolute nights on the books, against the median for the **same property,
same weekday and same days-out horizon**, taken from the last
8 comparable dates before the snapshot.

Two design decisions, both forced by defects found while building this:

1. **A trailing window, not all history.** Pooling all 18 months reported 24
   stay dates ahead of pace and zero behind. Sellable inventory grew from
   ~900 to ~1,200 unit-nights per month in March 2026, so the baseline was
   comparing a 40-unit portfolio against the period when it had about 30.

2. **A distribution band, not a fixed percentage.** Nights on the books for
   one property nine days out range from 3 to 15 across comparable Tuesdays.
   A median of 6 against an observation of 14 is 233% and entirely ordinary.
   A date is flagged only if it is outside the p25–p75 band **and** at least
   4 room-nights from the median.

Pace is never expressed as a share of the final book, because for a future
stay date the final book is precisely the unknown; any metric that appears to
compute it has substituted a forecast for the truth.

## Signals

No signal recommends a price. There is no competitor rate feed and no
elasticity in this warehouse, so a rate recommendation would be an opinion
wearing a number.

**Mon 13 Jul is 4 room-nights behind its usual position with 1 days to go** · `soft_demand` · confidence high

- 9 nights on the books
- typically 13.0 by this point on a Monday (usual range 10-14)
- shortfall 4.0 nights, 69% of the median
- baseline from the last 8 comparable Mondays at this property

_Investigate:_ Check whether the gap is demand or mix: compare channel pickup for this date against the same weekday, and confirm no rate or availability restriction is suppressing it.

**Mon 20 Jul is 9 room-nights ahead of its usual position with 8 days to go** · `demand_strength` · confidence high

- 16 nights on the books
- typically 7.0 by this point on a Monday (usual range 5-10)
- surplus 9.0 nights, 229% of the median
- baseline from the last 8 comparable Mondays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Tue 21 Jul is 8 room-nights ahead of its usual position with 9 days to go** · `demand_strength` · confidence high

- 14 nights on the books
- typically 6.0 by this point on a Tuesday (usual range 5-8)
- surplus 8.0 nights, 233% of the median
- baseline from the last 8 comparable Tuesdays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Thu 23 Jul is 8 room-nights ahead of its usual position with 11 days to go** · `demand_strength` · confidence medium

- 14 nights on the books
- typically 6.0 by this point on a Thursday (usual range 5-8)
- surplus 8.0 nights, 233% of the median
- baseline from the last 8 comparable Thursdays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Sun 19 Jul is 7 room-nights ahead of its usual position with 7 days to go** · `demand_strength` · confidence high

- 16 nights on the books
- typically 9.0 by this point on a Sunday (usual range 7-10)
- surplus 7.0 nights, 178% of the median
- baseline from the last 8 comparable Sundays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Fri 24 Jul is 6 room-nights ahead of its usual position with 12 days to go** · `demand_strength` · confidence medium

- 12 nights on the books
- typically 6.0 by this point on a Friday (usual range 5-8)
- surplus 6.0 nights, 200% of the median
- baseline from the last 8 comparable Fridays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Wed 22 Jul is 6 room-nights ahead of its usual position with 10 days to go** · `demand_strength` · confidence high

- 12 nights on the books
- typically 6.5 by this point on a Wednesday (usual range 6-7)
- surplus 5.5 nights, 185% of the median
- baseline from the last 8 comparable Wednesdays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Thu 23 Jul is 5 room-nights ahead of its usual position with 11 days to go** · `demand_strength` · confidence medium

- 12 nights on the books
- typically 7.0 by this point on a Thursday (usual range 6-7)
- surplus 5.0 nights, 171% of the median
- baseline from the last 8 comparable Thursdays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Sat 18 Jul is 4 room-nights ahead of its usual position with 6 days to go** · `demand_strength` · confidence high

- 15 nights on the books
- typically 11.0 by this point on a Saturday (usual range 9-12)
- surplus 4.0 nights, 136% of the median
- baseline from the last 8 comparable Saturdays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

**Wed 22 Jul is 4 room-nights ahead of its usual position with 10 days to go** · `demand_strength` · confidence high

- 11 nights on the books
- typically 7.0 by this point on a Wednesday (usual range 7-8)
- surplus 4.0 nights, 157% of the median
- baseline from the last 8 comparable Wednesdays at this property

_Investigate:_ Filling early. Verify remaining inventory and check whether the rate on the remaining units was set before this demand appeared.

## Booking curve

Share of the final book normally sold by N days out, portfolio median:

| Days out | Median % sold | p25–p75 |
|---:|---:|---|
| 0 | 100.0% | 100.0–100.0% |
| 1 | 100.0% | 100.0–100.0% |
| 3 | 100.0% | 95.1–100.0% |
| 5 | 92.5% | 79.3–100.0% |
| 7 | 75.9% | 64.4–92.4% |
| 10 | 58.7% | 46.7–74.0% |
| 14 | 39.5% | 27.4–51.3% |
| 21 | 19.4% | 7.9–30.2% |
| 30 | 5.6% | 0.0–15.0% |
| 45 | 0.0% | 0.0–5.3% |

A very short booking window: barely anything is sold a month out and the
book fills in the final week. That is consistent with the channel mix —
two channels book same-day and the portfolio median lead time is 7 days.

## Lead time by channel

| Channel | Bookings | Mean | Median | p90 | Same-day | 30d+ | Cancel |
|---|---:|---:|---:|---:|---:|---:|---:|
| MakeMyTrip | 1,050 | 12.9 | 10 | 26 | 0.1% | 7.9% | 18.9% |
| Corporate | 1,016 | 5.9 | 4 | 12 | 0.4% | 1.2% | 5.9% |
| Booking.com | 943 | 16.8 | 11 | 36 | 0.0% | 15.0% | 23.0% |
| Direct | 868 | 8.8 | 6 | 18 | 0.0% | 3.1% | 7.7% |
| Bag2Bag | 444 | 0.6 | 0 | 1 | 50.7% | 0.0% | 8.1% |
| Agoda | 422 | 19.6 | 14 | 41 | 0.0% | 19.4% | 22.7% |
| Airbnb | 366 | 24.7 | 18 | 53 | 0.0% | 27.0% | 14.2% |
| Walk-in | 119 | 0.7 | 1 | 1 | 43.7% | 0.0% | 3.4% |

Long-lead OTA channels cancel three to four times as often as short-lead
direct and corporate. That correlation is what makes wash worth modelling
per channel rather than as a single portfolio rate.

## Wash funnel

| Channel | Bookings | Cancelled | No-show | **Wash** |
|---|---:|---:|---:|---:|
| Booking.com | 943 | 23.0% | 1.1% | **24.1%** |
| Agoda | 422 | 22.7% | 0.7% | **23.5%** |
| MakeMyTrip | 1,050 | 18.9% | 1.9% | **20.8%** |
| Airbnb | 366 | 14.2% | 2.2% | **16.4%** |
| Direct | 868 | 7.7% | 1.8% | **9.6%** |
| Bag2Bag | 444 | 8.1% | 0.7% | **8.8%** |
| Corporate | 1,016 | 5.9% | 1.5% | **7.4%** |
| Walk-in | 119 | 3.4% | 0.0% | **3.4%** |

## Grain reconciliation

The demand grain (booking-nights) and the inventory grain (unit-nights) do
not trivially agree. Two structural differences close the gap exactly, and
both are asserted by the test suite rather than excused as rounding:

```
   13,640   exploded booking-nights (stayed bookings)
     -410   never allocated a unit — denied demand (3.0%)
     +380   hourly bookings holding a unit-night but selling no night
  -------
   13,610   occupied unit-nights   ✓ exact
```

The 380 hourly bookings are the whole Bag2Bag channel. They earn
revenue and consume a room, but under half-open `[check_in, check_out)`
intervals they sell zero room-nights — they check out on the day they check
in. That is why `adr_excl_microstay_inr` exists as a separate registered
metric: including them dilutes ADR without contributing occupancy.
