"""Cancellation risk: which reservations will not survive to arrival.

WHAT IS BEING PREDICTED, AND WHY IT IS NOT "WASH"

The obvious target is wash -- cancelled or no-show -- because that is the number
an overbooking policy consumes. It is the wrong target to model, and the
generator says why.

Cancellation has a mechanism:

    p_cancel = clip(channel.cancel_rate * (1 + 0.55*tanh((lead_days-10)/14)),
                    0.01, 0.62)

so it depends on the channel and on how far ahead the booking was made. Both are
known at booking time and both are learnable.

No-show does not have one:

    elif rng.random() < 0.014

a flat 1.4% applied to every booking that was not cancelled, independent of
channel, lead time, price, length of stay and everything else. **No model can
predict it**, and any model that appears to is fitting noise.

Pooling the two would mix a learnable signal with an unlearnable constant and
dilute the measurable performance of the part that works. So this module models
cancellation, reports the no-show rate as the constant it is, and demonstrates
the unlearnability rather than asserting it -- see `noshow_is_unlearnable`.


WHY LOGISTIC REGRESSION, AND WHY HAND-ROLLED

scikit-learn is not a dependency of this project and adding it pulls scipy onto
a free-tier build for one feature. Logistic regression with L2 and gradient
descent is sixty lines of numpy, every coefficient is inspectable, and there is
no hidden preprocessing to explain. The metrics that matter here -- calibration,
per-class precision and recall -- have to be written either way, because the
useful ones are not what a `.score()` call returns.

A gradient-boosted model would very likely score better. It would also make the
ground-truth check below much harder to read, and on a generator whose mechanism
is a channel rate times a smooth lead-time factor, a linear model in the right
features is close to the correct functional form.


THE TWO THINGS THAT WOULD MAKE THIS DISHONEST

1. RANDOM TRAIN/TEST SPLIT. Bookings are not exchangeable across time -- the
   portfolio grew, channel mix moved. A random split lets the model learn from
   bookings made after the ones it is scored on. The split here is TEMPORAL:
   train on bookings made before a cutoff, test on bookings made after it.

2. SCALING ON THE FULL DATASET. Standardising features using means and standard
   deviations computed over train and test together leaks the test distribution
   into the fitted model. Statistics come from the training rows only.

AUC alone would hide both. It is reported, but so are calibration, per-class
precision and recall at a stated threshold, and the base rate -- because a model
that predicts "never cancels" is 84.6% accurate on this data and useless.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from staypulse import db

# Share of bookings held back for testing, taken from the END of the booking
# timeline rather than at random. See the module docstring.
TEST_FRACTION = 0.25

# L2 penalty. Enough to keep the one-hot channel coefficients from running away
# on the thinner channels without flattening the signal the model exists to find.
L2_PENALTY = 1.0

# Full-batch gradient descent. The dataset is ~5k rows and ~20 features, so this
# converges in well under a second and needs no optimiser worth naming.
LEARNING_RATE = 0.5
MAX_ITERATIONS = 4000
CONVERGENCE_TOL = 1e-8

# Probability threshold for the per-class report. NOT tuned to flatter the
# model: it is the base rate, so the classifier flags a booking whenever it
# looks riskier than an average booking. Any other choice needs an argument
# about the relative cost of a missed cancellation against a false alarm, and
# this warehouse has no such cost.
THRESHOLD_IS_BASE_RATE = True

# Calibration bins. Ten is enough to see a miscalibrated model and few enough
# that each bin keeps a usable count at this sample size.
CALIBRATION_BINS = 10


@dataclass
class Model:
    """A fitted logistic regression, with the scaling it was fitted under."""

    feature_names: list[str]
    weights: np.ndarray
    intercept: float
    means: np.ndarray
    scales: np.ndarray
    iterations: int
    converged: bool

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        matrix = frame[self.feature_names].to_numpy(dtype=float)
        scaled = (matrix - self.means) / self.scales
        return _sigmoid(scaled @ self.weights + self.intercept)

    def coefficients(self) -> list[dict[str, Any]]:
        """Coefficients in standardised units, largest effect first.

        Reported on the standardised scale on purpose: a raw coefficient on
        `lead_time_days` and one on `net_room_amount_inr` are not comparable,
        and ranking them as if they were is how a feature-importance table
        becomes fiction.
        """
        return sorted(
            (
                {
                    "feature": name,
                    "coefficient": round(float(weight), 4),
                    "odds_ratio_per_sd": round(float(math.exp(weight)), 3),
                }
                for name, weight in zip(self.feature_names, self.weights)
            ),
            key=lambda row: -abs(row["coefficient"]),
        )


@dataclass
class Evaluation:
    """Out-of-sample performance. Every number here is on held-out bookings."""

    n_train: int
    n_test: int
    base_rate: float
    threshold: float
    auc: float
    brier: float
    precision: float
    recall: float
    f1: float
    lift_over_base_rate: float
    confusion: dict[str, int]
    calibration: list[dict[str, Any]]
    calibration_error: float
    split_date: dt.date

    def as_dict(self) -> dict[str, Any]:
        return {
            "split": {
                "method": "temporal -- train on bookings made before the split date",
                "split_date": self.split_date.isoformat(),
                "train_bookings": self.n_train,
                "test_bookings": self.n_test,
            },
            "base_rate_pct": round(100.0 * self.base_rate, 2),
            "threshold": round(self.threshold, 4),
            "discrimination": {
                "auc": round(self.auc, 4),
                "note": (
                    "AUC 0.5 is a coin toss. Reported first because it is asked "
                    "for, but it says nothing about whether the probabilities "
                    "themselves are usable -- see calibration."
                ),
            },
            "classification_at_threshold": {
                "precision_pct": round(100.0 * self.precision, 1),
                "recall_pct": round(100.0 * self.recall, 1),
                "f1": round(self.f1, 3),
                "lift_over_base_rate": round(self.lift_over_base_rate, 2),
                "confusion": self.confusion,
                "note": (
                    "Precision against the base rate is the number that matters. "
                    "A model predicting 'never cancels' scores "
                    f"{100.0 * (1 - self.base_rate):.1f}% accuracy here and is "
                    "worthless, which is why accuracy is not reported."
                ),
            },
            "calibration": {
                "bins": self.calibration,
                "weighted_mean_absolute_error_pp": round(
                    100.0 * self.calibration_error, 2),
                "note": (
                    "A calibrated model that says 30% is right 30% of the time. "
                    "Without this a ranking model can look excellent and still "
                    "produce probabilities nobody should act on. The error is "
                    "weighted by bin population: unweighted, a bin holding one "
                    "booking would count as much as one holding 344, which on "
                    "this model inflated the figure fivefold."
                ),
            },
            "brier_score": round(self.brier, 4),
        }


# ---------------------------------------------------------------------------
def _sigmoid(z: np.ndarray) -> np.ndarray:
    """Numerically stable logistic function."""
    out = np.empty_like(z, dtype=float)
    positive = z >= 0
    out[positive] = 1.0 / (1.0 + np.exp(-z[positive]))
    exp_z = np.exp(z[~positive])
    out[~positive] = exp_z / (1.0 + exp_z)
    return out


def dataset() -> pd.DataFrame:
    """Every booking, with the features knowable when it was made.

    NOTHING HERE MAY BE DERIVED FROM THE OUTCOME. `cancel_date`, `cancelled_at`
    and `status` are the outcome; the only one that appears is the target
    itself. A feature like "days between booking and cancellation" would score
    beautifully and be unusable, because at booking time it does not exist.
    """
    rows = db.fetch_all(
        """
        SELECT b.booking_key,
               b.booking_date,
               b.check_in_date,
               c.channel_code,
               p.property_code,
               b.stay_type,
               b.nights,
               b.adults,
               b.lead_time_days,
               b.net_room_amount_inr,
               b.discount_inr,
               (b.status = 'cancelled')                      AS cancelled,
               (b.status = 'no_show')                        AS no_show,
               EXTRACT(ISODOW FROM b.check_in_date)::int     AS check_in_dow,
               EXTRACT(MONTH  FROM b.check_in_date)::int     AS check_in_month
        FROM mart.fact_booking b
        JOIN mart.dim_channel  c USING (channel_key)
        JOIN mart.dim_property p USING (property_key)
        ORDER BY b.booking_date, b.booking_key
        """
    )
    frame = pd.DataFrame(rows)
    for column in ("nights", "adults", "lead_time_days", "check_in_dow",
                   "check_in_month"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("net_room_amount_inr", "discount_inr"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype(float)
    frame["cancelled"] = frame["cancelled"].astype(int)
    frame["no_show"] = frame["no_show"].astype(int)
    return frame


def build_features(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Design matrix from booking-time columns only.

    The lead-time term is deliberately not just `lead_time_days`. The planted
    mechanism scales cancellation by `tanh((lead - 10) / 14)`, which saturates:
    the difference between 5 and 20 days matters far more than between 60 and
    75. A linear term alone cannot express that, so a saturating transform is
    included alongside it and the model is allowed to weight both.

    That is a modelling choice informed by the generator, and it is worth being
    explicit that this is a luxury real data would not offer.
    """
    features = frame.copy()
    features["lead_time_log"] = np.log1p(features["lead_time_days"].clip(lower=0))
    features["lead_time_saturating"] = np.tanh(
        (features["lead_time_days"] - 10.0) / 14.0
    )
    features["is_weekend_arrival"] = (features["check_in_dow"] >= 6).astype(int)
    features["price_per_night"] = (
        features["net_room_amount_inr"] / features["nights"].clip(lower=1)
    )
    features["has_discount"] = (features["discount_inr"] > 0).astype(int)

    names = [
        "lead_time_days", "lead_time_log", "lead_time_saturating",
        "nights", "adults", "price_per_night", "has_discount",
        "is_weekend_arrival",
    ]

    for column, prefix in (("channel_code", "channel"),
                           ("property_code", "property"),
                           ("stay_type", "stay")):
        dummies = pd.get_dummies(features[column], prefix=prefix)
        # Drop one level to avoid a perfectly collinear set; the dropped level
        # is absorbed into the intercept.
        dummies = dummies.iloc[:, 1:]
        for name in dummies.columns:
            features[name] = dummies[name].astype(int)
            names.append(name)

    return features, names


def temporal_split(frame: pd.DataFrame,
                   test_fraction: float = TEST_FRACTION
                   ) -> tuple[pd.DataFrame, pd.DataFrame, dt.date]:
    """Split on booking date, not at random.

    A random split lets a model learn from bookings made after the ones it is
    scored on. On a portfolio that grew mid-series and whose channel mix moved,
    that is not a small effect and it flatters every metric.
    """
    ordered = frame.sort_values(["booking_date", "booking_key"]).reset_index(drop=True)
    cut = int(len(ordered) * (1.0 - test_fraction))
    split_date = ordered.loc[cut, "booking_date"]
    train = ordered[ordered["booking_date"] < split_date]
    test = ordered[ordered["booking_date"] >= split_date]
    return train, test, split_date


def fit(train: pd.DataFrame, names: list[str], target: str = "cancelled",
        l2: float = L2_PENALTY) -> Model:
    """L2-regularised logistic regression by full-batch gradient descent.

    Feature statistics come from the TRAINING rows only. Standardising over
    train and test together would leak the test distribution into the model, and
    it is the kind of leak that never shows up as an error.
    """
    matrix = train[names].to_numpy(dtype=float)
    y = train[target].to_numpy(dtype=float)

    means = matrix.mean(axis=0)
    scales = matrix.std(axis=0)
    scales[scales < 1e-12] = 1.0
    scaled = (matrix - means) / scales

    n, k = scaled.shape
    weights = np.zeros(k)
    intercept = 0.0
    previous = np.inf
    iterations = 0
    converged = False

    for iterations in range(1, MAX_ITERATIONS + 1):
        predicted = _sigmoid(scaled @ weights + intercept)
        error = predicted - y
        grad_w = (scaled.T @ error) / n + (l2 / n) * weights
        grad_b = float(error.mean())
        weights -= LEARNING_RATE * grad_w
        intercept -= LEARNING_RATE * grad_b

        # Penalised log loss, guarded against log(0).
        clipped = np.clip(predicted, 1e-12, 1 - 1e-12)
        loss = float(
            -np.mean(y * np.log(clipped) + (1 - y) * np.log(1 - clipped))
            + (l2 / (2 * n)) * float(weights @ weights)
        )
        if abs(previous - loss) < CONVERGENCE_TOL:
            converged = True
            break
        previous = loss

    return Model(
        feature_names=names,
        weights=weights,
        intercept=intercept,
        means=means,
        scales=scales,
        iterations=iterations,
        converged=converged,
    )


# ---------------------------------------------------------------------------
def roc_auc(y: np.ndarray, scores: np.ndarray) -> float:
    """AUC via the rank-sum identity, with ties handled by average ranks."""
    positives = int(y.sum())
    negatives = len(y) - positives
    if positives == 0 or negatives == 0:
        return float("nan")
    order = np.argsort(scores, kind="mergesort")
    ranks = np.empty(len(scores), dtype=float)
    ranks[order] = np.arange(1, len(scores) + 1)

    # Average the ranks inside each tied group, or a model that outputs many
    # identical probabilities is scored as if it had broken those ties well.
    sorted_scores = scores[order]
    start = 0
    for i in range(1, len(sorted_scores) + 1):
        if i == len(sorted_scores) or sorted_scores[i] != sorted_scores[start]:
            if i - start > 1:
                ranks[order[start:i]] = ranks[order[start:i]].mean()
            start = i

    rank_sum = float(ranks[y == 1].sum())
    return (rank_sum - positives * (positives + 1) / 2.0) / (positives * negatives)


def calibration_curve(y: np.ndarray, probabilities: np.ndarray,
                      bins: int = CALIBRATION_BINS) -> tuple[list[dict[str, Any]], float]:
    """Predicted probability against observed frequency, by bin.

    Equal-width bins over the observed probability range rather than quantile
    bins: quantile bins guarantee a populated table even when a model outputs a
    narrow band of probabilities, which hides exactly the failure worth seeing.

    The returned error is POPULATION-WEIGHTED, and that is not a detail. An
    unweighted mean over equal-width bins lets a bin holding one booking count
    as much as a bin holding 344, and on this model that inflated the reported
    error from 1.7pp to 9.1pp on the strength of three sparse tail bins -- one
    of which contained a single reservation. That is the same failure recorded
    in PART L-14: an average over units with wildly different weights is not a
    summary of those units. The per-bin table is returned alongside so the
    sparse tail stays visible rather than being smoothed away.
    """
    edges = np.linspace(probabilities.min(), probabilities.max() + 1e-12, bins + 1)
    rows: list[dict[str, Any]] = []
    weighted_error = 0.0
    total = 0
    for i in range(bins):
        mask = (probabilities >= edges[i]) & (probabilities < edges[i + 1])
        count = int(mask.sum())
        if count == 0:
            continue
        predicted = float(probabilities[mask].mean())
        observed = float(y[mask].mean())
        rows.append({
            "bin": f"{100 * edges[i]:.1f}-{100 * edges[i + 1]:.1f}%",
            "bookings": count,
            "mean_predicted_pct": round(100 * predicted, 2),
            "observed_pct": round(100 * observed, 2),
            "gap_pp": round(100 * (observed - predicted), 2),
        })
        weighted_error += abs(observed - predicted) * count
        total += count
    return rows, (weighted_error / total if total else float("nan"))


def evaluate(model: Model, train: pd.DataFrame, test: pd.DataFrame,
             split_date: dt.date, target: str = "cancelled") -> Evaluation:
    """Out-of-sample metrics, with the base rate beside every one of them."""
    y = test[target].to_numpy(dtype=float)
    probabilities = model.predict_proba(test)
    base_rate = float(train[target].mean())
    threshold = base_rate if THRESHOLD_IS_BASE_RATE else 0.5

    predicted = (probabilities >= threshold).astype(int)
    true_positive = int(((predicted == 1) & (y == 1)).sum())
    false_positive = int(((predicted == 1) & (y == 0)).sum())
    false_negative = int(((predicted == 0) & (y == 1)).sum())
    true_negative = int(((predicted == 0) & (y == 0)).sum())

    precision = true_positive / (true_positive + false_positive) if (
        true_positive + false_positive) else 0.0
    recall = true_positive / (true_positive + false_negative) if (
        true_positive + false_negative) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    test_base = float(y.mean())

    bins, calibration_error = calibration_curve(y, probabilities)

    return Evaluation(
        n_train=len(train),
        n_test=len(test),
        base_rate=test_base,
        threshold=threshold,
        auc=roc_auc(y, probabilities),
        brier=float(np.mean((probabilities - y) ** 2)),
        precision=precision,
        recall=recall,
        f1=f1,
        lift_over_base_rate=(precision / test_base) if test_base else float("nan"),
        confusion={
            "true_positive": true_positive,
            "false_positive": false_positive,
            "false_negative": false_negative,
            "true_negative": true_negative,
        },
        calibration=bins,
        calibration_error=calibration_error,
        split_date=split_date,
    )


# ---------------------------------------------------------------------------
def noshow_is_unlearnable(frame: pd.DataFrame | None = None) -> dict[str, Any]:
    """Demonstrate that no-show carries no learnable signal, rather than saying so.

    The generator draws it as a flat 1.4% over every booking that was not
    cancelled, so a model fitted on the same features that predict cancellation
    should land at an AUC indistinguishable from a coin toss. Fitting it and
    reporting the result is the honest way to establish that; asserting it from
    the generator source would be trusting a comment.
    """
    frame = dataset() if frame is None else frame
    # Only bookings that were NOT cancelled are eligible: the generator's branch
    # makes no-show conditional on surviving the cancellation draw.
    eligible = frame[frame["cancelled"] == 0].copy()
    features, names = build_features(eligible)
    train, test, split_date = temporal_split(features)

    model = fit(train, names, target="no_show")
    evaluation = evaluate(model, train, test, split_date, target="no_show")

    return {
        "target": "no_show",
        "eligible_bookings": len(eligible),
        "no_show_rate_pct": round(100.0 * float(eligible["no_show"].mean()), 2),
        "generator_constant_pct": 1.4,
        "auc": round(evaluation.auc, 4),
        "verdict": (
            "unlearnable" if abs(evaluation.auc - 0.5) < 0.10 else "signal present"
        ),
        "note": (
            "The generator draws no-show as a flat 1.4% over every booking that "
            "survived the cancellation draw, independent of channel, lead time, "
            "price and length of stay. An AUC near 0.5 is therefore the correct "
            "result and not a modelling failure. This is why the risk model "
            "targets cancellation rather than wash: pooling the two would dilute "
            "a learnable signal with a constant."
        ),
    }


def temporal_drift(frame: pd.DataFrame | None = None) -> dict[str, Any]:
    """Cancellation rate by booking quarter.

    THIS IS WHY THE SPLIT IS TEMPORAL, and it is a measurement rather than an
    argument. The rate falls steadily across the record, so a model fitted on
    earlier bookings meets a lower base rate when it is scored on later ones and
    over-predicts by construction. A random split would mix the eras, hide the
    drift entirely, and report a calibration the model does not actually have
    when used the way it would be used -- forwards.
    """
    frame = dataset() if frame is None else frame
    working = frame.copy()
    working["quarter"] = pd.to_datetime(working["booking_date"]).dt.to_period("Q")

    rows = [
        {
            "quarter": str(quarter),
            "bookings": int(len(group)),
            "cancel_rate_pct": round(100.0 * float(group["cancelled"].mean()), 2),
        }
        for quarter, group in working.groupby("quarter")
        if len(group) >= 50
    ]
    if len(rows) < 2:
        return {"quarters": rows, "note": "too few quarters to describe a trend"}

    first, last = rows[0], rows[-1]
    return {
        "quarters": rows,
        "first_quarter_pct": first["cancel_rate_pct"],
        "last_quarter_pct": last["cancel_rate_pct"],
        "change_pp": round(last["cancel_rate_pct"] - first["cancel_rate_pct"], 2),
        "note": (
            "The cancellation rate declines across the record. A model trained on "
            "earlier bookings therefore over-predicts on later ones, which is "
            "visible in the upper calibration bins. This is the drift a temporal "
            "split exposes and a random split conceals."
        ),
    }


def validate_against_planted(model: Model) -> dict[str, Any]:
    """Check the fitted coefficients against the generator's actual mechanism.

    Ground truth, from `generate/spec.py` and `generate/builder.py`:

        p_cancel = clip(channel.cancel_rate * (1 + 0.55*tanh((lead-10)/14)),
                        0.01, 0.62)

    Two consequences are checkable without knowing the constants: cancellation
    should rise with lead time, and the channel ordering the model implies
    should match the planted rates. The second is the stronger test, because a
    model can get the direction of one feature right by luck.
    """
    planted = {
        "BDC": 0.205, "AGODA": 0.190, "MMT": 0.170, "B2B-HR": 0.130,
        "AIRBNB": 0.120, "DIRECT": 0.085, "CORP": 0.055, "WALKIN": 0.030,
    }
    coefficients = {row["feature"]: row["coefficient"] for row in model.coefficients()}

    lead_terms = {
        name: coefficients[name]
        for name in ("lead_time_days", "lead_time_log", "lead_time_saturating")
        if name in coefficients
    }

    # The dropped dummy level sits in the intercept, so its coefficient is 0 by
    # construction rather than missing.
    channel_effects: dict[str, float] = {}
    for code in planted:
        channel_effects[code] = coefficients.get(f"channel_{code}", 0.0)

    ranked_model = [c for c, _ in sorted(channel_effects.items(),
                                         key=lambda kv: -kv[1])]
    ranked_planted = [c for c, _ in sorted(planted.items(), key=lambda kv: -kv[1])]

    return {
        "planted_mechanism": (
            "p_cancel = clip(channel.cancel_rate * "
            "(1 + 0.55*tanh((lead_days-10)/14)), 0.01, 0.62)"
        ),
        "lead_time": {
            "coefficients": {k: round(v, 4) for k, v in lead_terms.items()},
            "net_direction": "increasing" if sum(lead_terms.values()) > 0 else "decreasing",
            "expected": "increasing",
            "recovered": sum(lead_terms.values()) > 0,
        },
        "channel_ranking": {
            "planted": ranked_planted,
            "recovered": ranked_model,
            "spearman": round(_spearman(ranked_planted, ranked_model), 3),
            "coefficients": {k: round(v, 4) for k, v in channel_effects.items()},
        },
        "note": (
            "The channel coefficients are relative to the dropped dummy level, "
            "which is absorbed into the intercept, so their absolute values are "
            "not the planted rates. The ORDERING is the testable claim."
        ),
    }


def _spearman(a: list[str], b: list[str]) -> float:
    """Rank correlation between two orderings of the same items."""
    rank_a = {item: i for i, item in enumerate(a)}
    rank_b = {item: i for i, item in enumerate(b)}
    shared = sorted(set(rank_a) & set(rank_b))
    if len(shared) < 2:
        return float("nan")
    x = np.array([rank_a[i] for i in shared], dtype=float)
    y = np.array([rank_b[i] for i in shared], dtype=float)
    x -= x.mean()
    y -= y.mean()
    denominator = math.sqrt(float((x @ x) * (y @ y)))
    return float(x @ y) / denominator if denominator else float("nan")


# ---------------------------------------------------------------------------
def train_and_evaluate() -> tuple[Model, Evaluation, pd.DataFrame, list[str]]:
    """The standard pipeline: features, temporal split, fit, score."""
    frame = dataset()
    features, names = build_features(frame)
    train, test, split_date = temporal_split(features)
    model = fit(train, names)
    evaluation = evaluate(model, train, test, split_date)
    return model, evaluation, features, names


def summary() -> dict[str, Any]:
    """The published artifact: performance, calibration and ground-truth recovery."""
    model, evaluation, frame, _ = train_and_evaluate()
    return {
        "target": "booking cancelled before arrival",
        "why_not_wash": (
            "No-show is drawn as a flat 1.4% independent of every feature, so it "
            "carries no learnable signal. Pooling it with cancellation would "
            "dilute a real mechanism with a constant."
        ),
        "model": {
            "family": "logistic regression, L2, full-batch gradient descent",
            "implementation": (
                "numpy. scikit-learn is not a dependency of this project and the "
                "metrics that matter here had to be written either way."
            ),
            "features": len(model.feature_names),
            "converged": model.converged,
            "iterations": model.iterations,
            "coefficients": model.coefficients(),
        },
        "evaluation": evaluation.as_dict(),
        "ground_truth": validate_against_planted(model),
        "temporal_drift": temporal_drift(frame),
        "no_show": noshow_is_unlearnable(frame),
        "limitations": [
            "One synthetic portfolio. The mechanism is known because it was "
            "planted, which is exactly the luxury real booking data does not "
            "offer.",
            "No censoring. Every booking in this warehouse has a settled "
            "outcome; real data contains reservations whose fate is not yet "
            "known, and a survival model would be the right tool there.",
            "No cost asymmetry. The threshold is the base rate because nothing "
            "in this warehouse prices a missed cancellation against a false "
            "alarm.",
        ],
    }
