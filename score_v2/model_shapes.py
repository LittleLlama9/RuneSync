"""Non-linear model shape representations, shared by the RUNTIME layer
(`score_v2.artifact`, `score_v2.runtime`) and the DEVELOPMENT-ONLY
training modules that fit them (`score_v2.training.gam`,
`score_v2.training.boosting`, `score_v2.training.tree`).

This module lives in the runtime layer (stdlib-only, no dependency on
`score_v2.training`) so that `score_v2.artifact`/`score_v2.runtime` never
need to import anything from `score_v2.training` -- the training modules
import FROM here instead, constructing these exact dataclasses when they
fit a model. This keeps a single source of truth for how each non-linear
shape is evaluated: the same `evaluate_*` function runs whether it is
called mid-training (comparing candidates against a validation/test
split, before any artifact exists) or at shipped runtime (reading a
verified, hashed `Artifact`) -- there is no separate "training-time" and
"serving-time" implementation to drift apart.

Three shapes, one per non-linear model family:

  * `FeatureShapeFit` -- a monotonic GAM's per-feature piecewise-linear
    shape (a small number of `(x, y)` knots).
  * `Stump` -- one monotonic boosted weak learner (split one feature at
    one threshold, predict a constant on either side).
  * `TreeNode` -- a monotonic tree, recursively: either a leaf (constant
    value) or an internal split routing to a `low`/`high` child.
    `tree_depth`/`tree_node_count`/`tree_value_range`/
    `verify_tree_monotonicity` are structural helpers used both while
    fitting (complexity guardrails) and by `score_v2.artifact.Artifact.validate()`
    (an independent, bottom-up re-verification of the monotonicity
    invariant that does not trust the training code that produced the tree).

Every shape stores its OWN `robust_center`/`robust_scale` (or, for a
tree, each split node does) -- mirroring `score_v2.artifact.FeatureCoefficient`'s
pattern -- so it can be evaluated on any raw extracted feature value
(validation, test, or live runtime data) without ever refitting
normalization on that data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_v2.feature_spec import FeatureSpec, FeatureValue

# ── GAM shapes ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FeatureShapeFit:
    spec: FeatureSpec
    robust_center: float
    robust_scale: float
    knot_x: tuple[float, ...]
    knot_y: tuple[float, ...]

    def evaluate(self, normalized_value: Optional[float]) -> float:
        """`0.0` for a missing value; otherwise piecewise-linear
        interpolation between `knot_x`/`knot_y`, flat-extrapolated beyond
        the outer knots (flat extrapolation cannot violate monotonicity).
        """
        if normalized_value is None:
            return 0.0
        return _interpolate(normalized_value, self.knot_x, self.knot_y)

    def to_dict(self) -> dict:
        payload = self.spec.to_dict()
        payload.update({
            "robust_center": self.robust_center, "robust_scale": self.robust_scale,
            "knot_x": list(self.knot_x), "knot_y": list(self.knot_y),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping) -> "FeatureShapeFit":
        return cls(
            spec=FeatureSpec.from_dict(data), robust_center=float(data["robust_center"]),
            robust_scale=float(data["robust_scale"]),
            knot_x=tuple(float(x) for x in data["knot_x"]),
            knot_y=tuple(float(y) for y in data["knot_y"]),
        )


def interpolation_weights(x: float, knot_x: Sequence[float]) -> dict[int, float]:
    n = len(knot_x)
    if n == 1:
        return {0: 1.0}
    if x <= knot_x[0]:
        return {0: 1.0}
    if x >= knot_x[-1]:
        return {n - 1: 1.0}
    for index in range(n - 1):
        left, right = knot_x[index], knot_x[index + 1]
        if left <= x <= right:
            if right - left <= 1e-12:
                return {index: 1.0}
            weight_right = (x - left) / (right - left)
            return {index: 1.0 - weight_right, index + 1: weight_right}
    return {n - 1: 1.0}  # unreachable given sorted knot_x, defensive fallback


def _interpolate(x: float, knot_x: Sequence[float], knot_y: Sequence[float]) -> float:
    weights = interpolation_weights(x, knot_x)
    return sum(weight * knot_y[index] for index, weight in weights.items())


def evaluate_gam_shapes(
        shapes: Sequence[FeatureShapeFit], feature_vector: Mapping[str, FeatureValue]) -> float:
    """Sum of every feature's shape contribution. Missing -> `0.0`."""
    total = 0.0
    for shape in shapes:
        value = feature_vector.get(shape.spec.name)
        if value is None or not value.present:
            continue
        normalized = (value.transformed - shape.robust_center) / shape.robust_scale
        total += shape.evaluate(normalized)
    return total


# ── boosted stumps ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Stump:
    spec: FeatureSpec
    robust_center: float
    robust_scale: float
    threshold: float  # in normalized feature space
    low_value: float
    high_value: float

    def evaluate(self, normalized_value: Optional[float]) -> float:
        if normalized_value is None:
            return 0.0
        return self.low_value if normalized_value <= self.threshold else self.high_value

    def to_dict(self) -> dict:
        payload = self.spec.to_dict()
        payload.update({
            "robust_center": self.robust_center, "robust_scale": self.robust_scale,
            "threshold": self.threshold, "low_value": self.low_value,
            "high_value": self.high_value,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping) -> "Stump":
        return cls(
            spec=FeatureSpec.from_dict(data), robust_center=float(data["robust_center"]),
            robust_scale=float(data["robust_scale"]), threshold=float(data["threshold"]),
            low_value=float(data["low_value"]), high_value=float(data["high_value"]),
        )


def evaluate_boosted_stumps(
        stumps: Sequence[Stump], feature_vector: Mapping[str, FeatureValue]) -> float:
    """Sum of every stump's contribution. Missing -> `0.0` for that stump."""
    total = 0.0
    for stump in stumps:
        value = feature_vector.get(stump.spec.name)
        if value is None or not value.present:
            continue
        normalized = (value.transformed - stump.robust_center) / stump.robust_scale
        total += stump.evaluate(normalized)
    return total


# ── monotonic tree ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TreeNode:
    is_leaf: bool
    value: float = 0.0
    spec: Optional[FeatureSpec] = None
    robust_center: float = 0.0
    robust_scale: float = 1.0
    threshold: float = 0.0
    low: Optional["TreeNode"] = None
    high: Optional["TreeNode"] = None

    def evaluate(self, feature_vector: Mapping[str, FeatureValue]) -> float:
        node = self
        while not node.is_leaf:
            value = feature_vector.get(node.spec.name)
            if value is None or not value.present:
                return 0.0  # honest: cannot route through a missing split feature
            normalized = (value.transformed - node.robust_center) / node.robust_scale
            node = node.low if normalized <= node.threshold else node.high
        return node.value

    def to_dict(self) -> dict:
        if self.is_leaf:
            return {"is_leaf": True, "value": self.value}
        return {
            "is_leaf": False, "spec": self.spec.to_dict(),
            "robust_center": self.robust_center, "robust_scale": self.robust_scale,
            "threshold": self.threshold,
            "low": self.low.to_dict(), "high": self.high.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "TreeNode":
        if data["is_leaf"]:
            return cls(is_leaf=True, value=float(data["value"]))
        return cls(
            is_leaf=False, spec=FeatureSpec.from_dict(data["spec"]),
            robust_center=float(data["robust_center"]), robust_scale=float(data["robust_scale"]),
            threshold=float(data["threshold"]),
            low=cls.from_dict(data["low"]), high=cls.from_dict(data["high"]),
        )


def evaluate_tree(root: TreeNode, feature_vector: Mapping[str, FeatureValue]) -> float:
    return root.evaluate(feature_vector)


def tree_depth(node: TreeNode) -> int:
    if node.is_leaf:
        return 1
    return 1 + max(tree_depth(node.low), tree_depth(node.high))


def tree_node_count(node: TreeNode) -> int:
    if node.is_leaf:
        return 1
    return 1 + tree_node_count(node.low) + tree_node_count(node.high)


def tree_value_range(node: TreeNode) -> tuple[float, float]:
    """Bottom-up (min, max) of every leaf value reachable under `node`."""
    if node.is_leaf:
        return node.value, node.value
    low_min, low_max = tree_value_range(node.low)
    high_min, high_max = tree_value_range(node.high)
    return min(low_min, high_min), max(low_max, high_max)


def verify_tree_monotonicity(node: TreeNode) -> bool:
    """Structural, bottom-up re-verification of the value-range invariant
    at every internal node -- `True` iff the tree is provably monotonic.
    Independent of trusting whatever training code produced `node`.
    """
    if node.is_leaf:
        return True
    if not verify_tree_monotonicity(node.low) or not verify_tree_monotonicity(node.high):
        return False
    low_min, low_max = tree_value_range(node.low)
    high_min, high_max = tree_value_range(node.high)
    if node.spec.direction > 0:
        return low_max <= high_min
    if node.spec.direction < 0:
        return low_min >= high_max
    return True  # unconstrained feature: no ordering requirement between children


def collect_tree_feature_names(node: TreeNode) -> set[str]:
    """Every feature name used anywhere in the tree (for contract checks)."""
    if node.is_leaf:
        return set()
    return (
        {node.spec.name}
        | collect_tree_feature_names(node.low)
        | collect_tree_feature_names(node.high)
    )


def collect_tree_specs(node: TreeNode) -> dict[str, FeatureSpec]:
    """Every (name -> spec) used anywhere in the tree, for exact-contract
    validation (a tampered spec sharing a real feature's name at one node
    but not another must still be caught)."""
    if node.is_leaf:
        return {}
    specs = {node.spec.name: node.spec}
    specs.update(collect_tree_specs(node.low))
    specs.update(collect_tree_specs(node.high))
    return specs
