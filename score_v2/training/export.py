"""Ties `dataset` + `baseline` + `calibration` (+ optionally `evaluate`)
into one saved `Artifact` per evidence tier.

`train_tier` is the single function `scripts/score_v2/train_model.py`
calls once per tier (`match_v5`, `lcu_timeline`, `live_client`,
`aggregate`). It NEVER marks an artifact `production_ready=True` -- see
`docs/SCORE_V2_MODEL_CARD_TEMPLATE.md` "Release gates". Insufficient data
(too few usable pairwise labels for a non-trivial fit) is reported
honestly via `TierTrainingResult.status` rather than silently producing a
confident-looking artifact; the resulting artifact's coefficients are then
exactly the zero/neutral L2 prior (see `score_v2.training.baseline`), not
a fabricated signal.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Mapping, Optional

from score_features import SOURCE_PRIORITY
from score_v2.artifact import Artifact, FeatureCoefficient, build_artifact
from score_v2.feature_spec import feature_contract_for_tier
from score_v2.training import calibration as calibration_mod
from score_v2.training.baseline import (
    DEFAULT_GRADIENT_TOLERANCE,
    DEFAULT_ITERATIONS,
    DEFAULT_L2_LAMBDA,
    DEFAULT_LEARNING_RATE,
    DEFAULT_LOSS_TOLERANCE,
    DEFAULT_TIE_WEIGHT,
    fit_pairwise_baseline,
)
from score_v2.training.dataset import TrainingDataset

MIN_PAIRS_FOR_NONTRIVIAL_FIT = 20


@dataclass(frozen=True)
class TierTrainingResult:
    evidence_source: str
    status: str  # "fitted" | "insufficient_data"
    artifact: Artifact
    n_items: int
    n_pairs_used: int
    n_pairs_skipped: int
    notes: str

    def to_dict(self) -> dict:
        return {
            "evidence_source": self.evidence_source, "status": self.status,
            "n_items": self.n_items, "n_pairs_used": self.n_pairs_used,
            "n_pairs_skipped": self.n_pairs_skipped, "notes": self.notes,
            "artifact": self.artifact.to_dict(),
        }


def dataset_for_tier(dataset: TrainingDataset, evidence_source: str) -> TrainingDataset:
    """Restrict a multi-tier dataset to one evidence tier's records+pairs.

    Pair labels reference `base_ref` (tier-agnostic), so the SAME human
    label is re-resolved against each tier's own `base_ref` set here --
    it applies to a tier only if BOTH referenced participants actually
    have a record in that tier, never silently collapsed onto whichever
    tier happened to be filtered first.
    """
    records = tuple(
        record for record in dataset.feature_records
        if record.evidence_source == evidence_source
    )
    tier_base_refs = {record.base_ref for record in records}
    pairs = tuple(
        pair for pair in dataset.pair_labels
        if pair.left_ref in tier_base_refs and pair.right_ref in tier_base_refs
    )
    return TrainingDataset(
        schema_version=dataset.schema_version, feature_records=records, pair_labels=pairs,
    )


def train_tier(
        dataset: TrainingDataset, evidence_source: str, *, model_version: str,
        feature_version: str, calibration_version: str,
        l2_lambda: float = DEFAULT_L2_LAMBDA, learning_rate: float = DEFAULT_LEARNING_RATE,
        iterations: int = DEFAULT_ITERATIONS, tie_weight: float = DEFAULT_TIE_WEIGHT,
        loss_tolerance: float = DEFAULT_LOSS_TOLERANCE,
        gradient_tolerance: float = DEFAULT_GRADIENT_TOLERANCE,
        min_pairs_for_nontrivial_fit: int = MIN_PAIRS_FOR_NONTRIVIAL_FIT,
        include_abstained: bool = False,
        scale_sigma_multiplier: float = 1.0,
        fallback: Optional[Mapping] = None,
        now: Optional[datetime.datetime] = None) -> TierTrainingResult:
    """Fit, calibrate, and export one evidence tier's development artifact.

    `dataset` may contain records/pairs for other tiers too -- it is
    filtered down via `dataset_for_tier` first, so callers can pass one
    combined multi-tier `TrainingDataset` and call this once per tier.
    Uses `evidence_source`'s own canonical feature contract (see
    `score_v2.feature_spec.feature_contract_for_tier`) -- `aggregate`
    trains on only its three always-available raw KDA features, never the
    richer tiers' event/frame-derived ones.

    If `fitted.n_pairs_used < min_pairs_for_nontrivial_fit`, the exported
    artifact is GENUINELY NEUTRAL: every coefficient is exactly `0.0`,
    normalization is a no-op (`center=0.0, scale=1.0`), and role/score
    calibration are the fixed neutral defaults -- NOT whatever the
    underlying (statistically unreliable) fit happened to produce. Real,
    nonzero ("exploratory") coefficients are only ever exported when a
    caller explicitly lowers `min_pairs_for_nontrivial_fit` below the
    tier's actual usable-pair count. `training_metadata` still reports the
    REAL `n_pairs_used`/`n_pairs_skipped`/`n_items` either way.
    """
    if evidence_source not in SOURCE_PRIORITY:
        raise ValueError(f"Unknown evidence_source {evidence_source!r}")

    specs = feature_contract_for_tier(evidence_source)
    tier_dataset = dataset_for_tier(dataset, evidence_source)
    fitted = fit_pairwise_baseline(
        tier_dataset, specs=specs, l2_lambda=l2_lambda, learning_rate=learning_rate,
        iterations=iterations, tie_weight=tie_weight, loss_tolerance=loss_tolerance,
        gradient_tolerance=gradient_tolerance, include_abstained=include_abstained,
    )

    sufficient = fitted.n_pairs_used >= min_pairs_for_nontrivial_fit
    status = "fitted" if sufficient else "insufficient_data"

    if sufficient:
        role_calibration = calibration_mod.fit_role_calibration(
            tier_dataset, fitted, include_abstained=include_abstained,
        )
        score_calibration = calibration_mod.fit_score_calibration(
            tier_dataset, fitted, role_calibration,
            scale_sigma_multiplier=scale_sigma_multiplier,
            include_abstained=include_abstained,
        )
        coefficients = tuple(
            FeatureCoefficient(
                spec=spec, coefficient=fitted.coefficients[spec.name],
                robust_center=fitted.normalizations[spec.name].center,
                robust_scale=fitted.normalizations[spec.name].scale,
            )
            for spec in fitted.specs
        )
        intercept = fitted.intercept
    else:
        # Genuinely neutral: discard whatever the underlying fit produced
        # (even if some coefficients happened to be nonzero) rather than
        # exporting a statistically-unreliable "fitted-looking" artifact
        # under an "insufficient_data" label.
        role_calibration = calibration_mod.neutral_role_calibration(
            tier_dataset, include_abstained=include_abstained,
        )
        score_calibration = calibration_mod.neutral_score_calibration()
        coefficients = tuple(
            FeatureCoefficient(spec=spec, coefficient=0.0, robust_center=0.0, robust_scale=1.0)
            for spec in fitted.specs
        )
        intercept = 0.0

    notes = (
        f"{fitted.n_pairs_used} usable pairwise labels for tier "
        f"'{evidence_source}' ({fitted.n_pairs_skipped} skipped as "
        "tie/insufficient_evidence/unmatched-ref; "
        f"{fitted.n_items_excluded_abstain} records excluded as abstained). "
        + (
            "Meets the configured minimum for a non-trivial fit; still "
            "development-only pending the release gates in "
            "docs/SCORE_V2_MODEL_CARD_TEMPLATE.md."
            if sufficient else
            f"Below the configured minimum of {min_pairs_for_nontrivial_fit} "
            "-- this artifact's coefficients/calibration are the genuinely "
            "neutral prior (no fitted signal at all), not a real model. "
            "This artifact must never be routed as production evidence."
        )
    )

    training_metadata = {
        "trained_at": (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(),
        "n_items": fitted.n_items, "n_items_excluded_abstain": fitted.n_items_excluded_abstain,
        "n_pairs_used": fitted.n_pairs_used, "n_pairs_skipped": fitted.n_pairs_skipped,
        "l2_lambda": l2_lambda, "learning_rate": learning_rate,
        "iterations_configured": iterations, "iterations_run": fitted.iterations_run,
        "converged": fitted.converged, "final_loss": fitted.final_loss,
        "min_pairs_for_nontrivial_fit": min_pairs_for_nontrivial_fit,
        "include_abstained": include_abstained, "status": status,
    }

    artifact = build_artifact(
        model_version=model_version, feature_version=feature_version,
        calibration_version=calibration_version, evidence_source=evidence_source,
        intercept=intercept, coefficients=coefficients,
        role_calibration=role_calibration, score_calibration=score_calibration,
        confidence_params=calibration_mod.default_confidence_params(),
        abstention_params=calibration_mod.default_abstention_params(),
        training_metadata=training_metadata, evaluation_metadata=None,
        production_ready=False,
        release_notes=(
            "Pipeline-ready development artifact, NOT production-trained "
            "and NOT a replacement for DAEMON Score v1. " + notes
        ),
        fallback=fallback, now=now,
    )

    return TierTrainingResult(
        evidence_source=evidence_source, status=status, artifact=artifact,
        n_items=fitted.n_items, n_pairs_used=fitted.n_pairs_used,
        n_pairs_skipped=fitted.n_pairs_skipped, notes=notes,
    )


def train_all_tiers(
        dataset: TrainingDataset, *, model_version: str, feature_version: str,
        calibration_version: str, **kwargs) -> dict[str, TierTrainingResult]:
    """Train every one of the four evidence tiers present in `dataset`.

    A tier with zero `FeatureRecord`s in `dataset` is skipped entirely
    (not trained with a fabricated empty artifact) -- callers can check
    `set(results) == set(SOURCE_PRIORITY)` to see whether every tier had
    at least some data.
    """
    tiers_with_data = {record.evidence_source for record in dataset.feature_records}
    results = {}
    for evidence_source in SOURCE_PRIORITY:
        if evidence_source not in tiers_with_data:
            continue
        results[evidence_source] = train_tier(
            dataset, evidence_source, model_version=model_version,
            feature_version=feature_version, calibration_version=calibration_version,
            **kwargs,
        )
    return results
