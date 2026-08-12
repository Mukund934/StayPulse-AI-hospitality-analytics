# Anomaly detection

Day-of-week aware trailing baseline, robust scale via MAD, dual thresholds
(statistical **and** material), with a published false-alert budget.

Isolation Forest was deliberately not used: on a weekly-seasonal univariate
series it flags every weekend and misses the weekday running well below its
own weekday norm, and it produces no baseline, magnitude or explanation — so
its output cannot be acted on.

## False-alert budget

- 24 daily tests (6 metrics x 4 segments)
- Threshold `|z| > 3.5`
- Expected false alerts: **0.33/month**

> At |z| > 3.5 across 24 daily tests, expect about 0.3 false alerts a month. At |z| > 2.0 the same family would produce roughly 33 a month, which is why the threshold is not 2. Residuals are also fatter tailed than normal, so treat this as a floor rather than a forecast.

## Alerts raised

| Metric | Segment | Date | Actual | Baseline | Δ% | z | Confidence | Likely drivers |
|---|---|---|---|---|---|---|---|---|
| `avg_tat` | BLR-KOR/evening | 2025-08-31 | 264.0 | 24.5 | +977.6% | 14.7 | high | — |
| `revenue` | PORTFOLIO | 2025-07-04 | 37,102.6 | 89,015.6 | -58.3% | -12.3 | high | BLR-KOR -36,139.9 (66% of movement); BLR-BTM -18,364.6 (34% of movement) |
| `revenue` | PORTFOLIO | 2025-07-16 | 133,652.1 | 100,567.9 | +32.9% | 12.1 | high | BLR-KOR +20,100.7 (57% of movement); BLR-BTM +14,975.7 (43% of movement) |
| `adr_inr` | PORTFOLIO | 2025-03-21 | 3,834.3 | 4,527.2 | -15.3% | -11.6 | medium | BLR-KOR -1,149.9 (73% of movement); BLR-BTM -427.9 (27% of movement) |
| `avg_tat` | BLR-KOR/evening | 2025-12-15 | 96.0 | 13.2 | +624.5% | 10.6 | high | — |
| `avg_tat` | BLR-KOR/evening | 2026-01-19 | 228.0 | 26.0 | +776.9% | 10.5 | high | — |
| `adr_inr` | PORTFOLIO | 2025-10-18 | 4,900.0 | 4,391.7 | +11.6% | 10.1 | high | BLR-KOR +720.7 (83% of movement); BLR-BTM +151.7 (17% of movement) |
| `avg_tat` | BLR-KOR/evening | 2025-12-07 | 175.0 | 30.8 | +469.1% | 9.7 | high | — |
| `revenue` | PORTFOLIO | 2025-06-29 | 57,488.8 | 84,756.8 | -32.2% | -9.4 | high | BLR-KOR -28,475.6 (82% of movement); BLR-BTM +6,265.9 (18% of movement) |
| `revenue` | PORTFOLIO | 2026-06-05 | 167,310.7 | 130,381.7 | +28.3% | 9.1 | high | BLR-HSR +20,242.3 (55% of movement); BLR-BTM +15,545.0 (42% of movement); BLR-KOR +984.4 (3% of movement) |
| `occupancy_pct` | PORTFOLIO | 2026-07-29 | 65.0 | 88.6 | -26.7% | -8.9 | high | BLR-BTM -43.5 (55% of movement); BLR-HSR -27.8 (35% of movement); BLR-KOR -7.1 (9% of movement) |
| `avg_tat` | BLR-KOR/evening | 2026-05-07 | 274.0 | 34.0 | +705.9% | 8.9 | high | — |
| `occupancy_pct` | PORTFOLIO | 2025-12-27 | 51.7 | 77.6 | -33.4% | -8.6 | high | BLR-BTM -37.5 (84% of movement); BLR-KOR -7.1 (16% of movement) |
| `revenue` | PORTFOLIO | 2026-07-29 | 116,784.3 | 155,056.2 | -24.7% | -8.5 | high | BLR-BTM -28,395.1 (67% of movement); BLR-HSR -9,449.3 (22% of movement); BLR-KOR -4,326.2 (10% of movement) |
| `avg_tat` | BLR-KOR/evening | 2026-04-25 | 87.0 | 28.0 | +210.7% | 8.0 | high | — |
| `occupancy_pct` | PORTFOLIO | 2025-09-22 | 100.0 | 75.0 | +33.3% | 7.9 | high | BLR-KOR +33.5 (61% of movement); BLR-BTM +21.9 (39% of movement) |
| `avg_tat` | BLR-KOR/evening | 2026-03-02 | 269.0 | 29.5 | +811.9% | 7.9 | high | — |
| `revenue` | PORTFOLIO | 2026-07-30 | 104,338.7 | 152,696.1 | -31.7% | -7.5 | high | BLR-BTM -32,886.8 (68% of movement); BLR-HSR -8,180.0 (17% of movement); BLR-KOR -7,576.3 (16% of movement) |
| `adr_inr` | PORTFOLIO | 2025-06-28 | 3,861.9 | 4,398.6 | -12.2% | -7.0 | high | BLR-KOR -664.8 (61% of movement); BLR-BTM -433.8 (39% of movement) |
| `revenue` | PORTFOLIO | 2025-07-18 | 117,399.8 | 89,015.6 | +31.9% | 6.7 | high | BLR-KOR +18,582.1 (62% of movement); BLR-BTM +11,295.3 (38% of movement) |

## Ground-truth verification

| Planted signal | Expected | Result |
|---|---|---|
| **F1** Koramangala evening housekeeping degradation | detected | detected |
| **F2** business-date drift (9 weeks) | detected | detected via `DQ040` |
| **F3** WhatsApp integration outage (9 days) | detected | detected via `DQ051` run-length rule |
| **D1** channel-mix decoy | **no alarm** | no alarm raised |

The decoy is the meaningful test. It looks like a revenue problem and is not:
corporate mix rose, rate held, RevPAR held. A detector that alarms on it
sends Operations chasing a pricing decision that was never made.

Note that F2 and F3 are caught by **deterministic rules**, not the
statistical detector — and that is the correct division of labour. A stored
date disagreeing with its derived truth is a correctness bug, and a feed that
has stopped is a run-length question. Neither is an outlier problem, and
reaching for a z-score on either would be worse engineering.
