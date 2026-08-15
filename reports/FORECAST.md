# Forecast evaluation

_Generated 2026-08-15 10:23 UTC. Regenerate with `python scripts/run_revenue_analysis.py`._

## What is being forecast

Daily occupied room-nights, portfolio total. The series averages **24.4 room-nights per night**, so read every error figure against that level.

## Why five models

A single forecast with an error attached proves nothing. Without a baseline
there is no way to know whether 12% error is good, bad, or worse than
repeating last Tuesday. Seasonal naive is included precisely because it is
the bar a weekly-seasonal series sets, and it is the model that most
sophisticated attempts quietly fail to beat.

Rolling origin, 40 origins across the last 120 days, 5,325 forecasts evaluated. Every forecast uses only data at or before its own origin; a test
asserts the pickup model's inputs match an independent as-of reconstruction.

## Results

### 1-day horizon

| Model | MAE (nights) | RMSE | MAPE | Bias | vs mean level |
|---|---:|---:|---:|---:|---:|
| `pickup` **← best** | 1.25 | 1.64 | 4.0% | +0.28 | 5.1% |
| `naive` | 2.33 | 3.09 | 7.7% | -0.33 | 9.5% |
| `moving_average` | 3.75 | 4.67 | 12.9% | -0.63 | 15.3% |
| `dow_moving_average` | 3.90 | 5.11 | 13.3% | -0.74 | 16.0% |
| `seasonal_naive` | 4.05 | 5.11 | 14.0% | -0.50 | 16.6% |

### 7-day horizon

| Model | MAE (nights) | RMSE | MAPE | Bias | vs mean level |
|---|---:|---:|---:|---:|---:|
| `pickup` **← best** | 2.82 | 3.35 | 9.2% | -0.32 | 11.5% |
| `dow_moving_average` | 3.88 | 5.07 | 13.4% | -0.57 | 15.9% |
| `moving_average` | 3.88 | 4.92 | 13.3% | -0.65 | 15.9% |
| `naive` | 4.08 | 5.17 | 14.1% | -0.34 | 16.7% |
| `seasonal_naive` | 4.08 | 5.17 | 14.1% | -0.34 | 16.7% |

### 14-day horizon

| Model | MAE (nights) | RMSE | MAPE | Bias | vs mean level |
|---|---:|---:|---:|---:|---:|
| `pickup` **← best** | 3.58 | 4.54 | 11.6% | -0.42 | 14.7% |
| `moving_average` | 4.81 | 5.58 | 16.1% | -0.74 | 19.7% |
| `dow_moving_average` | 5.03 | 6.00 | 17.0% | -0.62 | 20.6% |
| `naive` | 6.14 | 7.01 | 20.7% | -0.19 | 25.1% |
| `seasonal_naive` | 6.14 | 7.01 | 20.7% | -0.19 | 25.1% |

### 30-day horizon

| Model | MAE (nights) | RMSE | MAPE | Bias | vs mean level |
|---|---:|---:|---:|---:|---:|
| `dow_moving_average` **← best** | 3.57 | 4.31 | 11.8% | -0.98 | 14.6% |
| `moving_average` | 3.61 | 4.32 | 11.9% | -0.90 | 14.8% |
| `pickup` | 4.39 | 5.06 | 14.4% | -1.55 | 18.0% |
| `naive` | 4.55 | 5.81 | 14.9% | -0.23 | 18.6% |
| `seasonal_naive` | 4.68 | 5.50 | 15.6% | -0.61 | 19.1% |

## Reading this honestly

The pickup model wins at 1, 7 and 14 days and **loses at 30** to a day-of-week moving average.
That is the expected shape and it is reported rather than buried: at 30 days
out the median stay date in this portfolio is only about 8% sold, so a model
built on the book has almost nothing to read and the seasonal average is
simply better. A pickup model that appeared to win at every horizon would be
evidence of leakage, not of skill.

`naive` and `seasonal_naive` score identically at horizons that are multiples
of seven. This is arithmetic, not a bug: at h=7 the most recent same-weekday
value *is* the origin. A test pins it so nobody later 'fixes' it.

## Limitations

- Portfolio level only. Per-property forecasts on 3 properties would be much noisier.
- No event or holiday regressor yet; a festival week is invisible to every model here.
- The dataset is synthetic. These error rates describe this generator, not a real hotel.
