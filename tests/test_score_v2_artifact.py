"""Tests for score_v2/artifact.py -- the immutable hashed artifact format.

Sections:
  1. build_artifact produces a valid, self-consistent artifact.
  2. Content-hash determinism and tamper detection (hand-edited field
     changes the recomputed hash; `Artifact.load` rejects it).
  3. Monotonic-invariant validation (coefficient sign must match
     `FeatureSpec.direction`).
  4. Fallback/shrinkage metadata validation.
  5. Schema-version and production_ready/release_notes gates.
  6. Save/load round trip via a real file.
  7. Hardening: finite-value checks, strict booleans, nonempty
     coefficients, score/confidence/abstention range+order checks, exact
     tier-contract matching (rejects arbitrary/extra/tampered specs),
     and rejection of a "rehashed" (tampered-then-rehashed) artifact.
"""

import dataclasses
import datetime
import json
import math

import pytest

from score_v2.artifact import (
    ARTIFACT_SCHEMA_VERSION,
    Artifact,
    ArtifactIntegrityError,
    ArtifactValidationError,
    FeatureCoefficient,
    RoleCalibration,
    build_artifact,
)
from score_v2.feature_spec import FEATURE_ALLOWLIST, feature_contract_for_tier

FIXED_NOW = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)


def _coefficients(evidence_source="match_v5", magnitude=0.3):
    return [
        FeatureCoefficient(
            spec=spec,
            coefficient=(
                magnitude if spec.direction > 0
                else (-magnitude if spec.direction < 0 else 0.0)
            ),
            robust_center=0.0, robust_scale=1.0,
        )
        for spec in feature_contract_for_tier(evidence_source)
    ]


def _build(**overrides):
    evidence_source = overrides.get("evidence_source", "match_v5")
    default_coefficients = (
        overrides["coefficients"] if "coefficients" in overrides
        else _coefficients(evidence_source)
    )
    kwargs = dict(
        model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", evidence_source=evidence_source,
        intercept=0.0, coefficients=default_coefficients,
        role_calibration={
            "mid": RoleCalibration(offset=0.0, sample_count=0, shrinkage_weight=0.0),
        },
        score_calibration={"midpoint": 50.0, "scale": 5.0, "clip_min": 0.0, "clip_max": 100.0},
        confidence_params={
            "missing_feature_penalty": 0.5, "evidence_quality_weight": 0.5,
            "interval_min_half_width": 3.0, "interval_max_half_width": 40.0,
        },
        abstention_params={
            "short_game_seconds": 600.0, "min_present_feature_fraction": 0.3,
            "min_confidence_to_report": 0.1,
        },
        training_metadata={"n_pairs_used": 0}, evaluation_metadata=None,
        production_ready=False, release_notes="dev artifact for tests",
        now=FIXED_NOW,
    )
    kwargs.update(overrides)
    return build_artifact(**kwargs)


# ── 1. basic construction ───────────────────────────────────────────────────

def test_build_artifact_is_valid():
    artifact = _build()
    artifact.validate()
    assert artifact.schema_version == ARTIFACT_SCHEMA_VERSION
    assert artifact.content_hash


def test_build_artifact_valid_for_every_tier():
    for evidence_source in ("match_v5", "lcu_timeline", "live_client", "aggregate"):
        artifact = _build(evidence_source=evidence_source)
        artifact.validate()


# ── 2. hashing / tamper detection ───────────────────────────────────────────

def test_content_hash_is_deterministic_for_identical_inputs():
    a = _build()
    b = _build()
    assert a.content_hash == b.content_hash


def test_content_hash_changes_when_a_field_changes():
    a = _build()
    b = _build(intercept=1.0)
    assert a.content_hash != b.content_hash


def test_verify_content_hash_passes_for_untampered_artifact():
    artifact = _build()
    artifact.verify_content_hash()  # should not raise


def test_load_rejects_tampered_file(tmp_path):
    artifact = _build()
    path = tmp_path / "artifact.json"
    artifact.save(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    data["intercept"] = 999.0  # tamper without recomputing content_hash
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ArtifactIntegrityError):
        Artifact.load(path)


def test_load_rejects_truncated_missing_field(tmp_path):
    artifact = _build()
    path = tmp_path / "artifact.json"
    artifact.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["score_calibration"]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(KeyError):
        Artifact.load(path)


def test_load_rejects_rehashed_tampered_artifact(tmp_path):
    """An attacker who tampers AND recomputes content_hash to match (so
    `verify_content_hash` alone would pass) must still be rejected by
    `validate()`'s semantic checks.
    """
    artifact = _build()
    path = tmp_path / "artifact.json"
    artifact.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))

    # Tamper: break clip ordering (a semantically invalid artifact).
    data["score_calibration"]["clip_min"] = 100.0
    data["score_calibration"]["clip_max"] = 0.0
    # Rehash: recompute content_hash so verify_content_hash() alone would pass.
    tampered_for_hash = Artifact.from_dict({**data, "content_hash": ""})
    data["content_hash"] = tampered_for_hash.compute_content_hash()
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ArtifactValidationError):
        Artifact.load(path)


# ── 3. monotonic invariants ──────────────────────────────────────────────────

def test_positive_direction_with_negative_coefficient_rejected():
    spec = next(s for s in FEATURE_ALLOWLIST if s.direction > 0)
    bad_coefficient = FeatureCoefficient(
        spec=spec, coefficient=-0.1, robust_center=0.0, robust_scale=1.0,
    )
    with pytest.raises(ArtifactValidationError):
        _build(coefficients=[bad_coefficient])


def test_negative_direction_with_positive_coefficient_rejected():
    spec = next(s for s in FEATURE_ALLOWLIST if s.direction < 0)
    bad_coefficient = FeatureCoefficient(
        spec=spec, coefficient=0.1, robust_center=0.0, robust_scale=1.0,
    )
    with pytest.raises(ArtifactValidationError):
        _build(coefficients=[bad_coefficient])


def test_unconstrained_direction_allows_any_sign():
    # Tested at the FeatureCoefficient level directly -- an Artifact-level
    # build would fail the tier-contract-match check instead (no current
    # FeatureSpec has an unconstrained direction), which is a separate
    # concern from whether FeatureCoefficient.validate() itself permits
    # any sign for direction=0.
    spec = FEATURE_ALLOWLIST[0]
    unconstrained_spec = dataclasses.replace(spec, direction=0)
    coefficient = FeatureCoefficient(
        spec=unconstrained_spec, coefficient=-5.0, robust_center=0.0, robust_scale=1.0,
    )
    coefficient.validate()  # should not raise


def test_zero_robust_scale_rejected():
    spec = FEATURE_ALLOWLIST[0]
    coefficient = FeatureCoefficient(
        spec=spec, coefficient=0.0, robust_center=0.0, robust_scale=0.0,
    )
    with pytest.raises(ArtifactValidationError):
        _build(coefficients=[coefficient])


def test_duplicate_feature_names_rejected():
    spec = FEATURE_ALLOWLIST[0]
    coefficient = FeatureCoefficient(
        spec=spec, coefficient=0.0, robust_center=0.0, robust_scale=1.0,
    )
    with pytest.raises(ArtifactValidationError):
        _build(coefficients=[coefficient, coefficient])


# ── 4. fallback / shrinkage metadata ────────────────────────────────────────

def test_fallback_requires_shrinkage_source():
    with pytest.raises(ArtifactValidationError):
        _build(fallback={"is_fallback": True, "shrinkage_source": None})


def test_fallback_shrinkage_source_must_differ_from_evidence_source():
    with pytest.raises(ArtifactValidationError):
        _build(fallback={"is_fallback": True, "shrinkage_source": "match_v5"})


def test_fallback_shrinkage_source_must_be_a_known_tier():
    with pytest.raises(ArtifactValidationError):
        _build(fallback={"is_fallback": True, "shrinkage_source": "not_a_tier"})


def test_non_fallback_with_shrinkage_source_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(fallback={"is_fallback": False, "shrinkage_source": "lcu_timeline"})


def test_valid_fallback_metadata_accepted():
    artifact = _build(
        evidence_source="live_client",
        fallback={"is_fallback": True, "shrinkage_source": "lcu_timeline"},
    )
    artifact.validate()
    assert artifact.fallback["is_fallback"] is True
    assert artifact.fallback["shrinkage_source"] == "lcu_timeline"


def test_default_fallback_is_not_a_fallback():
    artifact = _build()
    assert artifact.fallback["is_fallback"] is False
    assert artifact.fallback["shrinkage_source"] is None


def test_fallback_is_fallback_must_be_strict_bool():
    with pytest.raises(ArtifactValidationError):
        _build(fallback={"is_fallback": "yes", "shrinkage_source": None})


# ── 5. schema version / production_ready gates ──────────────────────────────

def test_unsupported_schema_version_rejected(tmp_path):
    artifact = _build()
    path = tmp_path / "artifact.json"
    artifact.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = ARTIFACT_SCHEMA_VERSION + 1
    # content_hash must be recomputed for the hash check to even reach
    # schema validation -- use the class directly to isolate the check.
    tampered = Artifact.from_dict(data)
    with pytest.raises(ArtifactValidationError):
        tampered.validate()


def test_unknown_evidence_source_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(evidence_source="not_a_real_tier", coefficients=_coefficients("match_v5"))


def test_production_ready_requires_release_notes():
    with pytest.raises(ArtifactValidationError):
        _build(production_ready=True, release_notes="")


def test_production_ready_with_release_notes_is_allowed_structurally():
    # Structural validation only -- this test does not claim the artifact
    # is actually production-ready, only that the schema allows the flag
    # when a caller documents why (no caller in this pipeline does this).
    artifact = _build(production_ready=True, release_notes="documented reason")
    artifact.validate()


def test_production_ready_must_be_strict_bool():
    with pytest.raises(ArtifactValidationError):
        _build(production_ready="true", release_notes="x")


def test_unknown_role_key_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(role_calibration={
            "not_a_role": RoleCalibration(offset=0.0, sample_count=0, shrinkage_weight=0.0),
        })


# ── 6. save/load round trip ─────────────────────────────────────────────────

def test_save_and_load_round_trip(tmp_path):
    artifact = _build()
    path = tmp_path / "artifact.json"
    artifact.save(path)
    loaded = Artifact.load(path)
    assert loaded.content_hash == artifact.content_hash
    assert loaded.evidence_source == artifact.evidence_source
    assert len(loaded.coefficients) == len(artifact.coefficients)
    assert loaded.to_dict() == artifact.to_dict()


# ── 7. hardening ─────────────────────────────────────────────────────────────

def test_nan_intercept_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(intercept=float("nan"))


def test_infinite_coefficient_rejected():
    spec = feature_contract_for_tier("match_v5")[0]
    bad = FeatureCoefficient(
        spec=spec,
        coefficient=(float("inf") if spec.direction >= 0 else float("-inf")),
        robust_center=0.0, robust_scale=1.0,
    )
    coeffs = [c for c in _coefficients("match_v5") if c.spec.name != spec.name] + [bad]
    with pytest.raises(ArtifactValidationError):
        _build(coefficients=coeffs)


def test_nonempty_coefficients_required():
    with pytest.raises(ArtifactValidationError):
        _build(coefficients=[])


def test_score_calibration_scale_must_be_positive():
    with pytest.raises(ArtifactValidationError):
        _build(score_calibration={
            "midpoint": 50.0, "scale": 0.0, "clip_min": 0.0, "clip_max": 100.0,
        })


def test_score_calibration_negative_scale_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(score_calibration={
            "midpoint": 50.0, "scale": -5.0, "clip_min": 0.0, "clip_max": 100.0,
        })


def test_score_calibration_clip_order_enforced():
    with pytest.raises(ArtifactValidationError):
        _build(score_calibration={
            "midpoint": 50.0, "scale": 5.0, "clip_min": 100.0, "clip_max": 0.0,
        })


def test_score_calibration_midpoint_must_be_within_clip_range():
    with pytest.raises(ArtifactValidationError):
        _build(score_calibration={
            "midpoint": 150.0, "scale": 5.0, "clip_min": 0.0, "clip_max": 100.0,
        })


def test_score_calibration_rejects_non_finite_values():
    with pytest.raises(ArtifactValidationError):
        _build(score_calibration={
            "midpoint": 50.0, "scale": float("inf"), "clip_min": 0.0, "clip_max": 100.0,
        })


def test_confidence_params_missing_key_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(confidence_params={"missing_feature_penalty": 0.5})


def test_confidence_params_penalty_out_of_range_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(confidence_params={
            "missing_feature_penalty": 1.5, "evidence_quality_weight": 0.5,
            "interval_min_half_width": 3.0, "interval_max_half_width": 40.0,
        })


def test_confidence_params_interval_order_enforced():
    with pytest.raises(ArtifactValidationError):
        _build(confidence_params={
            "missing_feature_penalty": 0.5, "evidence_quality_weight": 0.5,
            "interval_min_half_width": 40.0, "interval_max_half_width": 3.0,
        })


def test_abstention_params_missing_key_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(abstention_params={"short_game_seconds": 600.0})


def test_abstention_params_fraction_out_of_range_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(abstention_params={
            "short_game_seconds": 600.0, "min_present_feature_fraction": 1.5,
            "min_confidence_to_report": 0.1,
        })


def test_abstention_params_negative_short_game_seconds_rejected():
    with pytest.raises(ArtifactValidationError):
        _build(abstention_params={
            "short_game_seconds": -1.0, "min_present_feature_fraction": 0.3,
            "min_confidence_to_report": 0.1,
        })


def test_coefficients_must_exactly_match_tier_contract_extra_feature():
    # aggregate's contract is only raw_kills/raw_deaths/raw_assists --
    # passing match_v5's full contract under evidence_source="aggregate"
    # must be rejected (extra features not in aggregate's contract).
    with pytest.raises(ArtifactValidationError):
        _build(evidence_source="aggregate", coefficients=_coefficients("match_v5"))


def test_coefficients_must_exactly_match_tier_contract_missing_feature():
    coeffs = _coefficients("match_v5")[:-1]  # drop one required feature
    with pytest.raises(ArtifactValidationError):
        _build(evidence_source="match_v5", coefficients=coeffs)


def test_coefficients_reject_tampered_spec_with_matching_name():
    # Same name, but a different path than the canonical contract --
    # exact spec equality must still catch this even though "the name
    # looks right".
    coeffs = _coefficients("match_v5")
    target = coeffs[0]
    tampered_spec = dataclasses.replace(target.spec, path=("raw", "kills"))
    tampered_coefficient = FeatureCoefficient(
        spec=tampered_spec, coefficient=target.coefficient,
        robust_center=target.robust_center, robust_scale=target.robust_scale,
    )
    coeffs = [tampered_coefficient] + list(coeffs[1:])
    with pytest.raises(ArtifactValidationError):
        _build(evidence_source="match_v5", coefficients=coeffs)
