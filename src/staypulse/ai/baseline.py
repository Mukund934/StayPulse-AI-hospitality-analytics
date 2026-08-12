"""Deterministic keyword baseline for aspect extraction.

This exists so the LLM has to EARN its place. "I used AI" is not a result; "the
model beat a keyword baseline by N points of macro-F1, and here is the table" is.

A baseline also calibrates expectations honestly. Keyword matching does well on
aspect DETECTION -- hospitality vocabulary is narrow and repetitive -- and badly on
POLARITY, because "the AC was not cooling" and "the AC cooled quickly" share their
aspect keyword entirely. That asymmetry is the argument for a language model, and
it is more persuasive shown than asserted.

No lexicon sentiment library is used. VADER and similar are tuned on English
social media, have no aspect awareness, and fail outright on code-mixed Hinglish.
Including one would be a strawman rather than a baseline.
"""

from __future__ import annotations

import re

# Aspect keyword sets. Deliberately generous: a baseline should be a fair fight.
ASPECT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "cleanliness": ("clean", "spotless", "dirty", "dust", "stain", "sparkling", "hygien"),
    "housekeeping_response": ("housekeeping", "cleaning", "towel", "linen", "restock",
                              "maid", "room service"),
    "staff": ("staff", "front desk", "reception", "helpful", "courteous", "manager"),
    "check_in": ("check-in", "check in", "checkin", "access code", "key", "arrival"),
    "room_quality": ("apartment", "room", "furnish", "bed", "sofa", "kitchenette",
                     "spacious", "comfortable"),
    "location": ("location", "walking distance", "convenient", "road", "tech park",
                 "residential"),
    "wifi": ("wifi", "wi-fi", "internet", "network", "connectivity"),
    "value": ("value", "price", "cheap", "expensive", "worth", "vasool", "paisa"),
    "amenities": ("gym", "laundry", "parking", "pool", "restaurant", "amenit"),
    "maintenance": ("ac ", "a/c", "air condition", "hot water", "geyser", "plumb",
                    "repair", "broken", "not working", "fix"),
    "noise": ("noise", "noisy", "loud", "quiet", "peaceful", "construction", "corridor"),
    "food": ("breakfast", "tea", "coffee", "grocery", "food", "restaurant"),
}

# Negation and complaint cues, including Hinglish. Ordering matters less than
# coverage: a single hit flips polarity to negative.
NEGATIVE_CUES = (
    "not ", "no ", "never", "n't", "slow", "delay", "took ", "waited", "wait ",
    "problem", "issue", "complaint", "poor", "bad", "worse", "worst", "unhelpful",
    "broken", "dirty", "stain", "dust", "ignored", "failed", "stopped", "drop",
    "too ", "only complaint", "expected more", "inconvenien", "out of order",
    "nahi", "thoda", "bahut time", "lag gaye", "bar bar", "dirty tha",
)
POSITIVE_CUES = (
    "clean", "spotless", "great", "excellent", "prompt", "immediately", "quick",
    "comfortable", "helpful", "warm", "convenient", "fast", "stable", "peaceful",
    "well ", "perfect", "bonus", "worth", "recommend", "vasool", "ekdum",
)

SEVERITY_CUES = {
    "severe": ("three times", "never", "did not work", "stopped working", "severe",
               "ignored", "waited outside", "stains", "kept dropping", "not cooling"),
    "moderate": ("two hours", "forty minutes", "took nearly", "slow", "too slow",
                 "not been cleaned", "unhelpful", "bahut time", "lag gaye"),
}

ROUTING = {
    "cleanliness": "housekeeping", "housekeeping_response": "housekeeping",
    "staff": "front_office", "check_in": "front_office", "room_quality": "maintenance",
    "location": "none", "wifi": "maintenance", "value": "revenue",
    "amenities": "maintenance", "maintenance": "maintenance", "noise": "none",
    "food": "housekeeping",
}


def _sentences(text: str) -> list[str]:
    """Split on sentence and clause boundaries.

    Clause splitting is what gives the baseline any chance at polarity: "the room
    was clean but housekeeping was slow" must not be scored as one blob.
    """
    parts = re.split(r"(?<=[.!?])\s+|\s+(?:but|however|though)\s+|\s*[-;]\s*", text,
                     flags=re.IGNORECASE)
    return [p.strip() for p in parts if p and p.strip()]


def classify(text: str) -> list[dict]:
    """Return aspect rows for one review. Same output shape as the LLM path."""
    if not text:
        return []
    found: dict[tuple[str, str], dict] = {}

    for clause in _sentences(text):
        low = clause.lower()
        neg_hits = sum(1 for c in NEGATIVE_CUES if c in low)
        pos_hits = sum(1 for c in POSITIVE_CUES if c in low)
        polarity = "negative" if neg_hits > pos_hits else (
            "positive" if pos_hits > 0 else "neutral")

        severity = "not_applicable"
        if polarity == "negative":
            severity = "minor"
            for level, cues in SEVERITY_CUES.items():
                if any(c in low for c in cues):
                    severity = level
                    break

        for aspect, keywords in ASPECT_KEYWORDS.items():
            if any(k in low for k in keywords):
                key = (aspect, polarity)
                if key not in found:
                    found[key] = {
                        "category": aspect,
                        "polarity": polarity,
                        "severity": severity,
                        "confidence": "medium",
                        "actionable_by": ROUTING.get(aspect, "none")
                                         if polarity == "negative" else "none",
                        "evidence_span": clause[:200],
                        "method": "keyword_baseline",
                    }
    return list(found.values())
