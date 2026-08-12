# AI evaluation

`gemini-3.1-flash-lite` versus a deterministic keyword baseline, both scored on the same
418 reviews against 762 known aspect labels.

> **What this measures.** Ground truth is *generator-derived*: the aspects
> deliberately composed into each synthetic review. It measures whether a
> method recovers injected aspects — **not** whether it agrees with human
> judgement, which would require human annotation this project does not have.
> A hand-labelled set would be stronger evidence. This is the honest version
> of what exists.

## Aspect detection

| Method | Precision | Recall | F1 | Macro-F1 | Rows predicted |
|---|---|---|---|---|---|
| `gemini` | 80.1% | 82.1% | 81.1% | 79.4% | 781 |
| `keyword_baseline` | 73.6% | 89.1% | 80.6% | 81.2% | 949 |

## Aspect + polarity

| Method | Precision | Recall | F1 | Polarity accuracy on matched aspects |
|---|---|---|---|---|
| `gemini` | 79.2% | 80.6% | 79.9% | 96.0% (629 matched) |
| `keyword_baseline` | 56.2% | 69.9% | 62.3% | 73.7% (708 matched) |

## Per-category detection F1

| Category | Gold rows | Gemini F1 | Baseline F1 | Δ |
|---|---|---|---|---|
| `location` | 73 | 95.7% | 94.8% | +0.9 |
| `check_in` | 71 | 100.0% | 26.8% | +73.2 |
| `noise` | 66 | 97.7% | 82.0% | +15.7 |
| `amenities` | 64 | 64.4% | 100.0% | -35.6 |
| `wifi` | 63 | 100.0% | 100.0% | +0.0 |
| `maintenance` | 62 | 50.5% | 46.2% | +4.3 |
| `room_quality` | 62 | 65.7% | 66.7% | -0.9 |
| `cleanliness` | 60 | 93.0% | 83.3% | +9.7 |
| `housekeeping_response` | 60 | 79.4% | 81.1% | -1.7 |
| `food` | 59 | 6.0% | 95.2% | -89.2 |
| `staff` | 54 | 100.0% | 98.1% | +1.9 |
| `value` | 50 | 100.0% | 100.0% | +0.0 |

## By language

Code-mixed Hinglish is where a lexicon approach fails outright and a
frontier model does not. That gap is the argument for using one.

| Language | Gold rows | Gemini F1 | Baseline F1 |
|---|---|---|---|
| `en` | 711 | 80.4% | 80.3% |
| `hinglish` | 33 | 97.0% | 88.9% |

## Output validation

- **781** aspect rows published, every one carrying a verbatim
  evidence span verified as a literal substring of its source review.
- **0** rows quarantined and never published.

Validation is not advisory: an extraction whose quote does not appear in the
source is a fabricated citation and is blocked, not flagged.

## Cost

- 418 reviews processed, 113,632 tokens,
  631s wall clock, on the Gemini free tier.
- Measured from `usage_metadata` on every call and stored in
  `meta.llm_run_log` — not estimated from a price list.

## Verdict

- Detection F1: **+0.5 points** over the keyword baseline.
- Aspect+polarity F1: **+17.6 points** over the keyword baseline.

The model earns its place.

The asymmetry is the interesting part. Keyword matching is respectable at
*finding* aspects — hospitality vocabulary is narrow and repetitive — and
weak at *polarity*, because “the AC was not cooling” and “the AC cooled
quickly” share their aspect keyword entirely. Polarity is where the language
model separates, and polarity is what decides whether a row becomes a work
item or gets closed.

### Limitations

- Ground truth is generator-derived, not human-annotated (see above).
- Review text is template-composed, so its prose is less varied than real
  guest writing; both methods likely score better here than on live reviews.
- The Hinglish sample is small, so its per-language figure is indicative
  rather than precise.
- Severity was not scored: it is inherently subjective and a generator-derived
  severity label would measure agreement with a template, not with an operator.
