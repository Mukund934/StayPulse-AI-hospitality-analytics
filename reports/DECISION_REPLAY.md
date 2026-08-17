# Decision Replay

_Generated 2026-08-17T16:11:08+00:00 from 14 historical as-of dates, 35-day window._

## What a replay is

Pick a past date T. Rebuild everything StayPulse could have known then --
the book, the trailing pickup, the pace benchmark, the published holiday
calendar, the holiday effects measurable from holidays already past -- and
report what it would have said. Then, separately, show what happened.

The reconstruction and the outcome are two functions. The reconstruction
never receives the outcome, which is what makes the guarantee testable:
inserting bookings dated after T must leave the reconstruction
byte-identical, and a SHA-256 fingerprint over the whole decision reduces
that to one comparison.

## Forecast accuracy at the replayed origins

| Horizon (days) | Forecasts | MAE (room-nights) | Median abs error |
|---|---:|---:|---:|
| 1-3 | 42 | 1.167 | 1.0 |
| 4-7 | 56 | 2.464 | 2.0 |
| 8-14 | 98 | 3.286 | 2.5 |
| 15-30 | 224 | 3.462 | 3.0 |

## Pace calls against their outcome

Resolved calls: **605**.

A date flagged `behind` at T is claiming it will finish below what
comparable dates finally carry. The base rate is the same measurement over
every scored date, and the flag is only worth something to the extent it
beats it.

| Call | n | Precision | Recall | Base rate | Lift |
|---|---:|---:|---:|---:|---:|
| behind | 26 | 100.0% | 11.9% | 36.0% | 64.0pp |
| ahead | 46 | 95.7% | 11.4% | 64.0% | 31.7pp |

### Read the recall column before the precision column

The dual gate is a deliberately conservative flag: a date must be both
outside the p25-p75 band of comparable history AND at least 4 room-nights
from the median. That conservatism is the whole reason precision is high,
and the cost is visible in recall -- 11.9% of the
dates that finished below expectation were ever flagged. The flag is not a
detector of weak dates. It is a short list of the ones weak enough to be
worth someone's morning.

**On the 100.0%.** 26 calls with no misses does not mean the flag
cannot miss. With zero failures in n trials the 95% upper bound on the
failure rate is about 3/n -- here **11.5%**.
That is the honest ceiling on this claim, and a second year of data
would tighten it more than any change to the rule would.

### Does the dual gate earn its complexity?

Strip the band and the materiality threshold, keep only the sign of the
gap -- "below the median at all" -- and score that instead:

| Rule | n | Finished as called |
|---|---:|---:|
| below median at T (sign only) | 197 | 65.0% |
| **flagged `behind` (dual gate)** | 26 | **100.0%** |
| above median at T (sign only) | 353 | 79.6% |
| **flagged `ahead` (dual gate)** | 46 | **95.7%** |

It does. The gate is not restating the sign of the gap, and the two
thresholds are carrying the difference rather than decorating it.

Median nights picked up after the snapshot: **5**.

## Per-origin detail

| As of | On books | Scored | Behind | Ahead | Holidays ahead | With prior measurement | Forecast MAE |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2025-06-02 | 205 | 33 | 5 | 0 | 0 | 5 | 3.286 |
| 2025-07-02 | 289 | 46 | 4 | 4 | 0 | 5 | 2.3 |
| 2025-08-01 | 290 | 41 | 1 | 2 | 1 | 5 | 2.171 |
| 2025-08-31 | 353 | 47 | 1 | 5 | 1 | 6 | 2.4 |
| 2025-09-30 | 303 | 36 | 0 | 0 | 3 | 7 | 2.671 |
| 2025-10-30 | 331 | 43 | 3 | 7 | 1 | 9 | 3.086 |
| 2025-11-29 | 387 | 45 | 0 | 7 | 2 | 9 | 3.5 |
| 2025-12-29 | 264 | 41 | 6 | 0 | 2 | 10 | 3.586 |
| 2026-01-28 | 313 | 41 | 1 | 4 | 1 | 11 | 3.329 |
| 2026-02-27 | 340 | 34 | 1 | 1 | 3 | 11 | 3.157 |
| 2026-03-29 | 412 | 50 | 0 | 4 | 1 | 11 | 3.886 |
| 2026-04-28 | 355 | 44 | 2 | 2 | 0 | 11 | 3.471 |
| 2026-05-28 | 417 | 48 | 0 | 5 | 0 | 11 | 3.671 |
| 2026-06-27 | 414 | 56 | 2 | 5 | 0 | 11 | 3.5 |

## Limitations

- **One dataset, one portfolio.** Every number here describes three
  corporate aparthotels over eighteen months. None of it generalises.
- **The pace call is scored on the demand grain**, against booking-nights
  live on the arrival date. The forecast is scored on the inventory grain,
  against occupied unit-nights. They are different quantities on purpose;
  each is matched to what it predicted, and they are not comparable to
  each other.
- **No counterfactual.** This shows what the system would have said, not
  what would have happened had anyone acted on it. There is no price
  elasticity in this warehouse, so the value of acting is unmeasurable and
  is therefore not reported.
- **Capacity in the replayed forecast** comes from inventory the origin had
  already seen. Out-of-order nights are settled only once a date has
  passed, so future sellable capacity is not knowable at T even in
  principle.
