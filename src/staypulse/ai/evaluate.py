"""Evaluation: does the AI actually add value, and can its output be trusted?

Scored against `meta.review_aspect_truth` — the aspects known to have been composed
into each review.

WHAT THIS MEASURES, stated plainly because the distinction is load-bearing: whether
a method recovers deliberately injected aspects. It is GENERATOR ground truth, not
human annotation. It does not measure agreement with human judgement, and no claim
of that kind is made anywhere. A hand-labelled set would be stronger evidence; this
is the honest version of what exists.

Two scoring levels, because they answer different questions:
  DETECTION  did the method find the right aspect at all?  (category match)
  POLARITY   did it also get the direction right?          (category + polarity)

Reporting only the second understates a method that finds the issue but mislabels
tone; reporting only the first hides the failure mode that actually matters
operationally.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class Score:
    n_gold: int
    tp: int
    fp: int
    fn: int

    @property
    def precision(self) -> float:
        return 100.0 * self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return 100.0 * self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def _pairs(rows: list[dict], *, with_polarity: bool) -> set[tuple]:
    if with_polarity:
        return {(r["review_id"], r["category"], r["polarity"]) for r in rows}
    return {(r["review_id"], r["category"]) for r in rows}


def score(predicted: list[dict], gold: list[dict], *, with_polarity: bool) -> Score:
    p = _pairs(predicted, with_polarity=with_polarity)
    g = _pairs(gold, with_polarity=with_polarity)
    return Score(n_gold=len(g), tp=len(p & g), fp=len(p - g), fn=len(g - p))


def score_by(
    predicted: list[dict], gold: list[dict], key: str, *, with_polarity: bool
) -> dict[str, Score]:
    """Per-category (or per-language) breakdown.

    A macro average over these is the honest headline: a micro average is dominated
    by whichever category happens to be most frequent.
    """
    keys = {r[key] for r in gold if r.get(key)} | {r[key] for r in predicted if r.get(key)}
    out: dict[str, Score] = {}
    for k in sorted(keys):
        p = [r for r in predicted if r.get(key) == k]
        g = [r for r in gold if r.get(key) == k]
        out[k] = score(p, g, with_polarity=with_polarity)
    return out


def macro_f1(scores: dict[str, Score]) -> float:
    """Unweighted mean F1 across classes, ignoring classes with no gold examples."""
    vals = [s.f1 for s in scores.values() if s.n_gold > 0]
    return sum(vals) / len(vals) if vals else 0.0


def confusion_by_category(predicted: list[dict], gold: list[dict]) -> list[dict]:
    """Per-category TP/FP/FN, ordered by gold frequency."""
    per = score_by(predicted, gold, "category", with_polarity=False)
    rows = [{
        "category": cat,
        "n_gold": s.n_gold,
        "tp": s.tp, "fp": s.fp, "fn": s.fn,
        "precision_pct": round(s.precision, 1),
        "recall_pct": round(s.recall, 1),
        "f1_pct": round(s.f1, 1),
    } for cat, s in per.items()]
    return sorted(rows, key=lambda r: -r["n_gold"])


def polarity_confusion(predicted: list[dict], gold: list[dict]) -> dict:
    """Where a correctly-detected aspect got the wrong polarity.

    This is the interesting failure: the method saw the issue and misread the tone,
    which is operationally worse than missing it, because a negative read as
    positive silently closes a work item.
    """
    gold_pol = {(r["review_id"], r["category"]): r["polarity"] for r in gold}
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    matched = 0
    for r in predicted:
        k = (r["review_id"], r["category"])
        if k in gold_pol:
            matched += 1
            matrix[(gold_pol[k], r["polarity"])] += 1
    correct = sum(v for (g, p), v in matrix.items() if g == p)
    return {
        "matched_aspects": matched,
        "polarity_correct": correct,
        "polarity_accuracy_pct": round(100.0 * correct / matched, 1) if matched else 0.0,
        "matrix": {f"gold={g} -> pred={p}": v for (g, p), v in sorted(matrix.items())},
    }
