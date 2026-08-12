"""Run aspect extraction over guest reviews and persist validated output.

Only unprocessed reviews are sent, so re-running costs nothing and free-tier quota
is not burned re-classifying text that has not changed. Results are persisted keyed
by review, so a number on a dashboard cannot change on refresh.

Usage:
    python scripts/run_ai_pipeline.py --limit 300
    python scripts/run_ai_pipeline.py --all
    python scripts/run_ai_pipeline.py --baseline-only
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from sqlalchemy import text  # noqa: E402

from staypulse import db  # noqa: E402
from staypulse.ai import baseline  # noqa: E402
from staypulse.ai.client import GeminiExtractor  # noqa: E402

BATCH = 6


def pending_reviews(limit: int | None, *, recent_first: bool = False) -> list[dict]:
    # recent_first matters operationally: the daily brief reports guest issues over
    # a trailing 30-day window, so extracting oldest-first leaves the brief blank
    # however many reviews have been processed.
    order = "r.review_date DESC, r.review_key DESC" if recent_first else "r.review_key"
    sql = f"""
        SELECT r.review_id, r.review_key, r.property_key, r.review_date, r.review_text
        FROM mart.fact_review r
        WHERE r.review_text IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM mart.fact_review_aspect a
                          WHERE a.review_key = r.review_key)
        ORDER BY {order}
    """
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.fetch_all(sql)


def persist(accepted: list[dict], quarantined: list[dict], key_map: dict[str, int]) -> None:
    with db.connect() as conn:
        for row in accepted:
            rk = key_map.get(row["review_id"])
            if rk is None:
                continue
            conn.execute(text("""
                INSERT INTO mart.fact_review_aspect
                    (review_key, property_key, review_date, category, polarity,
                     severity, confidence, actionable_by, evidence_span,
                     evidence_verified, model)
                VALUES (:rk, :pk, :rd, :cat, :pol, :sev, :conf, :act, :ev, true, :model)
            """), {
                "rk": rk, "pk": row["property_key"], "rd": row["review_date"],
                "cat": row["category"], "pol": row["polarity"], "sev": row["severity"],
                "conf": row["confidence"], "act": row["actionable_by"],
                "ev": row["evidence_span"][:2000], "model": row["model"],
            })
        for q in quarantined:
            conn.execute(text("""
                INSERT INTO meta.absa_quarantine
                    (review_id, model, category, polarity, evidence_span, reason, raw_payload)
                VALUES (:rid, :m, :cat, :pol, :ev, :why, CAST(:raw AS jsonb))
            """), {
                "rid": q["review_id"], "m": q["model"], "cat": q.get("category"),
                "pol": q.get("polarity"),
                "ev": (q.get("evidence_span") or None),
                "why": q["reason"], "raw": q.get("raw_payload"),
            })


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=300)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--baseline-only", action="store_true")
    ap.add_argument("--recent-first", action="store_true",
                    help="process newest reviews first (what the daily brief needs)")
    args = ap.parse_args()

    reviews = pending_reviews(None if args.all else args.limit,
                             recent_first=args.recent_first)
    print("=" * 80)
    print("  StayPulse - aspect-based guest feedback extraction")
    print(f"  {len(reviews)} unprocessed reviews (cached results are never re-sent)")
    print("=" * 80)

    if not reviews:
        print("\n  Nothing to do. All reviews already extracted.")
        return 0

    key_map = {r["review_id"]: r["review_key"] for r in reviews}

    # ---- deterministic baseline (free, instant, no API) -------------------
    base_rows = []
    for r in reviews:
        for a in baseline.classify(r["review_text"]):
            base_rows.append({**a, "review_id": r["review_id"]})
    out = PROJECT_ROOT / "data" / "marts" / "baseline_aspects.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(base_rows, indent=1, default=str), encoding="utf-8")
    print(f"\n  keyword baseline : {len(base_rows):,} aspect rows -> {out.name}")

    if args.baseline_only:
        return 0

    # ---- Gemini -----------------------------------------------------------
    extractor = GeminiExtractor()
    print(f"  model            : {extractor.model}")

    run_id = None
    with db.connect() as conn:
        run_id = conn.execute(text(
            "INSERT INTO meta.pipeline_run (pipeline, notes) "
            "VALUES ('ai_absa', :n) RETURNING run_id"
        ), {"n": f"{len(reviews)} reviews, batch={BATCH}"}).scalar_one()

    t0 = time.perf_counter()
    tot_accepted, tot_quarantined = [], []
    tokens_in = tokens_out = tokens_all = 0
    calls = errors = 0

    for i in range(0, len(reviews), BATCH):
        chunk = reviews[i:i + BATCH]
        res = extractor.extract_batch(chunk)
        tot_accepted += res.accepted
        tot_quarantined += res.quarantined
        tokens_in += res.prompt_tokens
        tokens_out += res.output_tokens
        tokens_all += res.total_tokens
        calls += res.api_calls
        errors += res.api_errors
        done = min(i + BATCH, len(reviews))
        print(f"    {done:>4}/{len(reviews)}  accepted={len(tot_accepted):<5} "
              f"quarantined={len(tot_quarantined):<4} tokens={tokens_all:,}", flush=True)

    elapsed = time.perf_counter() - t0
    persist(tot_accepted, tot_quarantined, key_map)

    total_rows = len(tot_accepted) + len(tot_quarantined)
    q_rate = round(100.0 * len(tot_quarantined) / total_rows, 2) if total_rows else 0.0

    with db.connect() as conn:
        conn.execute(text("""
            INSERT INTO meta.llm_run_log
                (feature, model, records_in, records_ok, records_quarantined,
                 prompt_tokens, output_tokens, total_tokens, wall_clock_s, notes)
            VALUES ('absa_extraction', :m, :n, :ok, :q, :pt, :ot, :tt, :w, :notes)
        """), {
            "m": extractor.model, "n": len(reviews), "ok": len(tot_accepted),
            "q": len(tot_quarantined), "pt": tokens_in, "ot": tokens_out,
            "tt": tokens_all, "w": round(elapsed, 2),
            "notes": f"batch={BATCH} calls={calls} api_errors={errors}",
        })
        conn.execute(text(
            "UPDATE meta.pipeline_run SET finished_at=now(), status=:s, "
            "rows_out=:o, rows_rejected=:r WHERE run_id=:id"
        ), {"s": "success" if errors == 0 else "partial",
            "o": len(tot_accepted), "r": len(tot_quarantined), "id": run_id})

    print(f"\n  {'-' * 76}")
    print(f"  reviews sent        : {len(reviews):,}")
    print(f"  API calls           : {calls}   errors: {errors}")
    print(f"  aspects accepted    : {len(tot_accepted):,}")
    print(f"  quarantined         : {len(tot_quarantined):,}  ({q_rate}%)")
    if tot_quarantined:
        reasons: dict[str, int] = {}
        for q in tot_quarantined:
            reasons[q["reason"]] = reasons.get(q["reason"], 0) + 1
        for why, n in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"      {why:<38} {n}")
    print(f"  tokens              : in {tokens_in:,} / out {tokens_out:,} / total {tokens_all:,}")
    print(f"  wall clock          : {elapsed:,.1f}s")
    print(f"  {'-' * 76}")
    print("  Only evidence-verified rows were written to mart.fact_review_aspect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
