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

ARTIFACT_SCHEMA_VERSION = 1

ROLE_NAMES = ("top", "jungle", "mid", "bot", "support", "unknown")

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
            "intercept": self.intercept,
            "coefficients": [c.to_dict() for c in self.coefficients],
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

        if not self.coefficients:
            raise ArtifactValidationError(
                "an artifact must have at least one coefficient"
            )
        names = [c.spec.name for c in self.coefficients]
        if len(names) != len(set(names)):
            raise ArtifactValidationError("Duplicate feature names in coefficients")
        for coefficient in self.coefficients:
            coefficient.validate()

        # Coefficient specs must EXACTLY match the canonical, tier-specific
        # feature contract -- no arbitrary raw paths, no extra/missing
        # features, no tampered direction/transform/capability, even if
        # the artifact's content_hash was recomputed to match (a
        # "rehashed" tamper). See score_v2.feature_spec.TIER_FEATURE_CONTRACTS.
        canonical_specs = TIER_FEATURE_CONTRACTS.get(self.evidence_source)
        if canonical_specs is None:
            raise ArtifactValidationError(
                f"No canonical feature contract for evidence_source "
                f"{self.evidence_source!r}"
            )
        canonical_by_name = {spec.name: spec for spec in canonical_specs}
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
            canonical_spec = canonical_by_name[coefficient.spec.name]
            if coefficient.spec != canonical_spec:
                raise ArtifactValidationError(
                    f"{coefficient.spec.name}: spec does not exactly match the "
                    f"canonical contract for tier {self.evidence_source!r} (path/"
                    "direction/transform/capability/group must match exactly)"
                )

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
        now: Optional[datetime.datetime] = None) -> Artifact:
    """Construct and validate one immutable `Artifact`.

    `production_ready` defaults to `False`: nothing in this pipeline may
    mark an artifact production-ready except an explicit, documented
    caller (there is none yet -- see `docs/SCORE_V2_MODEL_CARD_TEMPLATE.md`
    "Release gates").
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
    )
    artifact.validate()
    return artifact.with_content_hash()
