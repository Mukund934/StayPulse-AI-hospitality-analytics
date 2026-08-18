# Forecast intervals and the Backtesting Lab

_Generated 2026-08-18T10:04:55+00:00 from 21090 forecasts over 122 rolling origins._

## The question an interval has to answer

An 80% interval claims the truth falls inside it 80% of the time. That is
measurable, and measuring it is the only thing that separates an interval
from a shaded band on a chart.

It has to be measured **out of sample**. An empirical quantile reproduces
its nominal level on its own sample by construction, so calibrating on the
evaluation set and then reporting coverage is testing arithmetic. Here the
interval is rebuilt at every evaluation origin from residuals whose target
had already been realised by then.

## Coverage, by method and level

| Method | Level | Out-of-sample | In-sample | Deviation | Median width |
|---|---:|---:|---:|---:|---:|
| absolute_plain | 50% | 47.2% | 53.8% | -2.8pp | 6.0 |
| absolute_conformal | 50% | 50.1% | 55.7% | +0.1pp | 6.75 |
| scaled_plain | 50% | 49.5% | 51.0% | -0.5pp | 6.96 |
| scaled_conformal **(published)** | 50% | 52.6% | 52.8% | +2.6pp | 7.55 |
| absolute_plain | 80% | 74.1% | 82.7% | -5.9pp | 11.46 |
| absolute_conformal | 80% | 78.6% | 84.6% | -1.4pp | 12.66 |
| scaled_plain | 80% | 77.7% | 81.0% | -2.3pp | 12.98 |
| scaled_conformal **(published)** | 80% | 82.6% | 83.3% | +2.6pp | 14.52 |
| absolute_plain | 95% | 90.9% | 96.7% | -4.1pp | 17.5 |
| absolute_conformal | 95% | 93.9% | 98.7% | -1.1pp | 20.18 |
| scaled_plain | 95% | 93.2% | 96.3% | -1.8pp | 19.97 |
| scaled_conformal **(published)** | 95% | 95.1% | 98.5% | +0.1pp | 22.99 |

### The gap between the two coverage columns is the whole point

The plain empirical quantile reports 82.7% in sample against a nominal 80% — near-perfect, and meaningless. The same intervals
covered 74.1% of the truths they had not seen.
Any interval method that reports only the first number is reporting the
behaviour of a quantile, not the behaviour of a forecast.

### Two corrections, neither of them tuned

The plain method under-covers badly. Two changes fixed it, and it matters
that each was derived rather than searched for — a widening factor adjusted
until coverage hit 80% would be fitting the evaluation set.

**1. Scale-relative residuals.** Measured first: the error spread grows from
sd 2.06 to sd 3.52 across the study window, tracking the portfolio's growth
from ~29 to ~39 sellable unit-nights a day. Residuals from a smaller business
understate the spread of a larger one. The residual is divided by the
trailing level of the series at its origin and multiplied back at prediction
time. This is the same lesson `revenue.BENCHMARK_WINDOW` already records: a
baseline must track the level of the business rather than average over its
history.

**2. Conformal quantile selection.** A plug-in empirical quantile under-covers
in finite samples. The split-conformal correction takes the
`ceil((n+1)(1-a/2))`-th order statistic instead, which carries a finite-sample
guarantee of *at least* the nominal level. It always widens and never narrows,
which is why the published method errs slightly conservative.

**The check that the corrections were not tuned:** they hold at 50% and 95%
as well as at 80%. A fudge factor fitted to one level cannot land correctly
at all three.

Published method: **scaled_conformal**, covering 82.6% out of sample at a nominal 80%.

### Coverage by horizon (published method, 80%)

| Horizon (days) | Forecasts | Coverage | Median width | Calibration residuals |
|---|---:|---:|---:|---:|
| 1 | 612 | 80.7% | 11.68 | 70 |
| 7 | 588 | 75.9% | 12.99 | 68 |
| 14 | 564 | 84.0% | 15.22 | 66 |
| 30 | 498 | 86.1% | 14.36 | 61 |

Long horizons calibrate on fewer residuals, and that is not a shortcut being
taken — it is the rule being enforced. A forecast made yesterday for 30 days
out has no error yet, so at any origin the 30-day horizon has a month less
usable history than the 1-day horizon. Filtering on the forecast's *origin*
instead of its *target* would hide that, and would quietly calibrate on
errors nobody had observed.

## Backtesting Lab

One backtest, cut along the dimensions that change the answer.

### By horizon

| Horizon | Forecasts | Best model | MAE |
|---|---:|---|---:|
| 1 | 732 | pickup | 1.172 |
| 7 | 720 | pickup | 2.663 |
| 14 | 708 | pickup | 3.602 |
| 30 | 672 | dow_moving_average | 2.993 |

The pickup model leads at short horizons and gives way to the seasonal
baseline at thirty days. That was the stated rationale for including it, and
this is the measurement rather than the assertion — a test now fails if the
relationship inverts.

### By weekday

| Weekday | Forecasts | Best model | MAE |
|---|---:|---|---:|
| Monday | 3030 | pickup | 3.189 |
| Tuesday | 3042 | pickup | 2.929 |
| Wednesday | 2988 | pickup | 3.037 |
| Thursday | 2994 | pickup | 3.097 |
| Friday | 3006 | dow_moving_average | 2.716 |
| Saturday | 3012 | pickup | 3.061 |
| Sunday | 3018 | pickup | 3.119 |

### By holiday adjacency

| Dates | Forecasts | Best model | MAE |
|---|---:|---|---:|
| ordinary | 13254 | pickup | 3.101 |
| holiday_adjacent | 7836 | pickup | 2.956 |

### By month

| Month | Forecasts | Best model | MAE |
|---|---:|---|---:|
| 2025-08 | 462 | moving_average | 2.227 |
| 2025-09 | 1728 | pickup | 2.686 |
| 2025-10 | 1860 | pickup | 2.298 |
| 2025-11 | 1800 | pickup | 2.742 |
| 2025-12 | 1860 | pickup | 3.848 |
| 2026-01 | 1860 | pickup | 2.597 |
| 2026-02 | 1680 | pickup | 2.864 |
| 2026-03 | 1860 | pickup | 3.189 |
| 2026-04 | 1800 | pickup | 3.432 |
| 2026-05 | 1860 | pickup | 2.969 |
| 2026-06 | 1800 | pickup | 3.893 |
| 2026-07 | 1860 | moving_average | 3.153 |
| 2026-08 | 660 | pickup | 2.836 |

## What is deliberately not sliced

**No per-property or per-channel accuracy.** The forecast target is
portfolio-total occupied room-nights — one series — so there is no
per-property prediction to score. Slicing the *actuals* by property while the
forecast stays portfolio-wide would produce a number that looks like
per-property accuracy and is not.

Making that cut real needs a per-property forecast target: daily actuals
grouped by property, the pickup model's on-the-books matrix likewise, and a
separate backtest per property. It would be forecasting a series of roughly
ten room-nights a day, where the models behave differently enough that these
results would not carry over. Named rather than approximated.

## Limitations

- **Coverage is a portfolio-level property.** These intervals are calibrated
  and validated on the portfolio total. They say nothing about how often a
  single property's occupancy falls inside a band.
- **Conformal guarantees are marginal, not conditional.** The guarantee is
  about the average over all forecasts, not about any particular horizon or
  weekday. The per-horizon table is reported precisely so the conditional
  behaviour is visible rather than assumed.
- **Residual autocorrelation is not modelled.** Forecasts from one origin at
  adjacent horizons are highly correlated, so the effective sample behind each
  quantile is smaller than its nominal count.
- **Bounds are clipped at zero and not at capacity**, because future sellable
  inventory is not knowable at the origin.
