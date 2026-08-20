# Model registry and drift

_Generated 2026-08-20T12:53:25+00:00. 7 models over a 365-day window._

## What this is

A record of what has been measured about every model this project ships. Not an MLOps platform: there is no model server, feature store or experiment tracker, because six baselines and one classifier on a five-thousand-row warehouse do not need any.

## The drift measurement, and the mistake it avoids

The obvious monitor compares MAE early against MAE late and alerts when it
rises. Measured that way here, **every model degrades** -- the naive models
by 15%, the moving averages by 44-46%, the production pickup model by 32%.

Almost none of that is degradation.

MAE is measured in room-nights, and the portfolio grew from roughly 29 to 39
sellable units in March 2026. The series level rose about 26% between the two
halves of the window, so the error scales with it. Normalising each error by
the level of the series at its origin changes the verdict for half the table.

| Model | Champion at | Absolute drift | Scale-relative | Verdict |
|---|---|---:|---:|---|
| `naive` | — | +15.4% | -6.8% | **stable** |
| `seasonal_naive` | — | +15.4% | -6.8% | **stable** |
| `moving_average` | — | +46.2% | +17.1% | **degrading** |
| `dow_moving_average` | [30] | +44.0% | +16.1% | **degrading** |
| `pickup` | [1, 7, 14] | +32.4% | +7.6% | **stable** |
| `seasonal_holiday` | — | +47.4% | +19.8% | **degrading** |

An absolute-MAE monitor calls the two naive models degraded when they
**improved** by about 7% relative to scale, and reports the production
model as +32% when the scale-relative figure is +7.6%.

This is the **fourth** time this project has been caught by the same family
of error -- comparing across units of different scale without normalising.
PART L-14 of the roadmap records the other three: pooled holiday multipliers,
Simpson's paradox in the alert bias, and an unweighted calibration mean.
Both figures are published here and the scale-relative one carries the
verdict.

## Registry

### `naive`

- **Family** time series baseline
- **Target** daily occupied room-nights, portfolio total
- **Version** `1eeb23a9e1dd`
- **Training window** rolling origin, 365 days, 122 origins
- **Features** realised occupancy history
- **Champion at horizons** none
- **Status** active
- **Limitations**
  - tomorrow equals today

### `seasonal_naive`

- **Family** time series baseline
- **Target** daily occupied room-nights, portfolio total
- **Version** `acfa6dade031`
- **Training window** rolling origin, 365 days, 122 origins
- **Features** realised occupancy history
- **Champion at horizons** none
- **Status** active
- **Limitations**
  - next Tuesday equals last Tuesday

### `moving_average`

- **Family** time series baseline
- **Target** daily occupied room-nights, portfolio total
- **Version** `0f5039c58b87`
- **Training window** rolling origin, 365 days, 122 origins
- **Features** realised occupancy history
- **Champion at horizons** none
- **Status** active
- **Limitations**
  - mean of the trailing 28 days

### `dow_moving_average`

- **Family** time series baseline
- **Target** daily occupied room-nights, portfolio total
- **Version** `32a77d8be1f4`
- **Training window** rolling origin, 365 days, 122 origins
- **Features** realised occupancy history
- **Champion at horizons** [30]
- **Status** active
- **Limitations**
  - mean of the last 4 same-weekday values

### `pickup`

- **Family** booking-curve pickup
- **Target** daily occupied room-nights, portfolio total
- **Version** `89ac2dc2ee56`
- **Training window** rolling origin, 365 days, 122 origins
- **Features** realised occupancy history, on-the-books at horizon
- **Champion at horizons** [1, 7, 14]
- **Status** active
- **Limitations**
  - Uses information the other models cannot see -- what is already sold -- so its advantage at short horizons is structural.
  - on the books now, plus the pickup comparable dates still received

### `seasonal_holiday`

- **Family** time series baseline
- **Target** daily occupied room-nights, portfolio total
- **Version** `568bef3c8335`
- **Training window** rolling origin, 365 days, 122 origins
- **Features** realised occupancy history, measured holiday multiplier
- **Champion at horizons** none
- **Status** active
- **Limitations**
  - Published as a FAILURE. On holiday-adjacent dates it scored MAE 4.90 against a 4.19 baseline. Kept registered so its loss appears in the comparison rather than being quietly removed.
  - day-of-week baseline scaled by a measured holiday effect

### `cancellation_risk`

- **Family** logistic regression, L2, numpy
- **Target** booking cancelled before arrival
- **Version** `f3e5f283f501`
- **Training window** temporal split at 2026-03-31; 3917 train / 1311 test
- **Features** channel_CORP, channel_DIRECT, lead_time_saturating, channel_WALKIN, channel_AIRBNB, nights, property_BLR-KOR, channel_MMT …
- **Champion at horizons** none
- **Status** active
- **Calibration** 2.03pp weighted MAE over 10 bins
- **Limitations**
  - No-show is deliberately excluded: it is a flat 1.4% in the generator and unlearnable, measured at AUC 0.527.
  - No censoring in this data; a survival model would be correct on real bookings.
