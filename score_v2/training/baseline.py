"""Deterministic, stdlib-only regularized pairwise-impact baseline trainer.

Given a `TrainingDataset` restricted to ONE evidence tier, fits a single
linear scoring function

    raw_linear(x) = sum_i coefficient_i * normalized(x_i)

by full-batch gradient descent on a confidence-weighted pairwise logistic
loss (Bradley-Terry style: `P(left beats right) = sigmoid(s(left) -
s(right))`), with:

  * L2 regularization on every coefficient,
  * a hand-reviewed monotonic SIGN CONSTRAINT per `FeatureSpec.direction`,
    enforced by projecting each coefficient back to its allowed sign after
    every gradient step -- "more kills" can never end up penalizing the
    score, no matter what a tiny/noisy corpus happens to suggest,
  * robust (median/MAD) normalization fit once from the training rows.

There is deliberately NO trained intercept. `diff = s(left) - s(right) =
sum_i coefficient_i * (left_i - right_i)` -- a shared additive intercept
term cancels out of every pairwise comparison exactly, regardless of its
value, so it is mathematically unidentifiable from pairwise-only
supervision. `FittedBaseline.intercept` stays fixed at `0.0`; centering is
instead the job of `score_v2.training.calibration`'s role/score
calibration layers, which DO have a principled way to set an offset.

This is deliberately NOT a GAM, gradient-boosted tree, or anything
requiring numpy/scipy/sklearn: with a training corpus this small, a
higher-capacity model would overfit invisibly, and no evaluation metric in
`score_v2.training.evaluate` could distinguish that overfit from a real
signal. See `docs/SCORE_V2_MODELS.md` ("Why a linear baseline").

Determinism: no RNG anywhere in this module. Pair labels are processed in
`(pair_id, reviewer_id)` sort order, a fixed learning-rate schedule is
used, so the exact same dataset and hyperparameters always yield
bit-identical coefficients. `converged` reflects a REAL stopping
criterion (loss-delta or gradient-norm below tolerance) -- never "at
least one usable pair existed."
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_v2.feature_spec import FEATURE_ALLOWLIST, FeatureSpec, extract_feature_vector
from score_v2.training.dataset import DatasetValidationError, TrainingDataset

DEFAULT_L2_LAMBDA = 0.05
DEFAULT_LEARNING_RATE = 0.05
DEFAULT_ITERATIONS = 500
DEFAULT_TIE_WEIGHT = 0.5
DEFAULT_LOSS_TOLERANCE = 1e-7
DEFAULT_GRADIENT_TOLERANCE = 1e-6
_MAD_EPSILON = 1e-6
_MAD_TO_STD = 1.4826  # scales MAD to be comparable to a normal std dev


def sigmoid(x: float) -> float:
    """Numerically stable logistic sigmoid, public for reuse by evaluation."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


@dataclass(frozen=True)
class RobustNormalization:
    center: float
    scale: float

    def apply(self, value: float) -> float:
        return (value - self.center) / self.scale


def fit_robust_normalization(values: Sequence[Optional[float]]) -> RobustNormalization:
    """Median/MAD normalization, robust to the tiny corpus's outliers.

    Falls back to `center=0.0, scale=1.0` (a no-op) when there is no
    observed spread (zero values, or a MAD of 0) -- an honest default
    rather than a fabricated scale.
    """
    finite = [value for value in values if value is not None]
    if not finite:
        return RobustNormalization(center=0.0, scale=1.0)
    center = statistics.median(finite)
    mad = statistics.median(abs(value - center) for value in finite)
    scale = mad * _MAD_TO_STD
    if scale < _MAD_EPSILON:
        scale = 1.0
    return RobustNormalization(center=center, scale=scale)


@dataclass(frozen=True)
class FittedBaseline:
    specs: tuple[FeatureSpec, ...]
    normalizations: Mapping[str, RobustNormalization]
    coefficients: Mapping[str, float]
    intercept: float
    n_items: int
    n_items_excluded_abstain: int
    n_pairs_used: int
    n_pairs_skipped: int
    iterations_run: int
    converged: bool
    final_loss: Optional[float]


def _project_sign(value: float, direction: int) -> float:
    if direction > 0:
        return max(0.0, value)
    if direction < 0:
        return min(0.0, value)
    return value


def _binary_cross_entropy(prob: float, target: float) -> float:
    eps = 1e-9
    clipped = min(1.0 - eps, max(eps, prob))
    return -(target * math.log(clipped) + (1 - target) * math.log(1 - clipped))


def fit_pairwise_baseline(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec] = FEATURE_ALLOWLIST,
        l2_lambda: float = DEFAULT_L2_LAMBDA, learning_rate: float = DEFAULT_LEARNING_RATE,
        iterations: int = DEFAULT_ITERATIONS,
        tie_weight: float = DEFAULT_TIE_WEIGHT,
        loss_tolerance: float = DEFAULT_LOSS_TOLERANCE,
        gradient_tolerance: float = DEFAULT_GRADIENT_TOLERANCE,
        include_abstained: bool = False) -> FittedBaseline:
    """Fit one evidence tier's regularized pairwise-impact baseline.

    `dataset` must already be restricted to a single `evidence_source`
    (see `score_v2.training.export.dataset_for_tier`) -- this is verified
    explicitly (raising `DatasetValidationError` otherwise) rather than
    silently mixing tiers under colliding `base_ref` keys.

    Pairs whose `choice` is `"insufficient_evidence"`, or whose `left_ref`/
    `right_ref` is not present in `dataset.feature_records` (including a
    ref excluded because it is abstained -- see `include_abstained`), are
    excluded and counted in `n_pairs_skipped` -- never silently treated as
    a training signal.

    `include_abstained=False` (the default) excludes every
    `FeatureRecord` with `abstain=True` from both robust-normalization
    fitting and pairwise training -- a short-game or otherwise
    low-evidence record's feature values are exactly the kind of noise
    `score_features.py`'s own `abstain` flag exists to warn about. Pass
    `include_abstained=True` to override this explicitly (e.g. for a
    deliberate research run).

    `converged` is a REAL stopping-criterion flag: `True` only if training
    stopped early because the loss delta or gradient norm dropped below
    `loss_tolerance`/`gradient_tolerance` before `iterations` was
    exhausted (or immediately, in the trivial single-pair-repeated case).
    Running out of the iteration budget without meeting either tolerance
    is honestly reported as `converged=False`.
    """
    evidence_sources = {record.evidence_source for record in dataset.feature_records}
    if len(evidence_sources) > 1:
        raise DatasetValidationError(
            "fit_pairwise_baseline requires a single-tier dataset; found "
            f"tiers {sorted(evidence_sources)} -- call "
            "score_v2.training.export.dataset_for_tier(...) first so "
            "records/pairs are resolved for one evidence tier at a time"
        )

    usable_records = [
        record for record in dataset.feature_records
        if include_abstained or not record.abstain
    ]
    n_items_excluded_abstain = len(dataset.feature_records) - len(usable_records)
    records_by_base_ref = {record.base_ref: record for record in usable_records}

    raw_by_ref: dict[str, dict[str, Optional[float]]] = {}
    for ref, record in records_by_base_ref.items():
        vector = extract_feature_vector(record.features, specs=specs)
        raw_by_ref[ref] = {name: value.transformed for name, value in vector.items()}

    normalizations = {
        spec.name: fit_robust_normalization(
            [raw_by_ref[ref][spec.name] for ref in raw_by_ref]
        )
        for spec in specs
    }

    normalized_by_ref: dict[str, dict[str, float]] = {}
    for ref in raw_by_ref:
        vector = {}
        for spec in specs:
            raw_value = raw_by_ref[ref][spec.name]
            vector[spec.name] = (
                0.0 if raw_value is None else normalizations[spec.name].apply(raw_value)
            )
        normalized_by_ref[ref] = vector

    usable_pairs = []
    skipped = 0
    for label in sorted(dataset.pair_labels, key=lambda label: (label.pair_id, label.reviewer_id)):
        if label.choice == "insufficient_evidence":
            skipped += 1
            continue
        if label.left_ref not in normalized_by_ref or label.right_ref not in normalized_by_ref:
            skipped += 1
            continue
        usable_pairs.append(label)

    specs_by_name = {spec.name: spec for spec in specs}
    coefficients = {spec.name: 0.0 for spec in specs}
    # The intercept is unidentifiable from pairwise-only data (see module
    # docstring) -- it is never trained and always stays exactly 0.0.
    intercept = 0.0
    final_loss: Optional[float] = None
    iterations_run = 0
    converged = False
    prev_loss: Optional[float] = None

    if usable_pairs:
        for _ in range(max(0, iterations)):
            iterations_run += 1
            grad_coeff = {name: 0.0 for name in coefficients}
            total_loss = 0.0
            for label in usable_pairs:
                left_vec = normalized_by_ref[label.left_ref]
                right_vec = normalized_by_ref[label.right_ref]
                s_left = sum(coefficients[name] * left_vec[name] for name in coefficients)
                s_right = sum(coefficients[name] * right_vec[name] for name in coefficients)
                diff = s_left - s_right
                if label.choice == "left":
                    target, weight = 1.0, max(0.0, min(1.0, label.confidence))
                elif label.choice == "right":
                    target, weight = 0.0, max(0.0, min(1.0, label.confidence))
                else:  # "tie"
                    target, weight = 0.5, tie_weight
                prob = sigmoid(diff)
                error = (prob - target) * weight
                total_loss += weight * _binary_cross_entropy(prob, target)
                for name in coefficients:
                    grad_coeff[name] += error * (left_vec[name] - right_vec[name])

            count = len(usable_pairs)
            gradients = {}
            for name in coefficients:
                gradients[name] = grad_coeff[name] / count + l2_lambda * coefficients[name]
            for name in coefficients:
                coefficients[name] -= learning_rate * gradients[name]
                coefficients[name] = _project_sign(
                    coefficients[name], specs_by_name[name].direction,
                )

            loss = total_loss / count
            final_loss = loss
            gradient_norm = math.sqrt(sum(g * g for g in gradients.values()))
            loss_delta = None if prev_loss is None else abs(prev_loss - loss)
            prev_loss = loss
            if (
                (loss_delta is not None and loss_delta < loss_tolerance)
                or gradient_norm < gradient_tolerance
            ):
                converged = True
                break

    return FittedBaseline(
        specs=tuple(specs), normalizations=normalizations, coefficients=dict(coefficients),
        intercept=intercept, n_items=len(records_by_base_ref),
        n_items_excluded_abstain=n_items_excluded_abstain,
        n_pairs_used=len(usable_pairs), n_pairs_skipped=skipped,
        iterations_run=iterations_run, converged=converged, final_loss=final_loss,
    )
