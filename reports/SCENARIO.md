# Scenario engine

_Generated 2026-08-19T15:45:12+00:00._

## A scenario is not a forecast

This is the distinction the whole feature rests on.

A **forecast** answers *what is going to happen*. It carries a model, it
can be scored against reality, and this project scores it -- with an 80%
interval whose measured out-of-sample coverage is 82.6%.

A **scenario** answers *what would the books say if occupancy were five
points higher*. It carries no model, predicts nothing, and cannot be right
or wrong. Its entire value is that it is exact.

Confusing the two is how a what-if tool becomes dishonest: a number
produced by holding ADR fixed and moving occupancy gets presented as a
projection, and the reader assumes someone believes it will happen. Every
result here is labelled `scenario`, states what it held constant, and never
claims the change is achievable. A test scans the output for forecast
vocabulary and fails if it appears.

## Baseline

| Measure | Value |
|---|---:|
| Rooms available | 17,949 |
| Rooms sold | 13,610 |
| Occupancy | 75.826% |
| ADR | 4,414.54 |
| RevPAR | 3,347.37 |
| Room revenue | 60,081,892.69 |
| Commission | 4,264,511.59 |
| Net revenue (after commission + GST) | 55,049,769.01 |

`RevPAR = ADR x Occupancy` holds exactly, and a test asserts it in the
baseline and in every scenario result.

## Worked examples

| Scenario | Occupancy | ADR | RevPAR | Change |
|---|---:|---:|---:|---:|
| occupancy +5pp | 80.823% | 4,414.54 | 3,567.98 | +220.62 |
| ADR +5% | 75.826% | 4,635.27 | 3,514.74 | +167.37 |
| occupancy +5pp and ADR +5% | 80.823% | 4,635.27 | 3,746.38 | +399.02 |

### The interaction term is real and it is handled

Occupancy alone gives +220.62. Rate alone gives +167.37. Both
together give +399.02 -- **not** +387.99.

The difference, +11.03, is the interaction:
`RevPAR = ADR x Occupancy` is multiplicative, so selling more nights at a
higher rate earns more than the two effects added. An implementation that
adds them is wrong by exactly this amount, and a test fails if it starts
doing so.

The interaction has to be attributed somewhere. This uses the symmetric
(Shapley) split -- each contribution measured against the *mean* of before
and after -- which is the same convention `analytics.rootcause` already
uses for observed movements. Using a different one would let the two
modules disagree about the same movement.

| Component | Contribution |
|---|---:|
| Occupancy | +226.1317 |
| Rate | +172.8838 |
| **Residual** | **0.0** |

The residual is zero, not small. A test asserts that rather than this
report claiming it.

## Sensitivity

| Occupancy change | RevPAR | Change |
|---:|---:|---:|
| -10pp | 2,905.89 | -441.48 |
| -5pp | 3,126.75 | -220.62 |
| -2pp | 3,259.07 | -88.3 |
| +2pp | 3,435.66 | +88.3 |
| +5pp | 3,567.98 | +220.62 |
| +10pp | 3,788.85 | +441.48 |

| ADR change | RevPAR | Change |
|---:|---:|---:|
| -10% | 3,012.63 | -334.74 |
| -5% | 3,180.0 | -167.37 |
| -2% | 3,280.42 | -66.95 |
| +2% | 3,414.31 | +66.95 |
| +5% | 3,514.74 | +167.37 |
| +10% | 3,682.1 | +334.74 |

One lever at a time, each holding the other constant. Reading two rows
together and adding them understates the result, for the reason above.

## Channel mix -- the one lever with measured economics

Every other lever here is arithmetic on an identity. This one is priced
from real data: commission per occupied night is **measured**, and it
differs enormously by channel.

| Channel | Nights | ADR | Commission/night | Net/night |
|---|---:|---:|---:|---:|
| CORP | 5,532 | 4,473.71 | 0.0 | 4,473.71 |
| DIRECT | 2,259 | 4,555.69 | 0.0 | 4,555.69 |
| MMT | 1,817 | 4,501.31 | 900.26 | 3,439.0 |
| BDC | 1,586 | 4,512.43 | 767.11 | 3,607.24 |
| AIRBNB | 1,163 | 4,564.97 | 684.75 | 3,756.97 |
| AGODA | 689 | 4,494.99 | 809.1 | 3,540.25 |
| B2B-HR | 380 | 1,277.72 | 153.33 | 1,096.79 |
| WALKIN | 184 | 4,427.99 | 0.0 | 4,427.99 |

**Example.** Moving 25% of MMT nights to DIRECT -- 454 nights -- changes net revenue by **+506,977.26**, or +1,116.69 per night.

Almost all of that is commission, not rate: the two channels charge
similar ADR, but one pays the OTA and one does not.

### Why this is still a scenario and not a plan

It assumes **the demand transfers** -- that a guest who booked through an
OTA would have booked direct if the OTA had not been there. Nothing in
this warehouse supports that, and for some channels it is plainly false: a
walk-in is a walk-in because they walked in.

It also excludes **acquisition cost**. Commission is recorded; the
marketing spend needed to move a booking direct is not. The saving shown
is gross of whatever it would cost to achieve, which for a real shift of
this size would not be nothing.

## What this engine cannot do

- Say how to achieve any of these changes.
- Say what a rate change would cost in volume, or what selling more nights would do to the achieved rate. That needs price elasticity, which this warehouse does not contain.
- Claim any of the revenue shown is capturable.

This is the same gap that stops `opportunity_signals` naming a price and
stops the overbooking simulator naming a level. There is no price
elasticity and no demand response in this warehouse, so the engine can say
what the books would show, and cannot say how to get there or whether the
revenue is capturable.
