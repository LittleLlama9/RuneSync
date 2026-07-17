"""Shared, stdlib-only utilities for every pairwise-supervised model family
(the linear baseline, monotonic GAM, monotonic boosted stumps, and the
monotonic tree).

Centralizes the parts of pairwise-supervised training that are family
agnostic:

  * `prepare_pairwise_data` -- single-tier enforcement, abstain exclusion,
    per-feature extraction + robust normalization, and pairwise-label
    filtering (`insufficient_evidence`/unmatched-ref skipping). This is
    the exact same preparation `score_v2.training.baseline.fit_pairwise_baseline`
    performs internally; every model family builds on this SAME prepared
    view of a dataset so comparisons across families are apples-to-apples
    (same items, same normalization, same usable pairs).
  * `prepare_pairwise_eval_data` -- the validation/test-safe counterpart:
    identical preparation, but normalizes using an ALREADY-FIT
    `normalizations` mapping (from a training split) instead of fitting
    fresh statistics from the split being prepared. This is what
    guarantees "no validation/test leakage" -- held-out data can never
    influence normalization center/scale.
  * `pairwise_target_and_weight` -- turns one `PairLabel` into a
    `(target, weight)` pair for a Bradley-Terry-style pairwise logistic
    loss (`P(left beats right) = sigmoid(s(left) - s(right))`).
  * `isotonic_projection` -- Pool Adjacent Violators Algorithm (PAVA), the
    L2-optimal projection of a sequence onto the monotonic cone. This is
    the principled generalization of the linear baseline's scalar
    sign-projection to an ORDERED sequence of values (GAM bin/knot
    values, or the two child values of a monotonic stump/tree split) --
    the true nearest monotonic sequence under squared error, not an ad
    hoc clip.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_v2.feature_spec import FeatureSpec, extract_feature_vector
from score_v2.training.baseline import RobustNormalization, fit_robust_normalization
from score_v2.training.dataset import DatasetValidationError, PairLabel, TrainingDataset


@dataclass(frozen=True)
class PreparedPairwiseData:
    specs: tuple[FeatureSpec, ...]
    normalizations: Mapping[str, RobustNormalization]
    normalized_by_ref: Mapping[str, Mapping[str, Optional[float]]]
    usable_pairs: tuple[PairLabel, ...]
    n_items: int
    n_items_excluded_abstain: int
    n_pairs_skipped: int


def _single_tier_check(dataset: TrainingDataset, *, caller_name: str) -> None:
    evidence_sources = {record.evidence_source for record in dataset.feature_records}
    if len(evidence_sources) > 1:
        raise DatasetValidationError(
            f"{caller_name} requires a single-tier dataset; found tiers "
            f"{sorted(evidence_sources)} -- call "
            "score_v2.training.export.dataset_for_tier(...) first so "
            "records/pairs are resolved for one evidence tier at a time"
        )


def _extract_raw_by_ref(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec],
        include_abstained: bool,
) -> tuple[dict, dict, int]:
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
    return records_by_base_ref, raw_by_ref, n_items_excluded_abstain


def _normalize_raw_by_ref(
        raw_by_ref: Mapping[str, Mapping[str, Optional[float]]],
        specs: Sequence[FeatureSpec],
        normalizations: Mapping[str, RobustNormalization],
) -> dict[str, dict[str, Optional[float]]]:
    normalized_by_ref: dict[str, dict[str, Optional[float]]] = {}
    for ref in raw_by_ref:
        vector = {}
        for spec in specs:
            raw_value = raw_by_ref[ref][spec.name]
            vector[spec.name] = (
                None if raw_value is None else normalizations[spec.name].apply(raw_value)
            )
        normalized_by_ref[ref] = vector
    return normalized_by_ref


def _filter_usable_pairs(
        pair_labels: Sequence[PairLabel],
        normalized_by_ref: Mapping[str, Mapping[str, Optional[float]]],
) -> tuple[tuple[PairLabel, ...], int]:
    usable_pairs = []
    skipped = 0
    for label in sorted(pair_labels, key=lambda label: (label.pair_id, label.reviewer_id)):
        if label.choice == "insufficient_evidence":
            skipped += 1
            continue
        if label.left_ref not in normalized_by_ref or label.right_ref not in normalized_by_ref:
            skipped += 1
            continue
        usable_pairs.append(label)
    return tuple(usable_pairs), skipped


def prepare_pairwise_data(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec],
        include_abstained: bool = False) -> PreparedPairwiseData:
    """Prepare one tier's dataset for ANY pairwise-supervised model family.

    `normalized_by_ref[ref][name]` is `None` when that feature was absent
    for that item (missing -- contributes exactly nothing to every model
    family's score, never a guessed/imputed value) and a normalized float
    otherwise. Raises `DatasetValidationError` if `dataset` mixes more
    than one evidence tier (see `score_v2.training.export.dataset_for_tier`).

    Fits normalization FROM `dataset` itself -- use this only for a
    TRAINING split. Evaluating a validation/test split must go through
    `prepare_pairwise_eval_data` instead, which reuses already-fit
    normalization rather than deriving new statistics from held-out data.
    """
    _single_tier_check(dataset, caller_name="prepare_pairwise_data")
    records_by_base_ref, raw_by_ref, n_items_excluded_abstain = _extract_raw_by_ref(
        dataset, specs=specs, include_abstained=include_abstained,
    )

    normalizations = {
        spec.name: fit_robust_normalization(
            [raw_by_ref[ref][spec.name] for ref in raw_by_ref]
        )
        for spec in specs
    }

    normalized_by_ref = _normalize_raw_by_ref(raw_by_ref, specs, normalizations)
    usable_pairs, skipped = _filter_usable_pairs(dataset.pair_labels, normalized_by_ref)

    return PreparedPairwiseData(
        specs=tuple(specs), normalizations=normalizations, normalized_by_ref=normalized_by_ref,
        usable_pairs=usable_pairs, n_items=len(records_by_base_ref),
        n_items_excluded_abstain=n_items_excluded_abstain, n_pairs_skipped=skipped,
    )


def prepare_pairwise_eval_data(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec],
        normalizations: Mapping[str, RobustNormalization],
        include_abstained: bool = False) -> PreparedPairwiseData:
    """Prepare a VALIDATION/TEST split using already-fit `normalizations`.

    This is the one sanctioned way to prepare a held-out split for
    evaluation: it guarantees held-out data can never influence the
    normalization center/scale, which would otherwise be a subtle form of
    validation/test leakage (the model's notion of "typical"/"scale"
    would then be partly informed by the very data used to judge it).
    `normalizations` should be exactly the mapping a training run of the
    SAME `specs` already produced (`PreparedPairwiseData.normalizations`,
    or an artifact's stored per-feature `robust_center`/`robust_scale`).
    """
    _single_tier_check(dataset, caller_name="prepare_pairwise_eval_data")
    records_by_base_ref, raw_by_ref, n_items_excluded_abstain = _extract_raw_by_ref(
        dataset, specs=specs, include_abstained=include_abstained,
    )
    normalized_by_ref = _normalize_raw_by_ref(raw_by_ref, specs, normalizations)
    usable_pairs, skipped = _filter_usable_pairs(dataset.pair_labels, normalized_by_ref)

    return PreparedPairwiseData(
        specs=tuple(specs), normalizations=normalizations, normalized_by_ref=normalized_by_ref,
        usable_pairs=usable_pairs, n_items=len(records_by_base_ref),
        n_items_excluded_abstain=n_items_excluded_abstain, n_pairs_skipped=skipped,
    )


def pairwise_target_and_weight(
        choice: str, confidence: float, tie_weight: float) -> tuple[float, float]:
    """`(target, weight)` for one pair's Bradley-Terry-style loss term.

    `choice` must already be `"left"`, `"right"`, or `"tie"` --
    `"insufficient_evidence"` pairs are filtered out by
    `prepare_pairwise_data` before this is ever called.
    """
    if choice == "left":
        return 1.0, max(0.0, min(1.0, confidence))
    if choice == "right":
        return 0.0, max(0.0, min(1.0, confidence))
    if choice == "tie":
        return 0.5, tie_weight
    raise ValueError(f"Unexpected pair choice {choice!r} reached pairwise training")


def binary_cross_entropy(prob: float, target: float) -> float:
    eps = 1e-9
    clipped = min(1.0 - eps, max(eps, prob))
    return -(target * math.log(clipped) + (1 - target) * math.log(1 - clipped))


def isotonic_projection(values: Sequence[float], direction: int) -> list[float]:
    """Pool Adjacent Violators Algorithm (PAVA).

    Returns the L2-nearest sequence to `values` that is non-decreasing
    (`direction > 0`), non-increasing (`direction < 0`), or `values`
    unchanged (`direction == 0`, unconstrained). This is the textbook
    isotonic-regression algorithm: values are merged into contiguous
    "blocks" (each block's fitted value is the mean of its members)
    whenever adjacent blocks would otherwise violate the monotonic order,
    which is provably the nearest-in-L2 monotonic sequence.
    """
    if direction == 0 or len(values) <= 1:
        return list(values)
    if direction < 0:
        return [-value for value in isotonic_projection([-value for value in values], 1)]

    # Non-decreasing PAVA via a stack of (sum, count) blocks.
    blocks: list[list[float]] = []
    for value in values:
        blocks.append([value, 1.0])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            last_sum, last_count = blocks.pop()
            blocks[-1][0] += last_sum
            blocks[-1][1] += last_count
    result: list[float] = []
    for total, count in blocks:
        result.extend([total / count] * int(count))
    return result
