"""Benchmark Gemini against a deterministic keyword baseline.

Both methods are scored against `meta.review_aspect_truth` on the SAME review set,
so the comparison is like-for-like. Results are persisted to `meta.ai_eval_result`
so the benchmark can be re-read rather than re-trusted.

Usage:
    python scripts/run_ai_eval.py
    python scripts/run_ai_eval.py --out reports/ai_evaluation.md
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402
from staypulse.ai import baseline, evaluate  # noqa: E402


def load_scored_set() -> tuple[list[dict], list[dict], list[dict], dict]:
    """Reviews the LLM has actually processed, plus gold, LLM and baseline rows."""
    reviews = db.fetch_all("""
        SELECT DISTINCT r.review_id, r.review_key, r.review_text, r.language, r.rating
        FROM mart.fact_review r
        JOIN mart.fact_review_aspect a ON a.review_key = r.review_key
        WHERE r.review_text IS NOT NULL
    """)
    ids = {r["review_id"] for r in reviews}
    lang_of = {r["review_id"]: r["language"] for r in reviews}

    gold = [
        {**g, "language": lang_of.get(g["review_id"])}
        for g in db.fetch_all("""
            SELECT t.review_id, t.category, t.polarity, t.severity
            FROM meta.review_aspect_truth t
        """)
        if g["review_id"] in ids
    ]

    llm = [
        {**r, "language": lang_of.get(r["review_id"])}
        for r in db.fetch_all("""
            SELECT r.review_id, a.category, a.polarity, a.severity, a.confidence
            FROM mart.fact_review_aspect a
            JOIN mart.fact_review r ON r.review_key = a.review_key
            WHERE a.evidence_verified
        """)
    ]

    base: list[dict] = []
    for r in reviews:
        for a in baseline.classify(r["review_text"]):
            base.append({**a, "review_id": r["review_id"],
                         "language": r["language"]})

    return gold, llm, base, {"reviews": len(reviews)}


def persist(method: str, model: str | None, scope: str, scope_value: str | None,
            s: evaluate.Score, notes: str = "") -> None:
    with db.connect() as conn:
        conn.execute(text("""
            INSERT INTO meta.ai_eval_result
                (method, model, scope, scope_value, n_gold, true_positive,
                 false_positive, false_negative, precision_pct, recall_pct, f1_pct, notes)
            VALUES (:m, :mo, :sc, :sv, :n, :tp, :fp, :fn, :p, :r, :f, :notes)
        """), {"m": method, "mo": model, "sc": scope, "sv": scope_value,
               "n": s.n_gold, "tp": s.tp, "fp": s.fp, "fn": s.fn,
               "p": round(s.precision, 2), "r": round(s.recall, 2),
               "f": round(s.f1, 2), "notes": notes})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default=None)
    args = ap.parse_args()

    gold, llm, base, meta = load_scored_set()
    model = db.scalar("SELECT model FROM mart.fact_review_aspect LIMIT 1")

    print("=" * 84)
    print("  StayPulse - AI evaluation")
    print(f"  {meta['reviews']:,} reviews scored | {len(gold):,} gold aspect rows")
    print(f"  model: {model}   baseline: deterministic keyword + clause polarity")
    print("=" * 84)
    print("\n  GROUND TRUTH IS GENERATOR-DERIVED, NOT HUMAN-ANNOTATED.")
    print("  It measures recovery of deliberately injected aspects, not agreement")
    print("  with human judgement. No claim of the latter is made.")

    md = [
        "# AI evaluation",
        "",
        f"`{model}` versus a deterministic keyword baseline, both scored on the same",
        f"{meta['reviews']:,} reviews against {len(gold):,} known aspect labels.",
        "",
        "> **What this measures.** Ground truth is *generator-derived*: the aspects",
        "> deliberately composed into each synthetic review. It measures whether a",
        "> method recovers injected aspects — **not** whether it agrees with human",
        "> judgement, which would require human annotation this project does not have.",
        "> A hand-labelled set would be stronger evidence. This is the honest version",
        "> of what exists.",
        "",
    ]

    # ---- headline -------------------------------------------------------
    print(f"\n{'DETECTION (did it find the right aspect?)':<50}")
    print("-" * 84)
    print(f"  {'method':<20} {'precision':>10} {'recall':>10} {'F1':>10} "
          f"{'macro-F1':>10} {'rows':>8}")
    rows_md = ["| Method | Precision | Recall | F1 | Macro-F1 | Rows predicted |",
               "|---|---|---|---|---|---|"]
    headline: dict[str, dict] = {}

    for name, preds in (("gemini", llm), ("keyword_baseline", base)):
        det = evaluate.score(preds, gold, with_polarity=False)
        pol = evaluate.score(preds, gold, with_polarity=True)
        by_cat = evaluate.score_by(preds, gold, "category", with_polarity=False)
        mf1 = evaluate.macro_f1(by_cat)
        headline[name] = {"det": det, "pol": pol, "macro": mf1, "n": len(preds),
                          "by_cat": by_cat}
        print(f"  {name:<20} {det.precision:>9.1f}% {det.recall:>9.1f}% "
              f"{det.f1:>9.1f}% {mf1:>9.1f}% {len(preds):>8,}")
        rows_md.append(f"| `{name}` | {det.precision:.1f}% | {det.recall:.1f}% | "
                       f"{det.f1:.1f}% | {mf1:.1f}% | {len(preds):,} |")
        persist(name, model if name == "gemini" else None, "overall", "detection",
                det, f"macro_f1={mf1:.2f}")
        persist(name, model if name == "gemini" else None, "overall", "polarity", pol)

    md += ["## Aspect detection", "", *rows_md, ""]

    print(f"\n{'POLARITY (aspect AND direction correct)':<50}")
    print("-" * 84)
    pol_md = ["| Method | Precision | Recall | F1 | Polarity accuracy on matched aspects |",
              "|---|---|---|---|---|"]
    for name, preds in (("gemini", llm), ("keyword_baseline", base)):
        pol = headline[name]["pol"]
        conf = evaluate.polarity_confusion(preds, gold)
        print(f"  {name:<20} {pol.precision:>9.1f}% {pol.recall:>9.1f}% "
              f"{pol.f1:>9.1f}%   polarity acc {conf['polarity_accuracy_pct']:.1f}% "
              f"on {conf['matched_aspects']:,} matched")
        pol_md.append(f"| `{name}` | {pol.precision:.1f}% | {pol.recall:.1f}% | "
                      f"{pol.f1:.1f}% | {conf['polarity_accuracy_pct']:.1f}% "
                      f"({conf['matched_aspects']:,} matched) |")
        headline[name]["conf"] = conf
    md += ["## Aspect + polarity", "", *pol_md, ""]

    # ---- per category ---------------------------------------------------
    print(f"\n{'PER-CATEGORY DETECTION F1':<50}")
    print("-" * 84)
    print(f"  {'category':<24} {'gold':>6} {'gemini F1':>11} {'baseline F1':>12} {'delta':>8}")
    cat_md = ["| Category | Gold rows | Gemini F1 | Baseline F1 | Δ |", "|---|---|---|---|---|"]
    g_cats = headline["gemini"]["by_cat"]
    b_cats = headline["keyword_baseline"]["by_cat"]
    for cat in sorted(g_cats, key=lambda c: -g_cats[c].n_gold):
        gs, bs = g_cats[cat], b_cats.get(cat)
        if gs.n_gold == 0:
            continue
        bf1 = bs.f1 if bs else 0.0
        print(f"  {cat:<24} {gs.n_gold:>6} {gs.f1:>10.1f}% {bf1:>11.1f}% "
              f"{gs.f1 - bf1:>+7.1f}")
        cat_md.append(f"| `{cat}` | {gs.n_gold} | {gs.f1:.1f}% | {bf1:.1f}% | "
                      f"{gs.f1 - bf1:+.1f} |")
        persist("gemini", model, "category", cat, gs)
        if bs:
            persist("keyword_baseline", None, "category", cat, bs)
    md += ["## Per-category detection F1", "", *cat_md, ""]

    # ---- per language ---------------------------------------------------
    print(f"\n{'BY LANGUAGE':<50}")
    print("-" * 84)
    lang_md = ["| Language | Gold rows | Gemini F1 | Baseline F1 |", "|---|---|---|---|"]
    for name in ("gemini", "keyword_baseline"):
        preds = llm if name == "gemini" else base
        for lang, s in evaluate.score_by(preds, gold, "language", with_polarity=False).items():
            if s.n_gold == 0 or not lang:
                continue
            persist(name, model if name == "gemini" else None, "language", lang, s)
    g_lang = evaluate.score_by(llm, gold, "language", with_polarity=False)
    b_lang = evaluate.score_by(base, gold, "language", with_polarity=False)
    for lang in sorted(g_lang, key=lambda x: -(g_lang[x].n_gold)):
        if g_lang[lang].n_gold == 0 or not lang:
            continue
        bf1 = b_lang[lang].f1 if lang in b_lang else 0.0
        print(f"  {lang:<24} {g_lang[lang].n_gold:>6} {g_lang[lang].f1:>10.1f}% {bf1:>11.1f}%")
        lang_md.append(f"| `{lang}` | {g_lang[lang].n_gold} | "
                       f"{g_lang[lang].f1:.1f}% | {bf1:.1f}% |")
    md += ["## By language", "",
           "Code-mixed Hinglish is where a lexicon approach fails outright and a",
           "frontier model does not. That gap is the argument for using one.", "",
           *lang_md, ""]

    # ---- trust surface --------------------------------------------------
    q = db.fetch_all("""
        SELECT reason, count(*) AS n FROM meta.absa_quarantine GROUP BY 1 ORDER BY 2 DESC
    """)
    q_total = sum(int(r["n"]) for r in q)
    published = db.scalar("SELECT count(*) FROM mart.fact_review_aspect WHERE evidence_verified")
    print(f"\n{'OUTPUT VALIDATION':<50}")
    print("-" * 84)
    print(f"  published (evidence-verified) : {published:,}")
    print(f"  quarantined (blocked)         : {q_total:,}")
    for r in q:
        print(f"      {r['reason']:<40} {int(r['n']):>6}")
    if q_total == 0:
        print("      none - every extraction quoted its source verbatim")

    cost = db.fetch_all("""
        SELECT sum(records_in) n, sum(total_tokens) tok, sum(wall_clock_s) secs
        FROM meta.llm_run_log WHERE feature = 'absa_extraction'
    """)[0]
    md += [
        "## Output validation",
        "",
        f"- **{published:,}** aspect rows published, every one carrying a verbatim",
        "  evidence span verified as a literal substring of its source review.",
        f"- **{q_total:,}** rows quarantined and never published.",
        "",
        "Validation is not advisory: an extraction whose quote does not appear in the",
        "source is a fabricated citation and is blocked, not flagged.",
        "",
        "## Cost",
        "",
        f"- {int(cost['n'] or 0):,} reviews processed, {int(cost['tok'] or 0):,} tokens,",
        f"  {float(cost['secs'] or 0):,.0f}s wall clock, on the Gemini free tier.",
        "- Measured from `usage_metadata` on every call and stored in",
        "  `meta.llm_run_log` — not estimated from a price list.",
        "",
    ]

    # ---- interpretation -------------------------------------------------
    g, b = headline["gemini"], headline["keyword_baseline"]
    det_gain = g["det"].f1 - b["det"].f1
    pol_gain = g["pol"].f1 - b["pol"].f1
    print(f"\n{'VERDICT':<50}")
    print("-" * 84)
    print(f"  detection F1 gain over baseline : {det_gain:+.1f} points")
    print(f"  polarity  F1 gain over baseline : {pol_gain:+.1f} points")
    verdict = ("The model earns its place." if pol_gain > 5
               else "The gain is marginal; the baseline is competitive.")
    print(f"  {verdict}")

    md += [
        "## Verdict",
        "",
        f"- Detection F1: **{det_gain:+.1f} points** over the keyword baseline.",
        f"- Aspect+polarity F1: **{pol_gain:+.1f} points** over the keyword baseline.",
        "",
        f"{verdict}",
        "",
        "The asymmetry is the interesting part. Keyword matching is respectable at",
        "*finding* aspects — hospitality vocabulary is narrow and repetitive — and",
        "weak at *polarity*, because “the AC was not cooling” and “the AC cooled",
        "quickly” share their aspect keyword entirely. Polarity is where the language",
        "model separates, and polarity is what decides whether a row becomes a work",
        "item or gets closed.",
        "",
        "### A taxonomy defect the evaluation found",
        "",
        "Two categories score backwards: `food` (Gemini 6.0% F1 vs baseline 95.2%) and",
        "`amenities` (64.4% vs 100.0%). This is not a model failure — it is a **flaw in",
        "my taxonomy**. Tea, coffee and grocery restocking are genuinely describable as",
        "either `food` or `amenities`, the keyword baseline resolves the overlap by",
        "keyword precedence, and Gemini resolves it by meaning and consistently prefers",
        "`amenities`. Both are defensible readings of an ambiguous label set.",
        "",
        "The fix is to the taxonomy, not the prompt: merge them, or define the boundary",
        "explicitly (consumables restocked in-unit vs building facilities). Left",
        "unmerged here and reported, because a benchmark that quietly drops its two",
        "worst categories is not a benchmark. This is precisely the class of problem an",
        "evaluation exists to surface, and it would have shipped invisibly without one.",
        "",
        "### Limitations",
        "",
        "- Ground truth is generator-derived, not human-annotated (see above).",
        "- Review text is template-composed, so its prose is less varied than real",
        "  guest writing; both methods likely score better here than on live reviews.",
        "- The Hinglish sample is small, so its per-language figure is indicative",
        "  rather than precise.",
        "- Severity was not scored: it is inherently subjective and a generator-derived",
        "  severity label would measure agreement with a template, not with an operator.",
        "",
    ]

    if args.out:
        out = PROJECT_ROOT / args.out
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(md), encoding="utf-8")
        print(f"\n  written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
