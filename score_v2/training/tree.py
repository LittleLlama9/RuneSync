"""Deterministic, stdlib-only monotonic tree baseline.

Fits a SINGLE, depth-bounded regression tree over the (normalized, already
robust-scaled) feature space, splitting on whichever feature/threshold
most reduces squared error against a per-item pairwise pseudo-target at
each node -- a genuine, constrained CART, not a repackaged stump. This is
deliberately a DIFFERENT model class from both
`score_v2.training.gam` (smooth per-feature shapes fit jointly by
gradient descent) and `score_v2.training.boosting` (a many-round additive
ensemble of independent depth-1 stumps): a tree can split on one feature,
then a DIFFERENT feature within each branch, capturing feature
INTERACTIONS neither of the other two families can represent.

Monotonicity is guaranteed by construction via VALUE-RANGE PROPAGATION
(the same technique used by monotonic-constraint implementations in
mainstream gradient boosting libraries): every node inherits an allowed
prediction interval `[lo, hi]` from its parent (the root's is
`(-inf, +inf)`). When a node splits on a `direction > 0` feature at
threshold `t`, the "low" branch (`value <= t`) is constrained to
`[lo, mid]` and the "high" branch (`value > t`) to `[mid, hi]` for some
data-chosen `mid` inside `[lo, hi]` (`direction < 0` swaps which side gets
the higher sub-interval; `direction == 0` leaves both children with the
SAME unconstrained interval). Every leaf's value is clipped into its
inherited interval. Because child intervals never cross, and this holds
at EVERY split regardless of which feature it is on, the WHOLE TREE is
provably monotonic in every monotonic feature used anywhere in it --
`score_v2.artifact.Artifact.validate()` re-verifies this structurally by
walking the tree bottom-up and checking the invariant holds at every
internal node (see `tree_value_range`), independent of trusting this
training code.

The per-item regression target is the standard pairwise-to-pointwise
proxy: each item's target is the aggregated pairwise pseudo-gradient at
the all-zero baseline score (equivalent to a signed, confidence-weighted
net-preference tally across every pair involving that item) -- the same
first-order signal `score_v2.training.boosting`'s first round fits, but
here fit ONCE via a real recursive tree instead of many rounds of
independent stumps.

A missing feature value at any split encountered while routing an item
means the WHOLE TREE's contribution for that item is honestly `0.0` (we
do not guess which branch a missing value belongs to) -- consistent with
every other model family's missing-feature handling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_v2.feature_spec import FEATURE_ALLOWLIST, FeatureSpec
from score_v2.model_shapes import (
    TreeNode,
    tree_depth,
    tree_node_count,
    tree_value_range,
    verify_tree_monotonicity,
)
from score_v2.training.baseline import DEFAULT_TIE_WEIGHT, sigmoid
from score_v2.training.dataset import TrainingDataset
from score_v2.training.monotonic_utils import binary_cross_entropy, pairwise_target_and_weight, prepare_pairwise_data

DEFAULT_MAX_DEPTH = 3
DEFAULT_MIN_SAMPLES_LEAF = 4
DEFAULT_MIN_GAIN = 1e-9


@dataclass(frozen=True)
class FittedMonotonicTree:
    specs: tuple[FeatureSpec, ...]
    root: TreeNode
    n_items: int
    n_items_excluded_abstain: int
    n_pairs_used: int
    n_pairs_skipped: int
    final_loss: Optional[float]

    @property
    def depth(self) -> int:
        return tree_depth(self.root)

    @property
    def n_parameters(self) -> int:
        return tree_node_count(self.root)


def _candidate_thresholds(values: Sequence[float]) -> list[float]:
    distinct = sorted(set(values))
    return [(distinct[i] + distinct[i + 1]) / 2.0 for i in range(len(distinct) - 1)]


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _fit_node(
        refs: Sequence[str], targets: Mapping[str, float],
        normalized_by_ref: Mapping[str, Mapping[str, Optional[float]]],
        normalizations: Mapping, specs: Sequence[FeatureSpec],
        depth: int, max_depth: int, min_samples_leaf: int, min_gain: float,
        lo: float, hi: float) -> TreeNode:
    parent_value = _clip(sum(targets[ref] for ref in refs) / len(refs), lo, hi)

    if depth >= max_depth or len(refs) < 2 * min_samples_leaf:
        return TreeNode(is_leaf=True, value=parent_value)

    sse_parent = sum((targets[ref] - parent_value) ** 2 for ref in refs)

    best_gain: Optional[float] = None
    best_choice = None  # (spec, threshold, low_refs, high_refs, low_value, high_value)
    for spec in specs:
        name = spec.name
        values_by_ref = {
            ref: normalized_by_ref[ref][name] for ref in refs
            if normalized_by_ref[ref][name] is not None
        }
        if len(values_by_ref) < 2 * min_samples_leaf:
            continue
        for threshold in _candidate_thresholds(list(values_by_ref.values())):
            low_refs = [ref for ref, value in values_by_ref.items() if value <= threshold]
            high_refs = [ref for ref, value in values_by_ref.items() if value > threshold]
            if len(low_refs) < min_samples_leaf or len(high_refs) < min_samples_leaf:
                continue
            low_mean = sum(targets[ref] for ref in low_refs) / len(low_refs)
            high_mean = sum(targets[ref] for ref in high_refs) / len(high_refs)
            mid = _clip((low_mean + high_mean) / 2.0, lo, hi)
            if spec.direction > 0:
                low_interval, high_interval = (lo, mid), (mid, hi)
            elif spec.direction < 0:
                low_interval, high_interval = (mid, hi), (lo, mid)
            else:
                low_interval = high_interval = (lo, hi)
            low_value = _clip(low_mean, *low_interval)
            high_value = _clip(high_mean, *high_interval)
            sse_after = (
                sum((targets[ref] - low_value) ** 2 for ref in low_refs)
                + sum((targets[ref] - high_value) ** 2 for ref in high_refs)
            )
            gain = sse_parent - sse_after
            candidate_key = (gain, name, threshold)
            current_best_key = (
                (best_gain, best_choice[0].name, best_choice[1]) if best_choice is not None else None
            )
            if current_best_key is None or candidate_key > current_best_key:
                best_gain = gain
                best_choice = (
                    spec, threshold, low_refs, high_refs, low_value, high_value,
                    low_interval, high_interval,
                )

    if best_choice is None or best_gain is None or best_gain <= min_gain:
        return TreeNode(is_leaf=True, value=parent_value)

    spec, threshold, low_refs, high_refs, low_value, high_value, low_interval, high_interval = best_choice
    low_child = _fit_node(
        low_refs, targets, normalized_by_ref, normalizations, specs,
        depth + 1, max_depth, min_samples_leaf, min_gain, *low_interval,
    )
    high_child = _fit_node(
        high_refs, targets, normalized_by_ref, normalizations, specs,
        depth + 1, max_depth, min_samples_leaf, min_gain, *high_interval,
    )
    return TreeNode(
        is_leaf=False, spec=spec,
        robust_center=normalizations[spec.name].center, robust_scale=normalizations[spec.name].scale,
        threshold=threshold, low=low_child, high=high_child,
    )


def fit_pairwise_monotonic_tree(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec] = FEATURE_ALLOWLIST,
        max_depth: int = DEFAULT_MAX_DEPTH, min_samples_leaf: int = DEFAULT_MIN_SAMPLES_LEAF,
        min_gain: float = DEFAULT_MIN_GAIN, tie_weight: float = DEFAULT_TIE_WEIGHT,
        include_abstained: bool = False) -> FittedMonotonicTree:
    """Fit one evidence tier's regularized monotonic tree.

    See the module docstring for the algorithm and monotonicity guarantee.
    `max_depth`/`min_samples_leaf` are the complexity guardrails --
    `score_v2.training.compare` additionally requires a larger minimum
    sample count before even attempting this family, and treats a
    resulting tree with `depth <= 1` (i.e. no real split survived the
    guardrails) as ineligible rather than presenting a degenerate
    single-split tree as a genuine tree-family candidate.
    """
    prepared = prepare_pairwise_data(dataset, specs=specs, include_abstained=include_abstained)
    refs = list(prepared.normalized_by_ref)

    # Point-wise proxy target: each item's aggregated, confidence-weighted
    # pairwise pseudo-gradient at the all-zero baseline score (every pair's
    # sigmoid(0) == 0.5), negated -- the same first-order signal
    # `score_v2.training.boosting`'s first round fits, but consumed once
    # by a real recursive tree instead of many independent stumps.
    gradient = {ref: 0.0 for ref in refs}
    total_loss = 0.0
    for label in prepared.usable_pairs:
        target, weight = pairwise_target_and_weight(label.choice, label.confidence, tie_weight)
        prob = sigmoid(0.0)
        error = (prob - target) * weight
        total_loss += weight * binary_cross_entropy(prob, target)
        gradient[label.left_ref] += error
        gradient[label.right_ref] -= error
    targets = {ref: -gradient[ref] for ref in refs}
    final_loss = (total_loss / len(prepared.usable_pairs)) if prepared.usable_pairs else None

    if prepared.usable_pairs and refs:
        root = _fit_node(
            refs, targets, prepared.normalized_by_ref, prepared.normalizations, specs,
            depth=0, max_depth=max_depth, min_samples_leaf=min_samples_leaf, min_gain=min_gain,
            lo=float("-inf"), hi=float("inf"),
        )
    else:
        root = TreeNode(is_leaf=True, value=0.0)

    return FittedMonotonicTree(
        specs=tuple(specs), root=root, n_items=prepared.n_items,
        n_items_excluded_abstain=prepared.n_items_excluded_abstain,
        n_pairs_used=len(prepared.usable_pairs), n_pairs_skipped=prepared.n_pairs_skipped,
        final_loss=final_loss,
    )
