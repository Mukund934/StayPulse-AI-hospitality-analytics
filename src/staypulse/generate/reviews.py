"""Guest review text generation with recorded ground truth.

Each review is composed from aspect-level fragments, so the aspects and polarities
that went IN are known exactly. That known set becomes the evaluation gold
standard.

HONESTY NOTE, and it matters for how the benchmark should be read: this is
GENERATOR ground truth, not human annotation. It measures whether the model
recovers aspects that were deliberately injected -- it does NOT measure agreement
with human judgement, and it cannot, because no human labelled it. The evaluation
reports it as such. A hand-labelled set would be strictly better evidence; this is
the honest version of what is achievable without one.

Text is template-composed rather than LLM-generated on purpose: it is free,
instant, reproducible from the seed, and -- critically -- it avoids the
circularity of generating text with a model and then scoring the same model on it.
The cost is that the prose is less varied than real reviews, which is stated as a
limitation rather than hidden.

Code-mixed Hinglish is included because guest messages in Indian hospitality
genuinely are, and because lexicon-based sentiment tools fail on it completely --
which is the argument for using a frontier model at all.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Aspect taxonomy. Fixed and closed, so week-over-week trends are comparable --
# open-ended topic modelling cannot produce a stable time series.
POSITIVE_FRAGMENTS: dict[str, list[str]] = {
    "cleanliness": [
        "The apartment was spotless when we arrived",
        "Room was very clean and fresh",
        "Bathroom was sparkling clean",
        "Ekdum clean tha, no complaints",
    ],
    "housekeeping_response": [
        "Housekeeping came within minutes of asking",
        "Daily cleaning was prompt and thorough",
        "Requested extra towels and they arrived immediately",
    ],
    "staff": [
        "The staff were genuinely warm and helpful",
        "Front desk went out of their way for us",
        "Staff bahut helpful the, felt looked after",
    ],
    "check_in": [
        "Check-in was quick and painless",
        "Self check-in worked perfectly at midnight",
        "Early check-in was arranged without fuss",
    ],
    "room_quality": [
        "The apartment is well furnished and spacious",
        "Kitchenette had everything we needed",
        "Bed was extremely comfortable",
        "Room spacious tha aur well maintained",
    ],
    "location": [
        "Great location, walking distance to everything",
        "Very convenient for the tech parks",
        "Quiet residential street but close to the main road",
    ],
    "wifi": [
        "WiFi was fast and stable for video calls",
        "Internet held up fine through a full day of meetings",
    ],
    "value": [
        "Excellent value for what you get",
        "Cheaper than a hotel and far more space",
        "Paisa vasool, definitely worth it",
    ],
    "amenities": [
        "Laundry and gym were a real bonus",
        "Free parking made a big difference",
    ],
    "maintenance": [
        "Everything worked exactly as it should",
        "AC cooled the room quickly",
    ],
    "noise": [
        "Very peaceful, slept well every night",
        "Quiet even on the weekend",
    ],
    "food": [
        "Grocery delivery service was handy",
        "Tea and coffee supplies were restocked daily",
    ],
}

NEGATIVE_FRAGMENTS: dict[str, list[tuple[str, str]]] = {
    # (fragment, severity)
    "cleanliness": [
        ("the bathroom had not been cleaned properly", "moderate"),
        ("there was dust on the shelves and under the bed", "minor"),
        ("bedsheets had stains when we checked in", "severe"),
        ("room thoda dirty tha, expected better", "moderate"),
    ],
    "housekeeping_response": [
        ("housekeeping took nearly two hours to respond", "moderate"),
        ("we asked for cleaning three times before anyone came", "severe"),
        ("evening housekeeping requests were simply ignored", "severe"),
        ("towels maange the, do ghante lag gaye", "moderate"),
    ],
    "staff": [
        ("the front desk was unhelpful when we raised an issue", "moderate"),
        ("nobody seemed to take ownership of the problem", "moderate"),
    ],
    "check_in": [
        ("check-in took over forty minutes", "moderate"),
        ("the access code did not work and we waited outside", "severe"),
        ("check-in mein bahut time laga", "moderate"),
    ],
    "room_quality": [
        ("the sofa was worn and the chair was broken", "minor"),
        ("the apartment felt smaller than the photos suggested", "minor"),
    ],
    "location": [
        ("the approach road is badly lit at night", "minor"),
    ],
    "wifi": [
        ("WiFi kept dropping during calls", "severe"),
        ("internet was too slow to work on", "moderate"),
        ("wifi bar bar disconnect ho raha tha", "moderate"),
    ],
    "value": [
        ("for this price I expected more", "minor"),
    ],
    "amenities": [
        ("there is no restaurant, which was inconvenient", "minor"),
        ("the gym equipment was out of order", "minor"),
    ],
    "maintenance": [
        ("the AC was not cooling and took a day to fix", "severe"),
        ("hot water stopped working on the second morning", "severe"),
        ("the geyser was not working properly", "moderate"),
        ("AC theek se kaam nahi kar raha tha", "severe"),
    ],
    "noise": [
        ("construction noise started early every morning", "moderate"),
        ("could hear everything from the corridor", "minor"),
    ],
    "food": [
        ("no tea or coffee was restocked after the first day", "minor"),
    ],
}

# Which team a negative aspect routes to. Without this the analysis produces a
# chart instead of a work item.
ROUTING: dict[str, str] = {
    "cleanliness": "housekeeping",
    "housekeeping_response": "housekeeping",
    "staff": "front_office",
    "check_in": "front_office",
    "room_quality": "maintenance",
    "location": "none",
    "wifi": "maintenance",
    "value": "revenue",
    "amenities": "maintenance",
    "maintenance": "maintenance",
    "noise": "none",
    "food": "housekeeping",
}

OPENERS = ["", "Overall, ", "Stayed here for work. ", "Second time staying here. ",
           "Booked for a short project. "]
CONNECTORS = [" but ", " however ", " though ", ". The only issue was that ",
              ". One problem: ", " - only complaint is "]
CLOSERS = ["", " Would stay again.", " Recommended.", " Will book again next trip.",
           " Hope they fix it."]


def _lang_of(text: str) -> str:
    """Crude but honest language tag, used to report accuracy per language band."""
    # "the" is deliberately NOT a marker: it is the commonest English word, and
    # including it tagged nearly every English review as Hinglish.
    hinglish_markers = ("tha ", "tha.", "bahut", "thoda", "nahi", "ekdum", " mein",
                        "maange", "vasool", " raha", " gaye", "kaam", "theek",
                        "bar bar", "lag gaye", "laga")
    lower = f" {text.lower()} "
    hits = sum(1 for m in hinglish_markers if f" {m}" in lower)
    return "hinglish" if hits >= 2 else "en"


def generate_review_text(
    reviews: pd.DataFrame, rng: np.random.Generator
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Attach text to review rows and return (reviews_with_text, ground_truth).

    ground_truth has one row per (review_id, aspect) with the polarity and
    severity that were composed in.
    """
    texts: list[str | None] = []
    langs: list[str] = []
    truth: list[dict] = []

    pos_aspects = list(POSITIVE_FRAGMENTS)
    neg_aspects = list(NEGATIVE_FRAGMENTS)

    for r in reviews.itertuples(index=False):
        rating = getattr(r, "rating", None)
        had_breach = bool(getattr(r, "_had_sla_breach", False))

        # A missing rating is a data defect elsewhere; the review still has text.
        effective = 4.6 if rating is None or pd.isna(rating) else float(rating)

        n_pos = int(rng.integers(1, 3))
        # High-rated stays still carry operational problems. That is the whole
        # point of aspect extraction: a 5-star review saying housekeeping took two
        # hours is a work item that document-level sentiment throws away.
        if effective >= 4.5:
            p_negative = 0.34 if had_breach else 0.17
        elif effective >= 3.5:
            p_negative = 0.72
        else:
            p_negative = 0.95
        n_neg = 1 if rng.random() < p_negative else 0
        if effective < 3.0 and rng.random() < 0.45:
            n_neg = 2

        chosen_pos = list(rng.choice(pos_aspects, size=min(n_pos, len(pos_aspects)),
                                     replace=False))
        # A stay with an SLA breach is far more likely to complain about response.
        if n_neg and had_breach and rng.random() < 0.55:
            chosen_neg = ["housekeeping_response"]
            if n_neg == 2:
                extra = str(rng.choice([a for a in neg_aspects if a != "housekeeping_response"]))
                chosen_neg.append(extra)
        else:
            chosen_neg = list(rng.choice(neg_aspects, size=min(n_neg, len(neg_aspects)),
                                         replace=False)) if n_neg else []

        parts = [str(rng.choice(OPENERS))]
        for i, asp in enumerate(chosen_pos):
            frag = str(rng.choice(POSITIVE_FRAGMENTS[asp]))
            parts.append(frag if i == 0 else f", and {frag[0].lower() + frag[1:]}")
            truth.append({
                "review_id": r.review_id, "category": asp, "polarity": "positive",
                "severity": "not_applicable", "actionable_by": "none",
            })

        for j, asp in enumerate(chosen_neg):
            frag, sev = NEGATIVE_FRAGMENTS[asp][int(rng.integers(0, len(NEGATIVE_FRAGMENTS[asp])))]
            parts.append((str(rng.choice(CONNECTORS)) if j == 0 else " Also ") + frag)
            truth.append({
                "review_id": r.review_id, "category": asp, "polarity": "negative",
                "severity": sev, "actionable_by": ROUTING[asp],
            })

        parts.append(str(rng.choice(CLOSERS)))
        text = "".join(parts).strip()
        if not text.endswith((".", "!", "?")):
            text += "."
        text = text[0].upper() + text[1:]

        texts.append(text)
        langs.append(_lang_of(text))

    out = reviews.copy()
    out["review_text"] = texts
    out["language"] = langs
    return out, pd.DataFrame(truth)
