# Cancellation risk and overbooking

_Generated 2026-08-19T06:30:49+00:00._

## What is predicted, and what is not

The obvious target is **wash** -- cancelled or no-show -- because that is
the number an overbooking policy consumes. It is the wrong thing to model,
and the generator says why.

Cancellation has a mechanism:

```
p_cancel = clip(channel.cancel_rate * (1 + 0.55*tanh((lead_days-10)/14)),
                0.01, 0.62)
```

No-show does not:

```
elif rng.random() < 0.014
```

a flat 1.4% applied to every booking that survived the cancellation draw,
independent of channel, lead time, price and length of stay. **No model can
predict it.** Pooling the two would dilute a real signal with a constant, so
this models cancellation and demonstrates the unlearnability of no-show
rather than asserting it.

Fitted on the same features, no-show scores **AUC 0.5269** -- a
coin toss -- against an observed rate of 1.67%
versus the planted 1.4%. That is the correct
result, not a modelling failure.

## Cancellation model

Logistic regression, L2, 18 booking-time features.

- **Split:** temporal -- train on bookings made before 2026-03-31 (3917 bookings), test on the 1311 made after.
- **Base rate:** 12.36%
- **AUC:** 0.6737
- **Precision:** 20.6% at a threshold equal to the base rate
- **Recall:** 65.4%
- **Lift over base rate:** 1.67x
- **Brier score:** 0.1048

**Accuracy is deliberately not reported.** On this base rate a model that
predicts 'never cancels' is around 88% accurate and completely useless.

### Calibration

Weighted mean absolute error: **2.03pp**.

| Predicted band | Bookings | Mean predicted | Observed | Gap |
|---|---:|---:|---:|---:|
| 2.7-6.8% | 344 | 5.32% | 4.36% | -0.96pp |
| 6.8-10.9% | 345 | 8.52% | 8.12% | -0.4pp |
| 10.9-15.0% | 124 | 12.97% | 12.9% | -0.07pp |
| 15.0-19.1% | 172 | 16.99% | 17.44% | +0.45pp |
| 19.1-23.3% | 137 | 20.95% | 23.36% | +2.41pp |
| 23.3-27.4% | 93 | 25.25% | 27.96% | +2.71pp |
| 27.4-31.5% | 67 | 29.26% | 13.43% | -15.83pp |
| 31.5-35.6% | 18 | 33.22% | 11.11% | -22.11pp |
| 35.6-39.7% | 10 | 37.46% | 40.0% | +2.54pp |
| 39.7-43.9% | 1 | 43.87% | 0.0% | -43.87pp |

**The weighting is not a detail.** An unweighted mean over these bins
reports 9.13pp, because a bin holding a single booking counts as much as
one holding 344. Weighted by population it is 2.03pp. This is the same
failure recorded in PART L-14 -- an average over units with very different
weights is not a summary of those units -- caught here in a metric rather
than in a finding.

The upper bins do drift: the model over-predicts above roughly 27%. That is
the temporal drift below arriving as miscalibration.

### Recovery of the planted mechanism

The generator's mechanism is known, so recovery is checkable -- the closest
thing to ground truth this project has.

- **Lead time:** expected increasing, recovered **increasing**
- **Channel ordering:** Spearman **0.905** against the planted rates

| | Ordering |
|---|---|
| Planted | BDC > AGODA > MMT > B2B-HR > AIRBNB > DIRECT > CORP > WALKIN |
| Recovered | BDC > AGODA > B2B-HR > MMT > AIRBNB > WALKIN > DIRECT > CORP |

The channel coefficients are relative to the dropped dummy level, which
sits in the intercept, so their absolute values are not the planted rates.
The **ordering** is the testable claim.

### Temporal drift, and why the split is temporal

Cancellation falls from **16.36%** to
**12.89%** across the record (-3.47pp).

| Booking quarter | Bookings | Cancel rate |
|---|---:|---:|
| 2025Q1 | 599 | 16.36% |
| 2025Q2 | 753 | 15.41% |
| 2025Q3 | 859 | 13.97% |
| 2025Q4 | 809 | 14.59% |
| 2026Q1 | 900 | 12.89% |
| 2026Q2 | 919 | 12.08% |
| 2026Q3 | 380 | 12.89% |

A model fitted on earlier bookings therefore meets a lower base rate when
scored on later ones and over-predicts by construction. **A random split
would mix the eras and hide this entirely**, reporting a calibration the
model does not have when used the way it would actually be used --
forwards.

## Overbooking simulator

### The number this refuses to produce

Every overbooking treatment ends with an optimal level, and it is always
the same arithmetic: accept one more booking while the expected cost of the
extra walk is below the expected cost of the extra empty room. That needs a
**cost ratio** -- the cost of walking a guest relative to an empty room --
and this warehouse does not contain one. There is no relocation cost, no
compensation field, no goodwill model. `walk_in` is a booking channel and
`relocation` is a guest segment; neither is the cost of a walk.

So no level is recommended. What is computed is the outcome distribution at
every level, and the **breakeven ratio** at which each level starts to pay
-- both fully determined by the data.

### Example: 2025-12-11, as of 2025-12-04

28 bookings on the books against 29 sellable rooms, 84.6% survival.

The date is **chosen, not fixed**: most dates in this portfolio are
undersold, and on an undersold date every overbooking level is walk-free,
which demonstrates nothing.

| Overbook by | Accepted | E[arrivals] | P(any walk) | E[walks] | E[empty] | Breakeven ratio |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 28 | 23.69 | 0.0% | 0.0 | 5.312 | n/a |
| 1 | 29 | 24.53 | 0.0% | 0.0 | 4.466 | n/a |
| 2 | 30 | 25.38 | 0.66% | 0.007 | 3.627 | 126.72 |
| 3 | 31 | 26.23 | 3.72% | 0.043 | 2.817 | 22.37 |
| 4 | 32 | 27.07 | 11.03% | 0.147 | 2.075 | 7.09 |
| 5 | 33 | 27.92 | 23.03% | 0.361 | 1.443 | 2.97 |
| 6 | 34 | 28.76 | 38.27% | 0.708 | 0.944 | 1.44 |
| 7 | 35 | 29.61 | 54.24% | 1.191 | 0.581 | 0.75 |
| 8 | 36 | 30.46 | 68.58% | 1.794 | 0.338 | 0.4 |
| 9 | 37 | 31.3 | 79.94% | 2.487 | 0.185 | 0.22 |
| 10 | 38 | 32.15 | 88.03% | 3.245 | 0.097 | 0.12 |

Read the breakeven column as: *overbooking by this much pays off only if
walking a guest costs you less than this many empty rooms.*

### How much the answer depends on the cost you cannot look up

| Cost ratio (walk / empty room) | Recommended overbook |
|---:|---:|
| 1.0 | 6 |
| 2.0 | 5 |
| 5.0 | 4 |
| 10.0 | 3 |
| 20.0 | 3 |
| 50.0 | 2 |

The recommendation moves across the plausible range, which is precisely why
no single figure is published. An operator who prices a walk at twice an
empty room and one who prices it at fifty times get materially different
policies from the same data.

## Limitations

- **No cost of walking a guest.** Recorded in PART H. Until a relocation
  cost per incident exists, the breakeven table is the answer and the
  optimum is not computable.
- **No censoring.** Every booking here has a settled outcome. Real booking
  data contains reservations whose fate is not yet known, and a survival
  model would be the right tool there.
- **The mechanism is known because it was planted.** Recovering it validates
  the method, not the method's performance on real data.
- **Heterogeneity narrows the arrival distribution** for mathematical
  reasons as well as predictive ones. A Poisson-binomial is tighter than the
  binomial with the same mean, so per-booking probabilities justify slightly
  more aggressive overbooking even when they predict no better. Both are
  published so the difference is not mistaken for model quality.
- **One synthetic portfolio.** None of these numbers generalise.
