# Why did RevPAR change?

_Generated 2026-08-15 10:24 UTC._

A deterministic decomposition. **No language model participates in finding,
ranking or naming a cause** — a test asserts the module imports none. Every
figure below is arithmetic on the warehouse and reproducible from it.

## Method

`RevPAR = Occupancy × ADR` is multiplicative, so splitting a movement into
'how much was volume' and 'how much was rate' is genuinely ambiguous — there
is an interaction term and it has to go somewhere. Assigning it to one factor
flatters whichever you pick. This uses the **symmetric (Shapley) split**,
which distributes it evenly:

```
occupancy contribution = Δ(Occ) × mean(ADR before, ADR after)
rate contribution      = Δ(ADR) × mean(Occ before, Occ after)
```

Those sum to the total movement with **no residual**, asserted to 0.01 INR.

### Attributing a ratio

Revenue attribution cannot decompose RevPAR, and getting this wrong produces
a confident wrong answer. The first version of this engine attributed the
revenue change and narrated it as RevPAR. On the case below it named HSR
Layout as the driver of an 18% RevPAR **decline** while HSR's revenue had
**risen** by ₹341,858 — and gave it a 134% share.

Both absurdities have one cause: the portfolio added 31.5% more sellable
inventory that month, so revenue rose while RevPAR fell. Attributing the
numerator explains nothing about the ratio.

Portfolio RevPAR is now written as a capacity-weighted average of each
member's own RevPAR and split exactly into a **capacity-mix effect** and a
**performance effect**. Channels keep a revenue attribution, clearly labelled
as such, because no rooms are allocated to Booking.com — inventing a
per-channel denominator to print a tidier number would be a fabrication.

---

## Worked example — 2026-03, the largest decline in the series

```
WHY DID REVPAR CHANGE?
  2026-03-01 .. 2026-03-31  vs  2026-01-29 .. 2026-02-28

  RevPAR  3,662 -> 2,996 INR   -18.2%

    occupancy   79.4% -> 68.7%  (-10.7pp)   contributes -478 INR
    ADR         4,613 -> 4,359 INR  (-5.5%)   contributes -188 INR

  CAPACITY    rooms available 917 -> 1,206  (+31.5%)

  DRIVERS  (contribution to the RevPAR movement, INR)
    dimension  member                             total   capacity-mix  performance
    property   StayPulse Residences BTM Layout      -508         -447          -61   (76%)
    property   StayPulse Residences Koramangala      -442         -444           +3   (66%)
    day_type   Weekday                             -439          +18         -457   (66%)
    property   StayPulse Residences HSR Layout      +283         +142         +142   (-43%)
    day_type   Weekend                             -227          -17         -210   (34%)

  CHANNEL  (revenue movement, INR -- channels hold no inventory,
            so they cannot be given a RevPAR)
    Direct                             +242,644       95% of revenue change     +46 nights
    MakeMyTrip                          -73,454      -29% of revenue change     -10 nights
    Airbnb                              +53,820       21% of revenue change     +16 nights
    Agoda                               +30,320       12% of revenue change      +9 nights
    Walk-in                             +20,002        8% of revenue change      +4 nights
    Corporate                            -9,775       -4% of revenue change     +28 nights

  ADR: rate effect -285 INR, mix effect +16 INR
       predominantly rate: channels changed what they charged

  PRIMARY SIGNAL   RevPAR fell 18.2%, occupancy-led (72% of the movement); but sellable inventory changed +31.5% between the windows, contributing -749 INR of the movement through capacity mix alone; concentrated in Weekday (66% of the gross day_type movement).
  CONFIDENCE       medium  (13 segment comparisons)
  NOTE             Sellable inventory changed +31.5% between the two windows. RevPAR is revenue per available room, so part of this movement is capacity, not trading. Read the capacity-mix and performance effects separately below.
  NOTE             ADR movement: predominantly rate: channels changed what they charged.
  NOTE             Attribution is descriptive, not causal. It identifies where the movement occurred, not why demand behaved as it did.
```

### What this says

The headline movement is real, but it is not primarily a commercial failure.
Sellable inventory grew 31.5% between the two windows, and RevPAR is revenue
per *available* room — opening rooms faster than demand fills them lowers it
by arithmetic. The engine detects the capacity change, surfaces it in the
headline, separates each property's capacity-mix effect from its trading
performance, and **caps its own confidence** because a capacity-driven
movement is a weaker commercial claim than a demand-driven one.

---

## Trailing 30 days

```
WHY DID REVPAR CHANGE?
  2026-07-13 .. 2026-08-11  vs  2026-06-13 .. 2026-07-12

  RevPAR  3,485 -> 3,571 INR   +2.5%

    occupancy   79.0% -> 81.5%  (+2.5pp)   contributes +109 INR
    ADR         4,412 -> 4,384 INR  (-0.6%)   contributes -23 INR

  DRIVERS  (contribution to the RevPAR movement, INR)
    dimension  member                             total   capacity-mix  performance
    day_type   Weekday                             +278         +260          +18   (322%)
    day_type   Weekend                             -191         -231          +40   (-222%)
    property   StayPulse Residences HSR Layout      +107          -14         +121   (124%)
    property   StayPulse Residences BTM Layout       -39           +9          -48   (-46%)
    property   StayPulse Residences Koramangala       +18          +14           +5   (21%)

  CHANNEL  (revenue movement, INR -- channels hold no inventory,
            so they cannot be given a RevPAR)
    Corporate                          -639,643     -767% of revenue change    -148 nights
    Airbnb                             +188,188      226% of revenue change     +49 nights
    Direct                             +165,706      199% of revenue change     +39 nights
    Booking.com                        +155,870      187% of revenue change     +39 nights
    MakeMyTrip                         +132,813      159% of revenue change     +23 nights
    Agoda                               +65,773       79% of revenue change     +14 nights

  ADR: rate effect +22 INR, mix effect -16 INR
       rate and mix both contributed materially

  PRIMARY SIGNAL   RevPAR rose 2.5%, occupancy-led (occupancy alone accounts for 126%; the other component moved the opposite way and offset part of it); concentrated in StayPulse Residences HSR Layout (65% of the gross property movement).
  CONFIDENCE       high  (13 segment comparisons)
  NOTE             ADR movement: rate and mix both contributed materially.
  NOTE             Attribution is descriptive, not causal. It identifies where the movement occurred, not why demand behaved as it did.
```

---

## Guardrails

- **Concentration is measured against gross movement, not net.** Two 30-day
  windows contain different numbers of weekends, so weekday and weekend
  effects came out at +260 and −231 INR — almost cancelling — and an earlier
  version announced 'concentrated in Weekday (322% of the movement)'. Shares
  are now bounded in 0–100% and the engine discloses when contributions
  largely offset instead of picking a winner out of noise.
- **Movements under 1% get no root cause.** A 0.3% wobble is not a finding.
- **Every explanation carries a causality caveat.** Attribution identifies
  *where* a movement occurred, never *why* demand behaved as it did.
