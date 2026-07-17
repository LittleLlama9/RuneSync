"""Deterministic, stdlib-only monotonic Generalized Additive Model (GAM)
baseline.

Fits

    raw_gam(x) = sum_i shape_i(x_i)

where each `shape_i` is a per-feature, monotonic, PIECEWISE-LINEAR
function defined by a small number of knots (x-position, y-value pairs).
Unlike the linear baseline (`score_v2.training.baseline`, one scalar
coefficient per feature), a GAM shape can represent a genuinely
NON-LINEAR monotonic relationship (e.g. sharply increasing then
flattening out) while still being provably monotonic end to end.

Training is full-batch gradient descent on the same confidence-weighted
pairwise logistic loss the linear baseline uses, treating each knot
y-value as its own parameter (piecewise-linear interpolation is linear in
the knot y-values for a fixed x, so each knot's gradient is just its
local interpolation weight), with:

  * L2 regularization on every knot value (shrinks each shape toward the
    flat "no effect" function, same spirit as the linear baseline),
  * a monotonic PROJECTION after every gradient step: each feature's knot
    y-values are re-sorted onto the monotonic cone via
    `score_v2.training.monotonic_utils.isotonic_projection` (the
    generalization of the linear baseline's scalar sign-clip to an
    ordered sequence),
  * the SAME robust (median/MAD) normalization and single-tier/abstain
    handling as every other model family, via
    `score_v2.training.monotonic_utils.prepare_pairwise_data`.

Knot x-positions are fixed once, deterministically, from the TRAINING
split's own empirical quantiles (`_choose_knot_positions`) -- never from
validation/test data, and never randomized. A missing feature value
contributes exactly `0.0` to the score (like every other family here),
never a guessed `shape(0)`.

This is deliberately NOT implemented with numpy/scipy/sklearn -- see
`docs/SCORE_V2_MODELS.md` ("Why a linear baseline") for why a
higher-capacity model needs proportionally more data before it is even
attempted, enforced by `score_v2.training.compare`'s minimum-sample
guardrails, not by this module refusing to fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_v2.feature_spec import FEATURE_ALLOWLIST, FeatureSpec
from score_v2.model_shapes import FeatureShapeFit, interpolation_weights
from score_v2.training.baseline import (
    DEFAULT_GRADIENT_TOLERANCE,
    DEFAULT_ITERATIONS,
    DEFAULT_L2_LAMBDA,
    DEFAULT_LEARNING_RATE,
    DEFAULT_LOSS_TOLERANCE,
    DEFAULT_TIE_WEIGHT,
    sigmoid,
)
from score_v2.training.dataset import TrainingDataset
from score_v2.training.monotonic_utils import (
    binary_cross_entropy,
    isotonic_projection,
    pairwise_target_and_weight,
    prepare_pairwise_data,
)

DEFAULT_MAX_KNOTS = 5
MIN_KNOTS = 2


@dataclass(frozen=True)
class FittedGAM:
    specs: tuple[FeatureSpec, ...]
    shapes: Mapping[str, FeatureShapeFit]
    n_items: int
    n_items_excluded_abstain: int
    n_pairs_used: int
    n_pairs_skipped: int
    iterations_run: int
    converged: bool
    final_loss: Optional[float]

    @property
    def n_parameters(self) -> int:
        return sum(len(shape.knot_y) for shape in self.shapes.values())


def _choose_knot_positions(values: Sequence[float], max_knots: int) -> list[float]:
    """Deterministic quantile-spaced knot x-positions from `values`.

    Falls back to a small symmetric window around a constant/degenerate
    feature (0 or 1 distinct values) rather than crashing -- an
    honest "no real shape to learn" case, not an error.
    """
    distinct_sorted = sorted(set(values))
    if not distinct_sorted:
        return [-1.0, 1.0]
    if len(distinct_sorted) == 1:
        value = distinct_sorted[0]
        return [value - 1.0, value + 1.0]

    n_knots = max(MIN_KNOTS, min(max_knots, len(distinct_sorted)))
    positions: list[float] = []
    for index in range(n_knots):
        fraction = index / (n_knots - 1)
        exact_index = fraction * (len(distinct_sorted) - 1)
        lower = int(math.floor(exact_index))
        upper = min(lower + 1, len(distinct_sorted) - 1)
        weight = exact_index - lower
        positions.append(
            distinct_sorted[lower] * (1 - weight) + distinct_sorted[upper] * weight
        )

    deduped: list[float] = []
    for position in positions:
        if not deduped or position > deduped[-1] + 1e-12:
            deduped.append(position)
    if len(deduped) < MIN_KNOTS:
        value = deduped[0] if deduped else 0.0
        return [value - 1.0, value + 1.0]
    return deduped


def fit_pairwise_gam(
        dataset: TrainingDataset, *, specs: Sequence[FeatureSpec] = FEATURE_ALLOWLIST,
        max_knots: int = DEFAULT_MAX_KNOTS, l2_lambda: float = DEFAULT_L2_LAMBDA,
        learning_rate: float = DEFAULT_LEARNING_RATE, iterations: int = DEFAULT_ITERATIONS,
        tie_weight: float = DEFAULT_TIE_WEIGHT, loss_tolerance: float = DEFAULT_LOSS_TOLERANCE,
        gradient_tolerance: float = DEFAULT_GRADIENT_TOLERANCE,
        include_abstained: bool = False) -> FittedGAM:
    """Fit one evidence tier's regularized monotonic GAM.

    See the module docstring for the model form and monotonicity
    guarantee. `converged`/tolerances/abstain handling mirror
    `score_v2.training.baseline.fit_pairwise_baseline` exactly, via the
    shared `score_v2.training.monotonic_utils.prepare_pairwise_data`.
    """
    prepared = prepare_pairwise_data(dataset, specs=specs, include_abstained=include_abstained)

    knot_x_by_feature: dict[str, list[float]] = {}
    for spec in specs:
        present_values = [
            prepared.normalized_by_ref[ref][spec.name]
            for ref in prepared.normalized_by_ref
            if prepared.normalized_by_ref[ref][spec.name] is not None
        ]
        knot_x_by_feature[spec.name] = _choose_knot_positions(present_values, max_knots)

    knot_y_by_feature: dict[str, list[float]] = {
        spec.name: [0.0] * len(knot_x_by_feature[spec.name]) for spec in specs
    }

    # Precompute each item's (at most 2 nonzero) interpolation weights per
    # feature ONCE -- knot x-positions never change during training, only
    # knot y-values do.
    item_weights: dict[str, dict[str, dict[int, float]]] = {}
    for ref, vector in prepared.normalized_by_ref.items():
        item_weights[ref] = {}
        for spec in specs:
            value = vector[spec.name]
            item_weights[ref][spec.name] = (
                {} if value is None else interpolation_weights(value, knot_x_by_feature[spec.name])
            )

    def score_of(ref: str) -> float:
        total = 0.0
        for spec in specs:
            weights = item_weights[ref][spec.name]
            knot_y = knot_y_by_feature[spec.name]
            total += sum(weight * knot_y[index] for index, weight in weights.items())
        return total

    specs_by_name = {spec.name: spec for spec in specs}
    final_loss: Optional[float] = None
    iterations_run = 0
    converged = False
    prev_loss: Optional[float] = None

    if prepared.usable_pairs:
        for _ in range(max(0, iterations)):
            iterations_run += 1
            grad_knot_y: dict[str, list[float]] = {
                name: [0.0] * len(values) for name, values in knot_y_by_feature.items()
            }
            total_loss = 0.0
            for label in prepared.usable_pairs:
                s_left, s_right = score_of(label.left_ref), score_of(label.right_ref)
                diff = s_left - s_right
                target, weight = pairwise_target_and_weight(
                    label.choice, label.confidence, tie_weight,
                )
                prob = sigmoid(diff)
                error = (prob - target) * weight
                total_loss += weight * binary_cross_entropy(prob, target)
                for spec in specs:
                    left_weights = item_weights[label.left_ref][spec.name]
                    right_weights = item_weights[label.right_ref][spec.name]
                    grad = grad_knot_y[spec.name]
                    for index, w in left_weights.items():
                        grad[index] += error * w
                    for index, w in right_weights.items():
                        grad[index] -= error * w

            count = len(prepared.usable_pairs)
            gradient_norm_sq = 0.0
            for spec in specs:
                name = spec.name
                knot_y = knot_y_by_feature[name]
                grad = grad_knot_y[name]
                for index in range(len(knot_y)):
                    step = grad[index] / count + l2_lambda * knot_y[index]
                    gradient_norm_sq += step * step
                    knot_y[index] -= learning_rate * step
                knot_y_by_feature[name] = isotonic_projection(knot_y, spec.direction)

            loss = total_loss / count
            final_loss = loss
            gradient_norm = math.sqrt(gradient_norm_sq)
            loss_delta = None if prev_loss is None else abs(prev_loss - loss)
            prev_loss = loss
            if (
                (loss_delta is not None and loss_delta < loss_tolerance)
                or gradient_norm < gradient_tolerance
            ):
                converged = True
                break

    shapes = {
        spec.name: FeatureShapeFit(
            spec=spec,
            robust_center=prepared.normalizations[spec.name].center,
            robust_scale=prepared.normalizations[spec.name].scale,
            knot_x=tuple(knot_x_by_feature[spec.name]),
            knot_y=tuple(knot_y_by_feature[spec.name]),
        )
        for spec in specs
    }

    return FittedGAM(
        specs=tuple(specs), shapes=shapes, n_items=prepared.n_items,
        n_items_excluded_abstain=prepared.n_items_excluded_abstain,
        n_pairs_used=len(prepared.usable_pairs), n_pairs_skipped=prepared.n_pairs_skipped,
        iterations_run=iterations_run, converged=converged, final_loss=final_loss,
    )
