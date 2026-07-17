"""Deterministic, stdlib-only monotonic additive boosting baseline.

Fits

    raw_boosted(x) = sum_t shrinkage * stump_t(x)

where each `stump_t` is a shallow, monotonic WEAK LEARNER: split one
feature at one threshold, predict a constant `low_value` below the
threshold and `high_value` above it, with `low_value <= high_value` (or
`>=`, for a `direction < 0` feature) enforced via
`score_v2.training.monotonic_utils.isotonic_projection`. This is standard
functional gradient boosting (Friedman's GBM recipe) specialized to a
Bradley-Terry pairwise loss and constrained to monotonic weak learners:

  1. Compute each item's pairwise pseudo-gradient (the sum, over every
     pair involving that item, of the loss's partial derivative with
     respect to that item's current score).
  2. Fit the next weak learner to the NEGATIVE of that gradient via
     weighted least squares (the standard steepest-descent-in-function-
     space step) -- deterministically: every `(feature, threshold)`
     candidate is scored by its squared-error reduction and the best one
     wins, ties broken by `(feature name, threshold)`. No randomness, no
     sampling.
  3. Shrink the fitted stump's `low_value`/`high_value` by a fixed
     `shrinkage` factor before adding it to the ensemble (the classic GBM
     regularization device) and add it to a running per-item score.
  4. Stop (`converged=True`) once the best available candidate's
     training-loss improvement drops below tolerance, or the loss delta
     between rounds does -- an intrinsic early-stopping criterion,
     distinct from (and in addition to) the OUTER validation-based model
     selection `score_v2.training.compare` performs across model
     families.

A missing feature value contributes exactly `0.0` to any stump built on
that feature (an item with a missing value participates in neither
branch of that particular candidate split), matching every other model
family's honest missing-feature handling.

**Early stopping must never see the OUTER validation/test split.** The
`validation_dataset` parameter below is a genuinely independent held-out
set -- but the OUTER validation split is reserved exclusively for
`score_v2.training.compare`'s cross-family SELECTION step; if boosting's
early stopping also tuned against it, that split would no longer be an
honest, arms-length judge of every family, and boosting specifically
would gain an unfair "peek" the other three families never get.
`derive_inner_early_stop_split` solves this: it deterministically carves
an INNER (fit, stop) split out of the TRAIN dataset alone (grouped by
connected game-id components via `PairLabel`s, so no pair/group ever
straddles the inner boundary), for boosting's own internal use only. If
train is too small to safely form both non-trivial inner sides, early
stopping is honestly disabled (`validation_dataset=None`, falling back to
the train-loss/no-further-gain stopping criteria) -- the outer
validation/test split is NEVER substituted in as a fallback.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_v2.feature_spec import FEATURE_ALLOWLIST, FeatureSpec
from score_v2.model_shapes import Stump
from score_v2.training.baseline import DEFAULT_TIE_WEIGHT, sigmoid
from score_v2.training.dataset import TrainingDataset, parse_base_ref
from score_v2.training.monotonic_utils import (
    binary_cross_entropy,
    isotonic_projection,
    pairwise_target_and_weight,
    prepare_pairwise_data,
    prepare_pairwise_eval_data,
)

DEFAULT_N_ROUNDS = 100
DEFAULT_SHRINKAGE = 0.15
DEFAULT_MIN_SAMPLES_LEAF = 3
DEFAULT_MIN_GAIN = 1e-9
DEFAULT_LOSS_TOLERANCE = 1e-7
DEFAULT_VALIDATION_PATIENCE = 5

# `derive_inner_early_stop_split` defaults. The seed is a fixed constant
# (not derived from wall-clock time or any run-specific value) so the
# SAME train_dataset always yields a bit-identical inner split, whether
# called during model comparison or later during artifact export/re-
# derivation -- see score_v2.training.compare.build_artifact_for_family.
DEFAULT_INNER_SPLIT_SEED = 20260716
DEFAULT_INNER_STOP_FRACTION = 0.2
DEFAULT_MIN_INNER_GROUPS = 4


def _connected_game_groups(dataset: TrainingDataset) -> list[tuple[int, ...]]:
    """Every distinct `game_id` present in `dataset`, partitioned into
    connected components: two games land in the same group if any
    `PairLabel` connects a participant in one to a participant in the
    other. Grouping (not per-record splitting) guarantees a later
    train/stop partition over these groups can never split a single pair
    across the inner boundary. Deterministic: pairs are processed in a
    fixed sort order and union-find always attaches the larger root under
    the smaller, independent of dict/set iteration order; the returned
    groups are themselves sorted tuples in a sorted list.
    """
    game_ids = sorted({record.game_id for record in dataset.feature_records})
    parent = {game_id: game_id for game_id in game_ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a == root_b:
            return
        if root_a < root_b:
            parent[root_b] = root_a
        else:
            parent[root_a] = root_b

    for pair in sorted(dataset.pair_labels, key=lambda label: (label.pair_id, label.reviewer_id)):
        left_game_id, _ = parse_base_ref(pair.left_ref)
        right_game_id, _ = parse_base_ref(pair.right_ref)
        if left_game_id in parent and right_game_id in parent:
            union(left_game_id, right_game_id)

    groups: dict[int, list[int]] = {}
    for game_id in game_ids:
        groups.setdefault(find(game_id), []).append(game_id)
    return sorted(tuple(sorted(members)) for members in groups.values())


def derive_inner_early_stop_split(
        train_dataset: TrainingDataset, *,
        inner_stop_fraction: float = DEFAULT_INNER_STOP_FRACTION,
        min_groups: int = DEFAULT_MIN_INNER_GROUPS,
        seed: int = DEFAULT_INNER_SPLIT_SEED,
        include_abstained: bool = False,
) -> tuple[Optional[TrainingDataset], Optional[TrainingDataset]]:
    """Deterministically derive an `(inner_fit, inner_stop)` split from
    `train_dataset` ALONE -- never from the outer validation/test split --
    for boosting's own internal early-stopping mechanism.

    Grouped by connected game-id components (`_connected_game_groups`):
    entire groups, never individual records, are assigned to one side or
    the other, so no pair (and no transitively-connected chain of pairs)
    is ever split across the inner boundary. The group order is shuffled
    with a `random.Random(seed)` instance seeded by a FIXED constant (not
    derived from anything time- or run-dependent), so this function is a
    pure, deterministic function of `train_dataset`'s own content -- the
    exact property `score_v2.training.compare.build_artifact_for_family`
    depends on to reconstruct an identical inner split (and therefore an
    identical fitted ensemble/content_hash) when re-deriving a selected
    boosting artifact for export, without needing to persist or pass the
    split separately.

    Returns `(None, None)` if `train_dataset` has fewer than `min_groups`
    independent groups, or if the resulting partition would leave either
    side empty of records or of usable pairs -- an honest "cannot safely
    form both non-trivial inner sides" signal. Callers (see
    `fit_pairwise_boosted_stumps`'s callers in `score_v2.training.compare`)
    must fall back to fitting on the WHOLE `train_dataset` with
    `validation_dataset=None` in that case -- never substituting the
    outer validation/test split as a replacement inner_stop.
    """
    groups = _connected_game_groups(train_dataset)
    if len(groups) < min_groups:
        return None, None

    rng = random.Random(seed)
    shuffled = list(groups)
    rng.shuffle(shuffled)

    records_by_game: dict[int, list] = {}
    for record in train_dataset.feature_records:
        records_by_game.setdefault(record.game_id, []).append(record)
    total_items = len(train_dataset.feature_records)
    target_stop_items = max(1, round(total_items * inner_stop_fraction))

    stop_game_ids: set[int] = set()
    stop_item_count = 0
    max_stop_groups = len(shuffled) - 1  # always reserve >= 1 group for inner_fit
    for index, group in enumerate(shuffled):
        if index >= max_stop_groups or stop_item_count >= target_stop_items:
            break
        for game_id in group:
            stop_game_ids.add(game_id)
            stop_item_count += len(records_by_game.get(game_id, []))

    fit_records = tuple(
        record for record in train_dataset.feature_records if record.game_id not in stop_game_ids
    )
    stop_records = tuple(
        record for record in train_dataset.feature_records if record.game_id in stop_game_ids
    )
    fit_base_refs = {record.base_ref for record in fit_records}
    stop_base_refs = {record.base_ref for record in stop_records}
    fit_pairs = tuple(
        pair for pair in train_dataset.pair_labels
        if pair.left_ref in fit_base_refs and pair.right_ref in fit_base_refs
    )
    stop_pairs = tuple(
        pair for pair in train_dataset.pair_labels
        if pair.left_ref in stop_base_refs and pair.right_ref in stop_base_refs
    )

    usable_fit_refs = {
        record.base_ref for record in fit_records
        if include_abstained or not record.abstain
    }
    usable_stop_refs = {
        record.base_ref for record in stop_records
        if include_abstained or not record.abstain
    }
    usable_fit_pairs = tuple(
        pair for pair in fit_pairs
        if pair.left_ref in usable_fit_refs and pair.right_ref in usable_fit_refs
    )
    usable_stop_pairs = tuple(
        pair for pair in stop_pairs
        if pair.left_ref in usable_stop_refs and pair.right_ref in usable_stop_refs
    )

    if (
        not fit_records or not stop_records
        or not usable_fit_pairs or not usable_stop_pairs
    ):
        return None, None

    fit_dataset = TrainingDataset(
        schema_version=train_dataset.schema_version, feature_records=fit_records,
        pair_labels=fit_pairs,
    )
    stop_dataset = TrainingDataset(
        schema_version=train_dataset.schema_version, feature_records=stop_records,
        pair_labels=stop_pairs,
    )
    return fit_dataset, stop_dataset


@dataclass(frozen=True)
class FittedBoostedStumps:
    specs: tuple[FeatureSpec, ...]
    stumps: tuple[Stump, ...]
    n_items: int
    n_items_excluded_abstain: int
    n_pairs_used: int
    n_pairs_skipped: int
    rounds_run: int
    converged: bool
    stopped_reason: str
    final_loss: Optional[float]
    best_validation_loss: Optional[float]

    @property
    def n_parameters(self) -> int:
        return len(self.stumps) * 3  # threshold + low_value + high_value per stump


def _candidate_thresholds(sorted_present_values: Sequence[float]) -> list[float]:
    """Deterministic CART-style midpoints between consecutive distinct values."""
    distinct = sorted(set(sorted_present_values))
    return [
        (distinct[index] + distinct[index + 1]) / 2.0 for index in range(len(distinct) - 1)
    ]


def _mean_pairwise_loss(pairs, current_score: Mapping[str, float], tie_weight: float) -> Optional[float]:
    if not pairs:
        return None
    total = 0.0
    for label in pairs:
        diff = current_score.get(label.left_ref, 0.0) - current_score.get(label.right_ref, 0.0)
        target, weight = pairwise_target_and_weight(label.choice, label.confidence, tie_weight)
        total += weight * binary_cross_entropy(sigmoid(diff), target)
    return total / len(pairs)


def fit_pairwise_boosted_stumps(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec] = FEATURE_ALLOWLIST,
        n_rounds: int = DEFAULT_N_ROUNDS, shrinkage: float = DEFAULT_SHRINKAGE,
        min_samples_leaf: int = DEFAULT_MIN_SAMPLES_LEAF, min_gain: float = DEFAULT_MIN_GAIN,
        tie_weight: float = DEFAULT_TIE_WEIGHT, loss_tolerance: float = DEFAULT_LOSS_TOLERANCE,
        include_abstained: bool = False,
        validation_dataset: Optional[TrainingDataset] = None,
        validation_patience: int = DEFAULT_VALIDATION_PATIENCE) -> FittedBoostedStumps:
    """Fit one evidence tier's regularized monotonic boosted-stump ensemble.

    See the module docstring for the algorithm. `min_samples_leaf` is the
    minimum item count required on EACH side of a candidate split for it
    to be considered at all (a complexity/overfitting guardrail, on top
    of whatever minimum `score_v2.training.compare` requires before even
    attempting this family).

    If `validation_dataset` is supplied (a single-tier `TrainingDataset`,
    normalized using THIS run's train-fit statistics via
    `prepare_pairwise_eval_data` -- never refit on validation data),
    boosting also tracks validation loss each round and stops early
    (`stopped_reason="validation_plateau"`) once it has not improved for
    `validation_patience` consecutive rounds. This is standard
    early-stopping practice (deciding WHEN to stop, never changing what
    is fit) and is a materially different, additional safeguard against
    overfitting from the training-loss/gain-based stopping below --
    boosting is the highest-capacity of the non-tree candidates here, so
    it is the one family where unrestrained rounds can most easily overfit.
    """
    prepared = prepare_pairwise_data(dataset, specs=specs, include_abstained=include_abstained)
    refs = list(prepared.normalized_by_ref)

    validation_prepared = None
    if validation_dataset is not None:
        validation_prepared = prepare_pairwise_eval_data(
            validation_dataset, specs=specs, normalizations=prepared.normalizations,
            include_abstained=include_abstained,
        )

    candidate_thresholds: dict[str, list[float]] = {}
    for spec in specs:
        present = [
            prepared.normalized_by_ref[ref][spec.name] for ref in refs
            if prepared.normalized_by_ref[ref][spec.name] is not None
        ]
        candidate_thresholds[spec.name] = _candidate_thresholds(present)

    current_score = {ref: 0.0 for ref in refs}
    validation_score = (
        {ref: 0.0 for ref in validation_prepared.normalized_by_ref}
        if validation_prepared is not None else None
    )
    stumps: list[Stump] = []
    final_loss: Optional[float] = None
    rounds_run = 0
    converged = False
    stopped_reason = "iteration_budget_exhausted"
    prev_loss: Optional[float] = None
    best_validation_loss: Optional[float] = (
        _mean_pairwise_loss(
            validation_prepared.usable_pairs, validation_score, tie_weight,
        )
        if validation_prepared is not None else None
    )
    best_validation_round = 0
    best_validation_stump_count = 0

    if prepared.usable_pairs:
        for _ in range(max(0, n_rounds)):
            rounds_run += 1
            # Step 1: per-item pairwise pseudo-gradient (accumulated over
            # every pair involving that item), and the current loss.
            gradient = {ref: 0.0 for ref in refs}
            total_loss = 0.0
            for label in prepared.usable_pairs:
                diff = current_score[label.left_ref] - current_score[label.right_ref]
                target, weight = pairwise_target_and_weight(
                    label.choice, label.confidence, tie_weight,
                )
                prob = sigmoid(diff)
                error = (prob - target) * weight
                total_loss += weight * binary_cross_entropy(prob, target)
                gradient[label.left_ref] += error
                gradient[label.right_ref] -= error

            loss = total_loss / len(prepared.usable_pairs)
            final_loss = loss
            loss_delta = None if prev_loss is None else abs(prev_loss - loss)
            prev_loss = loss

            # Step 2: fit the next monotonic stump to the negative gradient.
            fit_target = {ref: -gradient[ref] for ref in refs}
            best_gain: Optional[float] = None
            best_stump: Optional[Stump] = None
            for spec in specs:
                name = spec.name
                thresholds = candidate_thresholds[name]
                if not thresholds:
                    continue
                values_by_ref = {
                    ref: prepared.normalized_by_ref[ref][name] for ref in refs
                    if prepared.normalized_by_ref[ref][name] is not None
                }
                if len(values_by_ref) < 2 * min_samples_leaf:
                    continue
                for threshold in thresholds:
                    low_refs = [ref for ref, value in values_by_ref.items() if value <= threshold]
                    high_refs = [ref for ref, value in values_by_ref.items() if value > threshold]
                    if len(low_refs) < min_samples_leaf or len(high_refs) < min_samples_leaf:
                        continue
                    low_mean = sum(fit_target[ref] for ref in low_refs) / len(low_refs)
                    high_mean = sum(fit_target[ref] for ref in high_refs) / len(high_refs)
                    low_value, high_value = isotonic_projection(
                        [low_mean, high_mean], spec.direction,
                    )
                    sse_before = sum(fit_target[ref] ** 2 for ref in low_refs + high_refs)
                    sse_after = (
                        sum((fit_target[ref] - low_value) ** 2 for ref in low_refs)
                        + sum((fit_target[ref] - high_value) ** 2 for ref in high_refs)
                    )
                    gain = sse_before - sse_after
                    candidate_key = (gain, name, threshold)
                    best_key = (
                        (best_gain, best_stump.spec.name, best_stump.threshold)
                        if best_stump is not None else None
                    )
                    if best_key is None or candidate_key > best_key:
                        best_gain = gain
                        best_stump = Stump(
                            spec=spec,
                            robust_center=prepared.normalizations[name].center,
                            robust_scale=prepared.normalizations[name].scale,
                            threshold=threshold, low_value=low_value, high_value=high_value,
                        )

            if best_stump is None or best_gain is None or best_gain <= min_gain:
                converged = True
                stopped_reason = "no_further_gain"
                break

            shrunk_stump = Stump(
                spec=best_stump.spec, robust_center=best_stump.robust_center,
                robust_scale=best_stump.robust_scale, threshold=best_stump.threshold,
                low_value=best_stump.low_value * shrinkage,
                high_value=best_stump.high_value * shrinkage,
            )
            stumps.append(shrunk_stump)
            for ref in refs:
                value = prepared.normalized_by_ref[ref][shrunk_stump.spec.name]
                current_score[ref] += shrunk_stump.evaluate(value)

            if validation_prepared is not None:
                for ref in validation_score:
                    value = validation_prepared.normalized_by_ref[ref][shrunk_stump.spec.name]
                    validation_score[ref] += shrunk_stump.evaluate(value)
                validation_loss = _mean_pairwise_loss(
                    validation_prepared.usable_pairs, validation_score, tie_weight,
                )
                if validation_loss is not None:
                    if best_validation_loss is None or validation_loss < best_validation_loss:
                        best_validation_loss = validation_loss
                        best_validation_round = rounds_run
                        best_validation_stump_count = len(stumps)
                    elif rounds_run - best_validation_round >= validation_patience:
                        # Roll back to the best-validation-loss checkpoint --
                        # the rounds since then only helped train loss.
                        stumps = stumps[:best_validation_stump_count]
                        converged = True
                        stopped_reason = "validation_plateau"
                        break

            if loss_delta is not None and loss_delta < loss_tolerance:
                converged = True
                stopped_reason = "train_loss_plateau"
                break

    if validation_prepared is not None and best_validation_loss is not None:
        stumps = stumps[:best_validation_stump_count]

    return FittedBoostedStumps(
        specs=tuple(specs), stumps=tuple(stumps),
        n_items=prepared.n_items, n_items_excluded_abstain=prepared.n_items_excluded_abstain,
        n_pairs_used=len(prepared.usable_pairs), n_pairs_skipped=prepared.n_pairs_skipped,
        rounds_run=rounds_run, converged=converged, stopped_reason=stopped_reason,
        final_loss=final_loss, best_validation_loss=best_validation_loss,
    )
