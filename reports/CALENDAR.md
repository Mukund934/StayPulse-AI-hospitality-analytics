# Calendar intelligence

_Generated 2026-08-16 13:28 UTC. Regenerate with `python scripts/run_calendar_analysis.py`._

## Where the calendar comes from

**No external API.** Nager.Date is the obvious free, zero-auth choice and it
does not cover India. Verified 2026-08-15:

```
GET /api/v3/PublicHolidays/2025/IN  ->  HTTP 204, 0 bytes
GET /api/v3/PublicHolidays/2025/US  ->  HTTP 200, 3,700 bytes   (control)
GET /api/v3/AvailableCountries      ->  204 countries, "IN" absent
```

The replacement is a committed, source-cited table
(`data/reference/india_holidays.json`). That is a better fit, not a
workaround: this dataset is frozen at 2026-08-11 and will never need next
year's holidays, so a live API would add a key, a rate limit, a CI network
dependency and an outage mode in exchange for nothing.

- **21 holidays**, covering 2025-01-01 to 2026-12-25
- **9 entries carry lunar-calendar dates** and require a
  one-time human check against an official source

## How the effect is measured, and why it is not circular

The generator plants four suppressive festival windows. Reading those windows
out of the spec, flagging the same dates, and reporting that the effect
appears where it was planted would prove nothing.

So the warehouse stores only what is externally true — **the dates public
holidays fell on** — and the effect is measured at each *offset* from a
holiday, assuming no window at all. The window either emerges from the data
or it does not. A test asserts that the measurement path never imports the
generator spec.

The baseline for each date is the median occupancy of the **same weekday** at
the **same property** within ±8 weeks, excluding all holiday-adjacent dates.
Both controls are load-bearing: Diwali 2025 fell on a Monday, and Monday
carries a 1.14 demand multiplier against Saturday's 0.74, so an all-days
baseline would have reported a demand *lift* on a date demand actually fell.

## Measured effects

| Holiday | Effect | 95% interval | Occurrences | Dates | Direction |
|---|---:|---|---:|---:|---|
| Christmas Day | **-20.36pp** | -30.4 to -10.3 | 1 | 22 | suppresses demand |
| New Year's Day | **-11.47pp** | -22.2 to -0.7 | 2 | 22 | suppresses demand |
| Diwali | **-10.51pp** | -17.2 to -3.8 | 1 | 28 | suppresses demand |
| Gandhi Jayanti | **-6.48pp** | -13.5 to +0.5 | 1 | 30 | suppresses demand |
| Kannada Rajyotsava | **-3.39pp** | -11.8 to +5.0 | 1 | 26 | suppresses demand |
| Ugadi | **-3.12pp** | -7.8 to +1.6 | 2 | 61 | suppresses demand |
| Good Friday | **-2.58pp** | -6.6 to +1.4 | 2 | 75 | suppresses demand |
| Republic Day | **-0.85pp** | -7.1 to +5.4 | 2 | 34 | suppresses demand |
| Holi | **+0.36pp** | -3.9 to +4.6 | 2 | 65 | raises demand |
| Independence Day | **+4.92pp** | -0.7 to +10.5 | 2 | 42 | raises demand |
| Id-ul-Fitr | **+10.92pp** | +2.3 to +19.6 | 1 | 16 | raises demand |

**Holidays suppress demand at this portfolio.** That is the inverse of a
leisure property and exactly what a corporate aparthotel should show:
the guests are business travellers and business travel stops during festivals.
Most RMS marketing assumes the opposite.

### Recovery of the planted windows

Three of the four planted windows fall inside the data (3 of 4 — Diwali 2026 is after the horizon).

| Planted | Window | Multiplier | Recovered? |
|---|---|---:|---|
| Diwali | 2025-10-18 → 2025-10-25 | ×0.62 | **Yes** (-10.5pp) |
| Diwali | 2026-11-06 → 2026-11-13 | ×0.62 | — (after data horizon) |
| Year end | 2025-12-24 → 2026-01-02 | ×0.7 | **Yes** — Christmas −20.4pp, New Year −11.5pp, both intervals exclude zero |
| Holi | 2026-03-03 → 2026-03-05 | ×0.8 | **No** — effect not distinguishable from zero (+0.4pp) |

Diwali (×0.62) and the year-end window (×0.70) were both recovered with
intervals excluding zero. **Holi was not.** Its planted multiplier is ×0.80,
the mildest of the four, over a three-day window — and a 38% demand cut at
Diwali produced only a 9.6% occupancy fall, because at ~78% occupancy the
booking buffer absorbs most of a demand reduction. A 20% cut is simply not
visible above the noise. That is a coherent negative result, not a defect.

## Offset profile

Effect by days from the nearest holiday. No window assumed.

| Offset | Dates | Occupancy | Baseline | Effect | Interval excludes 0 |
|---:|---:|---:|---:|---:|---|
| -7 | 27 | 68.0% | 77.3% | -9.30pp | yes |
| -6 | 27 | 70.1% | 76.3% | -6.24pp |  |
| -5 | 29 | 74.6% | 74.0% | +0.55pp |  |
| -4 | 29 | 75.7% | 77.7% | -1.98pp |  |
| -3 | 28 | 77.4% | 76.2% | +1.19pp |  |
| -2 | 28 | 76.1% | 78.9% | -2.79pp |  |
| -1 | 28 | 74.9% | 81.2% | -6.30pp |  |
| +0 | 30 | 76.6% | 78.1% | -1.56pp |  |
| +1 | 28 | 74.4% | 77.0% | -2.60pp |  |
| +2 | 28 | 73.4% | 74.2% | -0.81pp |  |
| +3 | 29 | 72.4% | 77.5% | -5.06pp |  |
| +4 | 27 | 76.9% | 74.2% | +2.76pp |  |
| +5 | 27 | 75.8% | 78.8% | -3.01pp |  |
| +6 | 29 | 73.4% | 79.5% | -6.10pp |  |
| +7 | 27 | 71.8% | 76.7% | -4.83pp |  |

## Forecasting with holidays — a measured failure

This is the part worth reading.

The roadmap proposed a holiday-aware forecast model to attack the 30-day
horizon, where the incumbent `pickup` model loses. It was built, measured,
and **it does not work on this dataset.** Publishing that is the point:
a sixth model that loses is a legitimate result, and tuning until it won
would have been fitting the evaluation.

### Why a separate evaluation was needed

The standard 120-day backtest window contains **zero** festival windows —
all three in-data windows are earlier than mid-April 2026. Scored there, a
holiday model is identical to its baseline by construction. So the window was
widened to 260 days and accuracy is reported on
holiday-adjacent dates separately from ordinary ones.

### Result on holiday-adjacent dates

| Model | MAE | RMSE | Bias |
|---|---:|---:|---:|
| `pickup` | 3.32 | 4.18 | -0.55 |
| `moving_average` | 4.04 | 4.98 | -0.80 |
| `dow_moving_average` ← its baseline | 4.19 | 5.09 | -0.84 |
| `seasonal_holiday` ← the holiday model | 4.9 | 5.8 | +0.37 |
| `seasonal_naive` | 4.98 | 6.42 | -0.56 |
| `naive` | 5.04 | 6.35 | -0.60 |

**`seasonal_holiday` scores MAE 4.9 against its own baseline's 4.19.** Three variants were measured, each worse than doing nothing:

| Variant | MAE on holiday dates |
|---|---:|
| pooled cross-holiday fallback | 5.11 |
| specific holiday only | 4.94 |
| specific + significance gate | 4.90 |
| **no adjustment at all (baseline)** | **4.19** |

### The mechanism, which is more interesting than the model

**1. Pooling across holidays is unsound.** Christmas runs −20.4pp and New Year
−11.5pp, while Id-ul-Fitr runs +10.9pp and Independence Day +4.9pp. Averaging
them produced pooled multipliers of 1.02–1.19 — *above* 1, i.e. push the
forecast up — which were then applied to Christmas and New Year, the two dates
that collapse hardest. Bias flipped from −0.84 to +0.98.

**2. A significance gate does not save it, because the significance is fake.**
At the estimation date the data covered Feb–Nov 2025. Republic Day 2025 fell on
26 January — *before the dataset starts* — leaving 4 tail observations that
measured −34.13pp with an interval of [−58.2, −10.0]. It passed the gate on
noise. Meanwhile Holi, planted as suppressive at ×0.80, measured **+6.23pp** and
also passed, while **Diwali — the one real effect — did not** (interval
[−14.1, +1.6]).

Nine holidays tested at 95% confidence, each with a single occurrence: about
five came out 'significant' and most are artifacts. That is a textbook
multiple-comparisons problem, and it is why a significance filter is not a
safeguard on a small sample.

**3. The real constraint is the data, not the model.** Holidays with a real
planted effect occur *once* in eighteen months, so at any point in the test
window they have no prior occurrence to learn from. Holidays that repeat have
no real effect, so their multipliers are noise. Adjusting by noise adds
variance and removes nothing.

### What was kept

The model stays registered so its loss appears in the published comparison —
hiding a model that lost is the same failure as hiding a losing horizon. It
applies no adjustment where it has no evidence, so it degrades to its baseline
rather than adding noise, and a test asserts it never alters a date with no
holiday nearby.

**What would make it work:** a second full year, so each holiday has a prior
occurrence of its own. Nothing about the method needs to change.

## Interpretation

> Across 11 holidays with measurable adjacent dates, public holidays suppress occupancy at this portfolio. The largest effect is Christmas Day at -20.4pp (-30.4 to -10.3, n=22). That is the inverse of a leisure property and is what a corporate aparthotel should show: business travel stops during festivals. Christmas Day occurs once in this dataset, so those 22 adjacent dates are repeated measurements of a single event rather than independent samples. Treat the direction as the finding and the magnitude as indicative; the interval is narrower than the evidence warrants.
