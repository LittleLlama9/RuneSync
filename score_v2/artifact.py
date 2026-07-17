"""Immutable, hashed DAEMON Score v2 artifact format.

An `Artifact` is the single object the runtime scorer (`score_v2.runtime`)
consumes. It is produced only by `score_v2.training.export` and is treated
as **immutable once built**: `content_hash` is a SHA-256 digest over every
other field (canonical JSON: sorted keys, no whitespace), so any edit --
accidental or malicious -- to a saved artifact file is detected the moment
it is loaded, before a single feature is scored against it.

Every one of the four DAEMON Score v2 evidence tiers (`match_v5`,
`lcu_timeline`, `live_client`, `aggregate` -- see `score_features.py`) gets
its own artifact. `fallback` is the only sanctioned way one tier's
coefficients may stand in for another: when
`fallback["is_fallback"] is True`, `fallback["shrinkage_source"]` names the
tier this artifact's coefficients were actually fit/shrunk from, and
`score_v2.runtime.select_artifact` uses that field explicitly rather than
ever silently substituting a different tier's artifact for one that is
missing.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from score_features import SOURCE_PRIORITY
from score_v2.feature_spec import (
    DIRECTION_NEGATIVE,
    DIRECTION_POSITIVE,
    FeatureSpec,
    TIER_FEATURE_CONTRACTS,
)
from score_v2.model_shapes import (
    FeatureShapeFit,
    Stump,
    TreeNode,
    verify_tree_monotonicity,
)

ARTIFACT_SCHEMA_VERSION = 1

ROLE_NAMES = ("top", "jungle", "mid", "bot", "support", "unknown")

MODEL_FAMILY_LINEAR = "linear"
MODEL_FAMILY_GAM = "gam"
MODEL_FAMILY_BOOSTED_STUMPS = "boosted_stumps"
MODEL_FAMILY_MONOTONIC_TREE = "monotonic_tree"
MODEL_FAMILIES = (
    MODEL_FAMILY_LINEAR, MODEL_FAMILY_GAM, MODEL_FAMILY_BOOSTED_STUMPS,
    MODEL_FAMILY_MONOTONIC_TREE,
)

REQUIRED_SCORE_CALIBRATION_KEYS = ("midpoint", "scale", "clip_min", "clip_max")
REQUIRED_CONFIDENCE_PARAM_KEYS = (
    "missing_feature_penalty", "evidence_quality_weight",
    "interval_min_half_width", "interval_max_half_width",
)
REQUIRED_ABSTENTION_PARAM_KEYS = (
    "short_game_seconds", "min_present_feature_fraction", "min_confidence_to_report",
)


class ArtifactValidationError(Exception):
    """Raised when an artifact's fields are internally inconsistent."""


class ArtifactIntegrityError(Exception):
    """Raised when a loaded artifact's content hash does not verify."""


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def sha256_hex(data) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _require_finite(name: str, value) -> float:
    """Coerce to float and reject NaN/Infinity -- Python's `json` module
    happily parses the non-standard `NaN`/`Infinity`/`-Infinity` tokens,
    so a hand-edited or "rehashed" (tampered, then hash recomputed to
    match) artifact could otherwise carry a non-finite number straight
    past a naive type check.
    """
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(f"{name} must be a finite number, got {value!r}") from exc
    if not math.isfinite(numeric):
        raise ArtifactValidationError(f"{name} must be finite, got {value!r}")
    return numeric


def _require_strict_bool(name: str, value) -> bool:
    if not isinstance(value, bool):
        raise ArtifactValidationError(f"{name} must be a strict boolean, got {value!r}")
    return value


def _require_canonical_spec_match(
        feature_name: str, spec: FeatureSpec,
        canonical_by_name: Mapping[str, FeatureSpec], evidence_source: str) -> None:
    """A non-linear model family's shape may legitimately use only a
    SUBSET of a tier's canonical feature contract (unlike the linear
    family, which must cover it exactly) -- but every feature it DOES use
    must be an exact, untampered match against that tier's canonical
    `FeatureSpec` (no arbitrary raw path, no smuggled-in direction/
    transform/capability/group edit), even if the artifact's
    `content_hash` was recomputed to match a hand-edited payload.
    """
    canonical_spec = canonical_by_name.get(feature_name)
    if canonical_spec is None:
        raise ArtifactValidationError(
            f"{feature_name}: not part of tier {evidence_source!r}'s canonical "
            "feature contract"
        )
    if spec != canonical_spec:
        raise ArtifactValidationError(
            f"{feature_name}: spec does not exactly match the canonical contract "
            f"for tier {evidence_source!r} (path/direction/transform/capability/"
            "group must match exactly)"
        )


@dataclass(frozen=True)
class FeatureCoefficient:
    """One trained coefficient bound to its (immutable) `FeatureSpec`."""

    spec: FeatureSpec
    coefficient: float
    robust_center: float
    robust_scale: float

    def to_dict(self) -> dict:
        payload = self.spec.to_dict()
        payload.update({
            "coefficient": self.coefficient,
            "robust_center": self.robust_center,
            "robust_scale": self.robust_scale,
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping) -> "FeatureCoefficient":
        return cls(
            spec=FeatureSpec.from_dict(data),
            coefficient=float(data["coefficient"]),
            robust_center=float(data["robust_center"]),
            robust_scale=float(data["robust_scale"]),
        )

    def validate(self) -> None:
        _require_finite(f"{self.spec.name}.coefficient", self.coefficient)
        _require_finite(f"{self.spec.name}.robust_center", self.robust_center)
        _require_finite(f"{self.spec.name}.robust_scale", self.robust_scale)
        if self.spec.direction == DIRECTION_POSITIVE and self.coefficient < 0:
            raise ArtifactValidationError(
                f"{self.spec.name}: coefficient {self.coefficient} is negative "
                "but direction requires >= 0"
            )
        if self.spec.direction == DIRECTION_NEGATIVE and self.coefficient > 0:
            raise ArtifactValidationError(
                f"{self.spec.name}: coefficient {self.coefficient} is positive "
                "but direction requires <= 0"
            )
        if self.robust_scale <= 0:
            raise ArtifactValidationError(
                f"{self.spec.name}: robust_scale must be > 0, got {self.robust_scale}"
            )


@dataclass(frozen=True)
class RoleCalibration:
    offset: float
    sample_count: int
    shrinkage_weight: float

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "sample_count": self.sample_count,
            "shrinkage_weight": self.shrinkage_weight,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> "RoleCalibration":
        return cls(
            offset=float(data["offset"]),
            sample_count=int(data["sample_count"]),
            shrinkage_weight=float(data["shrinkage_weight"]),
        )

    def validate(self, role: str) -> None:
        _require_finite(f"role_calibration.{role}.offset", self.offset)
        _require_finite(f"role_calibration.{role}.shrinkage_weight", self.shrinkage_weight)
        if self.sample_count < 0:
            raise ArtifactValidationError(
                f"role_calibration.{role}.sample_count must be >= 0, got "
                f"{self.sample_count}"
            )
        if not (0.0 <= self.shrinkage_weight <= 1.0):
            raise ArtifactValidationError(
                f"role_calibration.{role}.shrinkage_weight must be in [0, 1], got "
                f"{self.shrinkage_weight}"
            )


@dataclass(frozen=True)
class Artifact:
    schema_version: int
    model_version: str
    feature_version: str
    calibration_version: str
    evidence_source: str
    fallback: Mapping
    intercept: float
    coefficients: tuple[FeatureCoefficient, ...]
    role_calibration: Mapping[str, RoleCalibration]
    score_calibration: Mapping
    confidence_params: Mapping
    abstention_params: Mapping
    training_metadata: Mapping
    evaluation_metadata: Optional[Mapping]
    production_ready: bool
    release_notes: str
    created_at: str
    model_family: str = MODEL_FAMILY_LINEAR
    gam_shapes: Optional[tuple[FeatureShapeFit, ...]] = None
    boosted_stumps: Optional[tuple[Stump, ...]] = None
    monotonic_tree: Optional[TreeNode] = None
    content_hash: str = ""

    # ── (de)serialization ───────────────────────────────────────────────

    def _hashable_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "model_version": self.model_version,
            "feature_version": self.feature_version,
            "calibration_version": self.calibration_version,
            "evidence_source": self.evidence_source,
            "fallback": dict(self.fallback),
            "model_family": self.model_family,
            "intercept": self.intercept,
            "coefficients": [c.to_dict() for c in self.coefficients],
            "gam_shapes": (
                [shape.to_dict() for shape in self.gam_shapes]
                if self.gam_shapes is not None else None
            ),
            "boosted_stumps": (
                [stump.to_dict() for stump in self.boosted_stumps]
                if self.boosted_stumps is not None else None
            ),
            "monotonic_tree": (
                self.monotonic_tree.to_dict() if self.monotonic_tree is not None else None
            ),
            "role_calibration": {
                role: cal.to_dict() for role, cal in self.role_calibration.items()
            },
            "score_calibration": dict(self.score_calibration),
            "confidence_params": dict(self.confidence_params),
            "abstention_params": dict(self.abstention_params),
            "training_metadata": dict(self.training_metadata),
            "evaluation_metadata": (
                dict(self.evaluation_metadata)
                if self.evaluation_metadata is not None else None
            ),
            "production_ready": self.production_ready,
            "release_notes": self.release_notes,
            "created_at": self.created_at,
        }

    def compute_content_hash(self) -> str:
        return sha256_hex(canonical_json(self._hashable_dict()))

    def to_dict(self) -> dict:
        payload = self._hashable_dict()
        payload["content_hash"] = self.content_hash or self.compute_content_hash()
        return payload

    @classmethod
    def from_dict(cls, data: Mapping) -> "Artifact":
        return cls(
            schema_version=int(data["schema_version"]),
            model_version=data["model_version"],
            feature_version=data["feature_version"],
            calibration_version=data["calibration_version"],
            evidence_source=data["evidence_source"],
            fallback=dict(data["fallback"]),
            intercept=float(data["intercept"]),
            coefficients=tuple(
                FeatureCoefficient.from_dict(c) for c in data["coefficients"]
            ),
            role_calibration={
                role: RoleCalibration.from_dict(cal)
                for role, cal in data["role_calibration"].items()
            },
            score_calibration=dict(data["score_calibration"]),
            confidence_params=dict(data["confidence_params"]),
            abstention_params=dict(data["abstention_params"]),
            training_metadata=dict(data["training_metadata"]),
            evaluation_metadata=(
                dict(data["evaluation_metadata"])
                if data.get("evaluation_metadata") is not None else None
            ),
            production_ready=data["production_ready"],
            release_notes=data.get("release_notes", ""),
            created_at=data["created_at"],
            model_family=data.get("model_family", MODEL_FAMILY_LINEAR),
            gam_shapes=(
                tuple(FeatureShapeFit.from_dict(s) for s in data["gam_shapes"])
                if data.get("gam_shapes") is not None else None
            ),
            boosted_stumps=(
                tuple(Stump.from_dict(s) for s in data["boosted_stumps"])
                if data.get("boosted_stumps") is not None else None
            ),
            monotonic_tree=(
                TreeNode.from_dict(data["monotonic_tree"])
                if data.get("monotonic_tree") is not None else None
            ),
            content_hash=data.get("content_hash", ""),
        )

    # ── validation ───────────────────────────────────────────────────────

    def validate(self) -> None:
        if self.schema_version != ARTIFACT_SCHEMA_VERSION:
            raise ArtifactValidationError(
                f"Unsupported artifact schema_version {self.schema_version}; "
                f"this loader supports {ARTIFACT_SCHEMA_VERSION}"
            )
        if self.evidence_source not in SOURCE_PRIORITY:
            raise ArtifactValidationError(
                f"Unknown evidence_source {self.evidence_source!r}"
            )
        _require_strict_bool("production_ready", self.production_ready)

        is_fallback_raw = self.fallback.get("is_fallback", False)
        is_fallback = _require_strict_bool("fallback.is_fallback", is_fallback_raw)
        shrinkage_source = self.fallback.get("shrinkage_source")
        if is_fallback:
            if shrinkage_source not in SOURCE_PRIORITY:
                raise ArtifactValidationError(
                    "fallback.is_fallback=True requires a valid "
                    "fallback.shrinkage_source"
                )
            if shrinkage_source == self.evidence_source:
                raise ArtifactValidationError(
                    "fallback.shrinkage_source must differ from evidence_source"
                )
        elif shrinkage_source is not None:
            raise ArtifactValidationError(
                "fallback.shrinkage_source must be null when is_fallback is False"
            )

        _require_finite("intercept", self.intercept)

        if self.model_family not in MODEL_FAMILIES:
            raise ArtifactValidationError(f"Unknown model_family {self.model_family!r}")

        # Canonical, tier-specific feature contract -- every family checks
        # its own features against this; no arbitrary raw paths, no
        # tampered direction/transform/capability, even if the artifact's
        # content_hash was recomputed to match (a "rehashed" tamper). See
        # score_v2.feature_spec.TIER_FEATURE_CONTRACTS.
        canonical_specs = TIER_FEATURE_CONTRACTS.get(self.evidence_source)
        if canonical_specs is None:
            raise ArtifactValidationError(
                f"No canonical feature contract for evidence_source "
                f"{self.evidence_source!r}"
            )
        canonical_by_name = {spec.name: spec for spec in canonical_specs}

        if self.model_family == MODEL_FAMILY_LINEAR:
            self._validate_linear_coefficients(canonical_by_name)
        else:
            if self.coefficients:
                raise ArtifactValidationError(
                    f"a {self.model_family!r} artifact must not carry linear "
                    "coefficients (they are unused and would be dead weight)"
                )
            if self.intercept != 0.0:
                raise ArtifactValidationError(
                    f"a {self.model_family!r} artifact's intercept must be 0.0 "
                    "-- there is no separate linear term for a non-linear shape"
                )
            if self.model_family == MODEL_FAMILY_GAM:
                self._validate_gam_shapes(canonical_by_name)
            elif self.model_family == MODEL_FAMILY_BOOSTED_STUMPS:
                self._validate_boosted_stumps(canonical_by_name)
            elif self.model_family == MODEL_FAMILY_MONOTONIC_TREE:
                self._validate_monotonic_tree(canonical_by_name)

        for role, calibration in self.role_calibration.items():
            if role not in ROLE_NAMES:
                raise ArtifactValidationError(f"Unknown role_calibration key {role!r}")
            calibration.validate(role)

        missing_score_keys = set(REQUIRED_SCORE_CALIBRATION_KEYS) - set(self.score_calibration)
        if missing_score_keys:
            raise ArtifactValidationError(
                f"score_calibration missing required keys {sorted(missing_score_keys)}"
            )
        midpoint = _require_finite("score_calibration.midpoint", self.score_calibration["midpoint"])
        scale = _require_finite("score_calibration.scale", self.score_calibration["scale"])
        clip_min = _require_finite("score_calibration.clip_min", self.score_calibration["clip_min"])
        clip_max = _require_finite("score_calibration.clip_max", self.score_calibration["clip_max"])
        if scale <= 0:
            raise ArtifactValidationError(
                f"score_calibration.scale must be > 0, got {scale}"
            )
        if not (clip_min < clip_max):
            raise ArtifactValidationError(
                f"score_calibration.clip_min ({clip_min}) must be < clip_max ({clip_max})"
            )
        if not (clip_min <= midpoint <= clip_max):
            raise ArtifactValidationError(
                f"score_calibration.midpoint ({midpoint}) must be within "
                f"[clip_min, clip_max] = [{clip_min}, {clip_max}]"
            )

        missing_confidence_keys = (
            set(REQUIRED_CONFIDENCE_PARAM_KEYS) - set(self.confidence_params)
        )
        if missing_confidence_keys:
            raise ArtifactValidationError(
                f"confidence_params missing required keys "
                f"{sorted(missing_confidence_keys)}"
            )
        missing_penalty = _require_finite(
            "confidence_params.missing_feature_penalty",
            self.confidence_params["missing_feature_penalty"],
        )
        quality_weight = _require_finite(
            "confidence_params.evidence_quality_weight",
            self.confidence_params["evidence_quality_weight"],
        )
        interval_min = _require_finite(
            "confidence_params.interval_min_half_width",
            self.confidence_params["interval_min_half_width"],
        )
        interval_max = _require_finite(
            "confidence_params.interval_max_half_width",
            self.confidence_params["interval_max_half_width"],
        )
        if not (0.0 <= missing_penalty <= 1.0):
            raise ArtifactValidationError(
                f"confidence_params.missing_feature_penalty must be in [0, 1], "
                f"got {missing_penalty}"
            )
        if not (0.0 <= quality_weight <= 1.0):
            raise ArtifactValidationError(
                f"confidence_params.evidence_quality_weight must be in [0, 1], "
                f"got {quality_weight}"
            )
        if interval_min < 0:
            raise ArtifactValidationError(
                f"confidence_params.interval_min_half_width must be >= 0, got "
                f"{interval_min}"
            )
        if interval_max < interval_min:
            raise ArtifactValidationError(
                "confidence_params.interval_max_half_width must be >= "
                "interval_min_half_width"
            )

        missing_abstention_keys = (
            set(REQUIRED_ABSTENTION_PARAM_KEYS) - set(self.abstention_params)
        )
        if missing_abstention_keys:
            raise ArtifactValidationError(
                f"abstention_params missing required keys "
                f"{sorted(missing_abstention_keys)}"
            )
        short_game_seconds = _require_finite(
            "abstention_params.short_game_seconds",
            self.abstention_params["short_game_seconds"],
        )
        min_present_fraction = _require_finite(
            "abstention_params.min_present_feature_fraction",
            self.abstention_params["min_present_feature_fraction"],
        )
        min_confidence_to_report = _require_finite(
            "abstention_params.min_confidence_to_report",
            self.abstention_params["min_confidence_to_report"],
        )
        if short_game_seconds < 0:
            raise ArtifactValidationError(
                f"abstention_params.short_game_seconds must be >= 0, got "
                f"{short_game_seconds}"
            )
        if not (0.0 <= min_present_fraction <= 1.0):
            raise ArtifactValidationError(
                f"abstention_params.min_present_feature_fraction must be in "
                f"[0, 1], got {min_present_fraction}"
            )
        if not (0.0 <= min_confidence_to_report <= 1.0):
            raise ArtifactValidationError(
                f"abstention_params.min_confidence_to_report must be in [0, 1], "
                f"got {min_confidence_to_report}"
            )

        if self.production_ready and not self.release_notes:
            raise ArtifactValidationError(
                "an artifact claiming production_ready=True must document why "
                "in release_notes"
            )

    def _validate_linear_coefficients(
            self, canonical_by_name: Mapping[str, FeatureSpec]) -> None:
        """The linear family must cover its tier's canonical feature
        contract EXACTLY (every canonical feature present, no extras) --
        unlike the non-linear families below, which may legitimately use
        only a subset.
        """
        if not self.coefficients:
            raise ArtifactValidationError(
                "a linear artifact must have at least one coefficient"
            )
        names = [c.spec.name for c in self.coefficients]
        if len(names) != len(set(names)):
            raise ArtifactValidationError("Duplicate feature names in coefficients")
        for coefficient in self.coefficients:
            coefficient.validate()

        coefficient_names = set(names)
        if coefficient_names != set(canonical_by_name):
            missing_features = sorted(set(canonical_by_name) - coefficient_names)
            extra_features = sorted(coefficient_names - set(canonical_by_name))
            raise ArtifactValidationError(
                f"coefficients for tier {self.evidence_source!r} do not match its "
                f"canonical feature contract (missing={missing_features}, "
                f"extra={extra_features})"
            )
        for coefficient in self.coefficients:
            _require_canonical_spec_match(
                coefficient.spec.name, coefficient.spec, canonical_by_name,
                self.evidence_source,
            )

    def _validate_gam_shapes(self, canonical_by_name: Mapping[str, FeatureSpec]) -> None:
        if not self.gam_shapes:
            raise ArtifactValidationError(
                "a gam artifact must have at least one feature shape"
            )
        names = [shape.spec.name for shape in self.gam_shapes]
        if len(names) != len(set(names)):
            raise ArtifactValidationError("Duplicate feature names in gam_shapes")
        for shape in self.gam_shapes:
            _require_canonical_spec_match(
                shape.spec.name, shape.spec, canonical_by_name, self.evidence_source,
            )
            _require_finite(f"{shape.spec.name}.robust_center", shape.robust_center)
            scale = _require_finite(f"{shape.spec.name}.robust_scale", shape.robust_scale)
            if scale <= 0:
                raise ArtifactValidationError(
                    f"{shape.spec.name}: robust_scale must be > 0, got {scale}"
                )
            if not shape.knot_x or len(shape.knot_x) != len(shape.knot_y):
                raise ArtifactValidationError(
                    f"{shape.spec.name}: knot_x/knot_y must be non-empty and of "
                    "equal length"
                )
            for index, x in enumerate(shape.knot_x):
                _require_finite(f"{shape.spec.name}.knot_x[{index}]", x)
            for index, y in enumerate(shape.knot_y):
                _require_finite(f"{shape.spec.name}.knot_y[{index}]", y)
            for index in range(len(shape.knot_x) - 1):
                if not (shape.knot_x[index] < shape.knot_x[index + 1]):
                    raise ArtifactValidationError(
                        f"{shape.spec.name}: knot_x must be strictly increasing"
                    )
            if shape.spec.direction == DIRECTION_POSITIVE:
                for index in range(len(shape.knot_y) - 1):
                    if shape.knot_y[index] > shape.knot_y[index + 1] + 1e-9:
                        raise ArtifactValidationError(
                            f"{shape.spec.name}: knot_y must be non-decreasing "
                            "for a positive-direction feature"
                        )
            elif shape.spec.direction == DIRECTION_NEGATIVE:
                for index in range(len(shape.knot_y) - 1):
                    if shape.knot_y[index] < shape.knot_y[index + 1] - 1e-9:
                        raise ArtifactValidationError(
                            f"{shape.spec.name}: knot_y must be non-increasing "
                            "for a negative-direction feature"
                        )

    def _validate_boosted_stumps(
            self, canonical_by_name: Mapping[str, FeatureSpec]) -> None:
        if not self.boosted_stumps:
            raise ArtifactValidationError(
                "a boosted_stumps artifact must have at least one stump"
            )
        for stump in self.boosted_stumps:
            _require_canonical_spec_match(
                stump.spec.name, stump.spec, canonical_by_name, self.evidence_source,
            )
            _require_finite(f"{stump.spec.name}.robust_center", stump.robust_center)
            scale = _require_finite(f"{stump.spec.name}.robust_scale", stump.robust_scale)
            if scale <= 0:
                raise ArtifactValidationError(
                    f"{stump.spec.name}: robust_scale must be > 0, got {scale}"
                )
            _require_finite(f"{stump.spec.name}.threshold", stump.threshold)
            low = _require_finite(f"{stump.spec.name}.low_value", stump.low_value)
            high = _require_finite(f"{stump.spec.name}.high_value", stump.high_value)
            if stump.spec.direction == DIRECTION_POSITIVE and low > high + 1e-9:
                raise ArtifactValidationError(
                    f"{stump.spec.name}: low_value must be <= high_value for a "
                    "positive-direction feature"
                )
            if stump.spec.direction == DIRECTION_NEGATIVE and low < high - 1e-9:
                raise ArtifactValidationError(
                    f"{stump.spec.name}: low_value must be >= high_value for a "
                    "negative-direction feature"
                )

    def _validate_monotonic_tree(
            self, canonical_by_name: Mapping[str, FeatureSpec]) -> None:
        if self.monotonic_tree is None:
            raise ArtifactValidationError(
                "a monotonic_tree artifact must have a tree"
            )
        self._validate_tree_node(self.monotonic_tree, canonical_by_name)
        # Independent, bottom-up structural re-verification of the
        # monotonicity invariant -- does not trust whatever training code
        # produced this tree; catches a hand-edited/tampered-then-rehashed
        # tree whose individual nodes look well-formed in isolation but
        # whose child intervals actually cross.
        if not verify_tree_monotonicity(self.monotonic_tree):
            raise ArtifactValidationError(
                "monotonic_tree failed independent structural monotonicity "
                "re-verification"
            )

    def _validate_tree_node(
            self, node: TreeNode, canonical_by_name: Mapping[str, FeatureSpec]) -> None:
        if node.is_leaf:
            _require_finite("monotonic_tree leaf value", node.value)
            return
        _require_canonical_spec_match(
            node.spec.name, node.spec, canonical_by_name, self.evidence_source,
        )
        _require_finite(f"monotonic_tree.{node.spec.name}.robust_center", node.robust_center)
        scale = _require_finite(
            f"monotonic_tree.{node.spec.name}.robust_scale", node.robust_scale,
        )
        if scale <= 0:
            raise ArtifactValidationError(
                f"monotonic_tree.{node.spec.name}: robust_scale must be > 0, got {scale}"
            )
        _require_finite(f"monotonic_tree.{node.spec.name}.threshold", node.threshold)
        if node.low is None or node.high is None:
            raise ArtifactValidationError(
                "an internal monotonic_tree node must have both low and high children"
            )
        self._validate_tree_node(node.low, canonical_by_name)
        self._validate_tree_node(node.high, canonical_by_name)

    # ── hashing / persistence ────────────────────────────────────────────

    def with_content_hash(self) -> "Artifact":
        """Return a copy with `content_hash` set to the freshly computed digest."""
        payload = self.to_dict()
        payload["content_hash"] = self.compute_content_hash()
        return Artifact.from_dict(payload)

    def verify_content_hash(self) -> None:
        expected = self.compute_content_hash()
        if self.content_hash != expected:
            raise ArtifactIntegrityError(
                f"content_hash mismatch: stored={self.content_hash!r} "
                f"recomputed={expected!r}; artifact file may be corrupted or "
                "hand-edited"
            )

    def save(self, path) -> None:
        self.validate()
        finalized = self.with_content_hash()
        finalized.validate()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(finalized.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")

    @classmethod
    def load(cls, path) -> "Artifact":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        artifact = cls.from_dict(data)
        artifact.verify_content_hash()
        artifact.validate()
        return artifact


def build_artifact(
        *, model_version: str, feature_version: str, calibration_version: str,
        evidence_source: str, intercept: float,
        coefficients: Sequence[FeatureCoefficient],
        role_calibration: Mapping[str, RoleCalibration],
        score_calibration: Mapping, confidence_params: Mapping,
        abstention_params: Mapping, training_metadata: Mapping,
        evaluation_metadata: Optional[Mapping] = None,
        production_ready: bool = False, release_notes: str = "",
        fallback: Optional[Mapping] = None,
        model_family: str = MODEL_FAMILY_LINEAR,
        gam_shapes: Optional[Sequence[FeatureShapeFit]] = None,
        boosted_stumps: Optional[Sequence[Stump]] = None,
        monotonic_tree: Optional[TreeNode] = None,
        now: Optional[datetime.datetime] = None) -> Artifact:
    """Construct and validate one immutable `Artifact`.

    `production_ready` defaults to `False`: nothing in this pipeline may
    mark an artifact production-ready except an explicit, documented
    caller (there is none yet -- see `docs/SCORE_V2_MODEL_CARD_TEMPLATE.md`
    "Release gates").

    `model_family` defaults to the linear baseline (`coefficients`
    required, `gam_shapes`/`boosted_stumps`/`monotonic_tree` all `None`).
    For a non-linear family, pass `coefficients=()`, `intercept=0.0`, and
    exactly one of `gam_shapes`/`boosted_stumps`/`monotonic_tree` -- see
    `score_v2.training.compare` for how each candidate family is built and
    `Artifact.validate` for the exact per-family contract enforced.
    """
    timestamp = (now or datetime.datetime.now(datetime.timezone.utc)).isoformat()
    artifact = Artifact(
        schema_version=ARTIFACT_SCHEMA_VERSION,
        model_version=model_version,
        feature_version=feature_version,
        calibration_version=calibration_version,
        evidence_source=evidence_source,
        fallback=dict(fallback or {"is_fallback": False, "shrinkage_source": None}),
        intercept=float(intercept),
        coefficients=tuple(coefficients),
        role_calibration=dict(role_calibration),
        score_calibration=dict(score_calibration),
        confidence_params=dict(confidence_params),
        abstention_params=dict(abstention_params),
        training_metadata=dict(training_metadata),
        evaluation_metadata=evaluation_metadata,
        production_ready=production_ready,
        release_notes=release_notes,
        created_at=timestamp,
        model_family=model_family,
        gam_shapes=(tuple(gam_shapes) if gam_shapes is not None else None),
        boosted_stumps=(tuple(boosted_stumps) if boosted_stumps is not None else None),
        monotonic_tree=monotonic_tree,
    )
    artifact.validate()
    return artifact.with_content_hash()
