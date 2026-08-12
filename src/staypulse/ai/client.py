"""Aspect-based guest feedback extraction with Gemini.

Design commitments, each of which exists because the alternative fails:

FIXED TAXONOMY, not topic modelling. Open-ended clustering assigns a large share
of short reviews to an outlier bucket and its topics are unstable run to run, so it
cannot produce a comparable week-over-week trend. An operator wants
"housekeeping_response volume up 34% at BTM this week", which needs a closed label
set.

ASPECT GRAIN, not document sentiment. A review praising the apartment and damning
check-in becomes two rows, not one score. A score cannot be routed to a team. On a
4.8-star corpus a sentiment classifier also returns ~96% positive and surfaces
nothing.

STRING ENUMS ONLY in the schema. Numeric min/max constraints are honoured by some
providers and silently dropped by others, so a 1-5 severity can come back as 7.
Enums are constrained correctly everywhere.

EVIDENCE SPAN ON EVERY ROW, verified as a literal substring of the source review.
This is what turns "validate AI output against source data" from an aspiration into
an automated test. Anything failing is quarantined, never published, and the
quarantine rate is reported.

THE MODEL NEVER PRODUCES A NUMBER. It emits labels and quotes. Every count, rate
and total on any dashboard comes from SQL over those labels.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from staypulse import config

MODEL = "gemini-3.1-flash-lite"

# Closed taxonomy. Adding a category is a deliberate act with a migration, not a
# side effect of a prompt edit.
CATEGORIES = (
    "cleanliness", "housekeeping_response", "staff", "check_in", "room_quality",
    "location", "wifi", "value", "amenities", "maintenance", "noise", "food", "other",
)

TEAMS = ("housekeeping", "front_office", "maintenance", "revenue", "tech", "none")


class Aspect(BaseModel):
    category: Literal[CATEGORIES] = Field(description="Closed taxonomy category.")
    polarity: Literal["positive", "negative", "neutral"]
    severity: Literal["minor", "moderate", "severe", "not_applicable"] = Field(
        description="Operational severity. 'not_applicable' for positive aspects.")
    evidence_span: str = Field(
        description="A VERBATIM substring of the review supporting this aspect. "
                    "Copy it exactly. Do not paraphrase, correct or translate it.")
    actionable_by: Literal[TEAMS] = Field(
        description="Team that would act on this. 'none' for positive or "
                    "non-actionable aspects.")
    confidence: Literal["high", "medium", "low"]


class ReviewAnalysis(BaseModel):
    language: Literal["en", "hinglish", "hi", "other"]
    overall_polarity: Literal["positive", "negative", "mixed", "neutral"]
    aspects: list[Aspect]


class BatchAnalysis(BaseModel):
    """One entry per input review, keyed back by review_id."""
    results: list["KeyedAnalysis"]


class KeyedAnalysis(BaseModel):
    review_id: str
    language: Literal["en", "hinglish", "hi", "other"]
    overall_polarity: Literal["positive", "negative", "mixed", "neutral"]
    aspects: list[Aspect]


BatchAnalysis.model_rebuild()


SYSTEM_PROMPT = """You are an operations analyst for a serviced-apartment operator \
in Bengaluru, India. You read guest reviews and extract structured, actionable \
aspect-level findings.

Rules:
1. Extract one entry per DISTINCT aspect mentioned. A review that praises the room \
and complains about check-in produces TWO aspects, not one.
2. evidence_span must be copied VERBATIM from the review text - an exact substring. \
Never paraphrase, translate, correct spelling, or invent wording. If you cannot \
quote it exactly, do not report the aspect.
3. A positive overall review can still contain a negative operational aspect. \
Report it. Those buried complaints are the most valuable output you produce.
4. severity applies to negative aspects only; use 'not_applicable' for positive \
and neutral ones.
5. Some reviews are code-mixed Hindi-English (Hinglish). Handle them normally and \
set language accordingly.
6. Report only what the text supports. Do not infer, speculate, or add aspects that \
are not mentioned. Fewer accurate aspects beat more guessed ones.
7. Never output numbers, counts, rates or totals. Labels and quotes only."""


def _norm(s: str) -> str:
    """Whitespace- and case-insensitive normalisation for substring comparison."""
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


@dataclass
class ExtractionResult:
    accepted: list[dict] = field(default_factory=list)
    quarantined: list[dict] = field(default_factory=list)
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    api_calls: int = 0
    api_errors: int = 0
    reviews_in: int = 0
    reviews_returned: int = 0

    @property
    def quarantine_rate_pct(self) -> float:
        total = len(self.accepted) + len(self.quarantined)
        return round(100.0 * len(self.quarantined) / total, 2) if total else 0.0


class GeminiExtractor:
    def __init__(self, model: str = MODEL) -> None:
        from google import genai

        config.load_env()
        self._genai = genai
        self.client = genai.Client(api_key=config.get_gemini_api_key())
        self.model = model

    def _call(self, prompt: str, *, max_retries: int = 4):
        """Single API call with exponential backoff on transient failures."""
        from google.genai import types

        cfg = types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=BatchAnalysis,
            temperature=0.0,          # determinism: the same review must classify the same way
        )
        delay = 2.0
        last: Exception | None = None
        for attempt in range(max_retries):
            try:
                return self.client.models.generate_content(
                    model=self.model, contents=prompt, config=cfg)
            except Exception as exc:  # noqa: BLE001
                last = exc
                msg = str(exc)
                # 429 (quota) and 5xx are worth retrying; 400 never is.
                if "400" in msg or "INVALID_ARGUMENT" in msg:
                    raise
                if attempt < max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
        raise last if last else RuntimeError("unreachable")

    def extract_batch(self, reviews: list[dict]) -> ExtractionResult:
        """Extract aspects for a batch of reviews.

        `reviews` items need review_id, review_text, property_key, review_date.
        """
        out = ExtractionResult(reviews_in=len(reviews))
        payload = [{"review_id": r["review_id"], "text": r["review_text"]} for r in reviews]
        prompt = (
            "Extract aspect-level findings for each review below. Return one "
            "results entry per review_id.\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=1)
        )

        try:
            resp = self._call(prompt)
            out.api_calls = 1
        except Exception as exc:  # noqa: BLE001
            out.api_errors = 1
            for r in reviews:
                out.quarantined.append({
                    "review_id": r["review_id"], "model": self.model,
                    "category": None, "polarity": None, "evidence_span": None,
                    "reason": f"api_error: {type(exc).__name__}",
                    "raw_payload": None,
                })
            return out

        usage = getattr(resp, "usage_metadata", None)
        if usage:
            out.prompt_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
            out.output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
            out.total_tokens = int(getattr(usage, "total_token_count", 0) or 0)

        parsed = getattr(resp, "parsed", None)
        if parsed is None:
            try:
                parsed = BatchAnalysis.model_validate_json(resp.text or "")
            except Exception as exc:  # noqa: BLE001
                for r in reviews:
                    out.quarantined.append({
                        "review_id": r["review_id"], "model": self.model,
                        "category": None, "polarity": None, "evidence_span": None,
                        "reason": f"unparseable_response: {type(exc).__name__}",
                        "raw_payload": json.dumps({"text": (resp.text or "")[:800]}),
                    })
                return out

        by_id = {r["review_id"]: r for r in reviews}
        out.reviews_returned = len(parsed.results)

        for item in parsed.results:
            src = by_id.get(item.review_id)
            if src is None:
                # The model invented a review_id. Never publish it.
                out.quarantined.append({
                    "review_id": item.review_id, "model": self.model,
                    "category": None, "polarity": None, "evidence_span": None,
                    "reason": "unknown_review_id",
                    "raw_payload": json.dumps({"language": item.language}),
                })
                continue

            source_norm = _norm(src["review_text"])
            for asp in item.aspects:
                row = {
                    "review_id": item.review_id,
                    "property_key": src["property_key"],
                    "review_date": src["review_date"],
                    "category": asp.category,
                    "polarity": asp.polarity,
                    "severity": asp.severity,
                    "confidence": asp.confidence,
                    "actionable_by": asp.actionable_by,
                    "evidence_span": asp.evidence_span,
                    "language": item.language,
                    "model": self.model,
                }
                # THE validation gate: the quote must genuinely appear in the source.
                # A span that does not is a fabricated citation, and the row is
                # blocked rather than published with a caveat.
                if not asp.evidence_span or _norm(asp.evidence_span) not in source_norm:
                    out.quarantined.append({
                        "review_id": item.review_id, "model": self.model,
                        "category": asp.category, "polarity": asp.polarity,
                        "evidence_span": asp.evidence_span,
                        "reason": "evidence_span_not_in_source",
                        "raw_payload": json.dumps(row, default=str),
                    })
                    continue
                row["evidence_verified"] = True
                out.accepted.append(row)

        # A review the model simply skipped is a silent loss unless it is recorded.
        missing = set(by_id) - {i.review_id for i in parsed.results}
        for rid in missing:
            out.quarantined.append({
                "review_id": rid, "model": self.model,
                "category": None, "polarity": None, "evidence_span": None,
                "reason": "review_omitted_from_response", "raw_payload": None,
            })

        return out
