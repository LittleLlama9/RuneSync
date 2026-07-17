"""Deterministic, stdlib-only comparison orchestrator across DAEMON Score
v2's four monotonic model families -- linear (`score_v2.training.baseline`),
a monotonic GAM (`score_v2.training.gam`), monotonic boosted stumps
(`score_v2.training.boosting`), and a monotonic tree
(`score_v2.training.tree`) -- run independently PER EVIDENCE TIER.

For each tier, `compare_tier`:

  1. Restricts `dataset` to that tier (`score_v2.training.export.dataset_for_tier`,
     which re-resolves each `PairLabel` against the tier's own `base_ref`
     set -- see that module for why this matters when the same
     game/participant has records in multiple tiers) and then to each of
     the three corpus splits already assigned on `FeatureRecord.split`
     (`score_v2.training.dataset.select_split`): TRAIN, VALIDATION, TEST.
     An empty split is never silently treated as "use everything" -- see
     `select_split`'s own docstring; a tier with zero train-split records
     short-circuits to `status="insufficient_data"` with `candidates=()`
     and `selected_model=None`.
  2. Fits every one of the four families ONLY on the TRAIN split via
     their own `fit_pairwise_*` entry point, reusing every existing
     safeguard: the abstain exclusion, single-tier assertion, PairLabel
     validation, and the shared robust-normalization/monotonic-projection
     machinery in `score_v2.training.monotonic_utils`. **The outer
     VALIDATION split is unseen by every family during this step,
     including boosting**: boosting's own internal early-stopping
     mechanism derives a separate, deterministic INNER (fit, stop) split
     from the TRAIN dataset alone
     (`score_v2.training.boosting.derive_inner_early_stop_split`, grouped
     by connected game-id components so no pair/group ever straddles the
     inner boundary) -- if TRAIN is too small to safely form both
     non-trivial inner sides, early stopping is honestly disabled rather
     than falling back to the outer validation split. This keeps the
     outer VALIDATION split an equally arms-length judge of every family;
     none of them gets an early "peek" at it before selection.
  3. Applies a per-family MINIMUM-USABLE-TRAIN-PAIRS eligibility gate
     BEFORE any validation-based comparison is even attempted -- see the
     module-level `MIN_PAIRS_*` constants and their docstring for why
     each non-linear family's threshold is a materially higher, honestly
     conservative multiple of the linear baseline's own long-established
     `score_v2.training.export.MIN_PAIRS_FOR_NONTRIVIAL_FIT` (higher
     capacity implies higher overfitting risk on tiny data, so a higher
     bar before it is even considered -- independent of how well it
     happens to fit the tiny training set it was given). A fitted
     monotonic tree that degenerates to a single leaf
     (`score_v2.model_shapes.tree_depth(root) <= 1`, i.e. no split
     survived its OWN internal guardrails) is additionally marked
     ineligible -- a lone leaf must never be presented as architecturally
     distinct from the linear baseline's own honest zero/neutral case.
  4. For every ELIGIBLE candidate: fits role/score calibration from the
     SAME train split via the generic, family-agnostic
     `score_v2.training.calibration.fit_role_calibration_for_score_fn`/
     `fit_score_calibration_for_score_fn`, builds one complete,
     self-consistent, content-hashed, in-memory `Artifact` via
     `score_v2.artifact.build_artifact` (this module never saves an
     artifact to disk itself -- see `scripts/score_v2/compare_models.py`
     for the one explicit, clearly-labeled opt-in export path), then
     evaluates that artifact on the VALIDATION split via the real
     dependency-free `score_v2.runtime.score_participant` path (never a
     training-time shortcut) and the full
     `score_v2.training.evaluate.evaluate_dataset` metric suite.
  5. SELECTS the candidate with the best VALIDATION pairwise accuracy
     among those that produced one at all -- NEVER a training metric ("no
     model wins on training metrics alone"). Ties are broken
     deterministically: fewer `n_parameters` first (prefer the simpler
     model), then family name (alphabetical) -- never insertion order,
     never randomness. If NO eligible candidate produced a scoreable
     validation pairwise accuracy, `selected_model=None`.
  6. Only AFTER selection, evaluates the SAME frozen selected artifact on
     the TEST split, purely for reporting -- test never influences which
     family was chosen.

This module builds artifacts (in memory) ONLY to run them through the
real scoring path during comparison; it never marks anything
`production_ready=True`, and nothing it returns is a release candidate --
see `docs/SCORE_V2_MODEL_CARD_TEMPLATE.md`.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from score_features import SOURCE_PRIORITY
from score_v2.artifact import (
    Artifact,
    FeatureCoefficient,
    MODEL_FAMILY_BOOSTED_STUMPS,
    MODEL_FAMILY_GAM,
    MODEL_FAMILY_LINEAR,
    MODEL_FAMILY_MONOTONIC_TREE,
    build_artifact,
)
from score_v2.feature_spec import FeatureSpec, extract_feature_vector, feature_contract_for_tier
from score_v2.model_shapes import (
    evaluate_boosted_stumps,
    evaluate_gam_shapes,
    evaluate_tree,
    tree_depth,
    tree_node_count,
)
from score_v2.runtime import score_participant
from score_v2.training import calibration as calibration_mod
from score_v2.training.baseline import fit_pairwise_baseline
from score_v2.training.boosting import derive_inner_early_stop_split, fit_pairwise_boosted_stumps
from score_v2.training.dataset import FeatureRecord, TrainingDataset, select_split
from score_v2.training.evaluate import evaluate_dataset
from score_v2.training.export import MIN_PAIRS_FOR_NONTRIVIAL_FIT, dataset_for_tier
from score_v2.training.gam import fit_pairwise_gam
from score_v2.training.tree import fit_pairwise_monotonic_tree

MODEL_FAMILIES_IN_ORDER = (
    MODEL_FAMILY_LINEAR, MODEL_FAMILY_GAM, MODEL_FAMILY_BOOSTED_STUMPS,
    MODEL_FAMILY_MONOTONIC_TREE,
)

# Minimum USABLE train pairs before a family is even considered eligible for
# validation-based comparison -- deliberately conservative, judgment-call
# multiples of the linear baseline's own long-established minimum
# (`score_v2.training.export.MIN_PAIRS_FOR_NONTRIVIAL_FIT`), reflecting each
# family's higher parameter count / overfitting risk. These are NOT
# empirically validated against a real corpus (none exists yet -- Score v2
# validation remains gated on the blocked Match-V5 authorization, see the
# vault decision); revisit once real labeled data exists.
MIN_PAIRS_LINEAR = MIN_PAIRS_FOR_NONTRIVIAL_FIT  # 20
MIN_PAIRS_GAM = 80
MIN_PAIRS_BOOSTED_STUMPS = 120
MIN_PAIRS_MONOTONIC_TREE = 60

DEFAULT_MIN_PAIRS_BY_FAMILY: Mapping[str, int] = {
    MODEL_FAMILY_LINEAR: MIN_PAIRS_LINEAR,
    MODEL_FAMILY_GAM: MIN_PAIRS_GAM,
    MODEL_FAMILY_BOOSTED_STUMPS: MIN_PAIRS_BOOSTED_STUMPS,
    MODEL_FAMILY_MONOTONIC_TREE: MIN_PAIRS_MONOTONIC_TREE,
}

DEFAULT_TRAIN_SPLIT = "train"
DEFAULT_VALIDATION_SPLIT = "validation"
DEFAULT_TEST_SPLIT = "test"

ScoreFn = Callable[[FeatureRecord], float]


@dataclass(frozen=True)
class _FittedCandidate:
    """Internal, family-agnostic view of one fitted candidate -- lets the
    orchestrator loop below treat all four families uniformly.
    """

    model_family: str
    specs: tuple[FeatureSpec, ...]
    n_items: int
    n_items_excluded_abstain: int
    n_pairs_used: int
    n_pairs_skipped: int
    n_parameters: int
    converged: Optional[bool]
    final_loss: Optional[float]
    extra_training_metadata: dict
    score_fn: ScoreFn
    shape_kwargs: dict  # passed straight through to build_artifact(**shape_kwargs)
    ineligible_structural_reason: Optional[str]


def _fit_linear_candidate(
        train_dataset: TrainingDataset, specs: Sequence[FeatureSpec],
        *, include_abstained: bool) -> _FittedCandidate:
    fitted = fit_pairwise_baseline(train_dataset, specs=specs, include_abstained=include_abstained)
    coefficients = tuple(
        FeatureCoefficient(
            spec=spec, coefficient=fitted.coefficients[spec.name],
            robust_center=fitted.normalizations[spec.name].center,
            robust_scale=fitted.normalizations[spec.name].scale,
        )
        for spec in fitted.specs
    )
    return _FittedCandidate(
        model_family=MODEL_FAMILY_LINEAR, specs=fitted.specs,
        n_items=fitted.n_items, n_items_excluded_abstain=fitted.n_items_excluded_abstain,
        n_pairs_used=fitted.n_pairs_used, n_pairs_skipped=fitted.n_pairs_skipped,
        n_parameters=len(coefficients), converged=fitted.converged, final_loss=fitted.final_loss,
        extra_training_metadata={"iterations_run": fitted.iterations_run},
        score_fn=lambda record: calibration_mod.raw_linear_score(record, fitted),
        shape_kwargs={"intercept": fitted.intercept, "coefficients": coefficients},
        ineligible_structural_reason=None,
    )


def _fit_gam_candidate(
        train_dataset: TrainingDataset, specs: Sequence[FeatureSpec],
        *, include_abstained: bool) -> _FittedCandidate:
    fitted = fit_pairwise_gam(train_dataset, specs=specs, include_abstained=include_abstained)
    shapes = tuple(fitted.shapes[spec.name] for spec in fitted.specs)

    def score_fn(record: FeatureRecord, fitted=fitted) -> float:
        vector = extract_feature_vector(record.features, specs=fitted.specs)
        return evaluate_gam_shapes(list(fitted.shapes.values()), vector)

    return _FittedCandidate(
        model_family=MODEL_FAMILY_GAM, specs=fitted.specs,
        n_items=fitted.n_items, n_items_excluded_abstain=fitted.n_items_excluded_abstain,
        n_pairs_used=fitted.n_pairs_used, n_pairs_skipped=fitted.n_pairs_skipped,
        n_parameters=fitted.n_parameters, converged=fitted.converged, final_loss=fitted.final_loss,
        extra_training_metadata={"iterations_run": fitted.iterations_run},
        score_fn=score_fn,
        shape_kwargs={"intercept": 0.0, "coefficients": (), "gam_shapes": shapes},
        ineligible_structural_reason=None,
    )


def _fit_boosted_stumps_candidate(
        train_dataset: TrainingDataset, specs: Sequence[FeatureSpec],
        *, include_abstained: bool) -> _FittedCandidate:
    """Fit boosting on `train_dataset` using ONLY an INNER (fit, stop)
    split derived from `train_dataset` itself for early stopping --
    never the outer validation/test split, which must stay unseen by
    every family until cross-family selection (see
    `score_v2.training.boosting.derive_inner_early_stop_split`). If
    `train_dataset` is too small to safely form both non-trivial inner
    sides, early stopping is honestly disabled (fit on the whole
    `train_dataset`, `validation_dataset=None`) rather than reaching for
    the outer validation split.
    """
    inner_fit, inner_stop = derive_inner_early_stop_split(
        train_dataset, include_abstained=include_abstained,
    )
    inner_split_enabled = inner_stop is not None
    fit_input = inner_fit if inner_split_enabled else train_dataset

    fitted = fit_pairwise_boosted_stumps(
        fit_input, specs=specs, include_abstained=include_abstained,
        validation_dataset=inner_stop,
    )

    def score_fn(record: FeatureRecord, fitted=fitted) -> float:
        vector = extract_feature_vector(record.features, specs=fitted.specs)
        return evaluate_boosted_stumps(fitted.stumps, vector)

    return _FittedCandidate(
        model_family=MODEL_FAMILY_BOOSTED_STUMPS, specs=fitted.specs,
        n_items=fitted.n_items, n_items_excluded_abstain=fitted.n_items_excluded_abstain,
        n_pairs_used=fitted.n_pairs_used, n_pairs_skipped=fitted.n_pairs_skipped,
        n_parameters=fitted.n_parameters, converged=fitted.converged, final_loss=fitted.final_loss,
        extra_training_metadata={
            "rounds_run": fitted.rounds_run, "stopped_reason": fitted.stopped_reason,
            "best_validation_loss": fitted.best_validation_loss,
            "inner_early_stop_split_enabled": inner_split_enabled,
            "inner_fit_n_items": len(fit_input.feature_records),
            "inner_stop_n_items": (len(inner_stop.feature_records) if inner_split_enabled else 0),
        },
        score_fn=score_fn,
        shape_kwargs={"intercept": 0.0, "coefficients": (), "boosted_stumps": fitted.stumps},
        # An empty ensemble (round 1 already had no gain-positive candidate)
        # is not a genuine boosted-stumps candidate -- there is nothing to
        # build an artifact from, and presenting a zero-stump "ensemble" as
        # architecturally distinct from the linear zero/neutral case would
        # be dishonest.
        ineligible_structural_reason=(None if fitted.stumps else "no_stumps_fit"),
    )


def _fit_tree_candidate(
        train_dataset: TrainingDataset, specs: Sequence[FeatureSpec],
        *, include_abstained: bool) -> _FittedCandidate:
    fitted = fit_pairwise_monotonic_tree(
        train_dataset, specs=specs, include_abstained=include_abstained,
    )

    def score_fn(record: FeatureRecord, fitted=fitted) -> float:
        vector = extract_feature_vector(record.features, specs=fitted.specs)
        return evaluate_tree(fitted.root, vector)

    depth = tree_depth(fitted.root)
    return _FittedCandidate(
        model_family=MODEL_FAMILY_MONOTONIC_TREE, specs=fitted.specs,
        n_items=fitted.n_items, n_items_excluded_abstain=fitted.n_items_excluded_abstain,
        n_pairs_used=fitted.n_pairs_used, n_pairs_skipped=fitted.n_pairs_skipped,
        n_parameters=fitted.n_parameters,
        # A single greedy CART fit has no iterative loss-delta/gradient-norm
        # stopping criterion to report -- `None` ("not applicable"), never a
        # fabricated `True` just because a tree was produced.
        converged=None, final_loss=fitted.final_loss,
        extra_training_metadata={
            "tree_depth": depth, "tree_node_count": tree_node_count(fitted.root),
        },
        score_fn=score_fn,
        shape_kwargs={"intercept": 0.0, "coefficients": (), "monotonic_tree": fitted.root},
        ineligible_structural_reason=("degenerate_no_split" if depth <= 1 else None),
    )


_FIT_DISPATCH: Mapping[str, Callable] = {
    MODEL_FAMILY_LINEAR: (
        lambda train, specs, include_abstained:
        _fit_linear_candidate(train, specs, include_abstained=include_abstained)
    ),
    MODEL_FAMILY_GAM: (
        lambda train, specs, include_abstained:
        _fit_gam_candidate(train, specs, include_abstained=include_abstained)
    ),
    MODEL_FAMILY_BOOSTED_STUMPS: (
        lambda train, specs, include_abstained:
        _fit_boosted_stumps_candidate(train, specs, include_abstained=include_abstained)
    ),
    MODEL_FAMILY_MONOTONIC_TREE: (
        lambda train, specs, include_abstained:
        _fit_tree_candidate(train, specs, include_abstained=include_abstained)
    ),
}


def _group_records_by_game(records: Sequence[FeatureRecord]) -> dict[int, dict]:
    """Reconstruct one game's `compute_feature_set`-shaped dict from its
    `FeatureRecord`s, so `score_v2.runtime.score_participant` (which reads
    whole-game fields like `abstain`/`chosen_source_completeness`) can be
    called exactly as it would be at real runtime. A private duplicate of
    `scripts/score_v2/evaluate_model.py`'s own `_group_by_game` (kept
    separate rather than importing a script module, since scripts are
    entry points, not library code, in this repo's conventions).
    """
    games: dict[int, dict] = {}
    for record in records:
        game = games.setdefault(record.game_id, {
            "evidence_source": record.evidence_source,
            "abstain": record.abstain, "abstain_reason": record.abstain_reason,
            "chosen_source_completeness": record.chosen_source_completeness,
            "duration_seconds": record.duration_seconds,
            "participants": {},
        })
        game["participants"][str(record.participant_id)] = record.features
    return games


def _evaluate_artifact_on_split(
        artifact: Artifact, split_tier_dataset: TrainingDataset,
        *, bootstrap_seed: int, bootstrap_resamples: int) -> Optional[dict]:
    """Score every record in `split_tier_dataset` via the real
    `score_v2.runtime.score_participant` path and run the full
    `score_v2.training.evaluate.evaluate_dataset` metric suite. `None` if
    the split has zero records for this tier (never fabricated metrics).
    """
    if not split_tier_dataset.feature_records:
        return None
    games = _group_records_by_game(split_tier_dataset.feature_records)
    scores: dict[str, float] = {}
    confidences: dict[str, float] = {}
    for record in split_tier_dataset.feature_records:
        result = score_participant(artifact, games[record.game_id], record.participant_id)
        # Keyed by base_ref (tier-agnostic), matching PairLabel refs.
        scores[record.base_ref] = result.score
        confidences[record.base_ref] = result.confidence
    report = evaluate_dataset(
        split_tier_dataset, scores, confidences,
        bootstrap_seed=bootstrap_seed, bootstrap_resamples=bootstrap_resamples,
    )
    return report.to_dict()


@dataclass(frozen=True)
class CandidateResult:
    model_family: str
    eligible: bool
    ineligibility_reason: Optional[str]
    min_pairs_required: int
    n_parameters: int
    n_items: int
    n_items_excluded_abstain: int
    train_n_pairs_used: int
    train_n_pairs_skipped: int
    train_converged: Optional[bool]
    train_final_loss: Optional[float]
    training_metadata_extra: Mapping
    validation_evaluation: Optional[dict]
    validation_pairwise_accuracy: Optional[float]

    def to_dict(self) -> dict:
        return {
            "model_family": self.model_family, "eligible": self.eligible,
            "ineligibility_reason": self.ineligibility_reason,
            "min_pairs_required": self.min_pairs_required,
            "n_parameters": self.n_parameters,
            "n_items": self.n_items,
            "n_items_excluded_abstain": self.n_items_excluded_abstain,
            "train_n_pairs_used": self.train_n_pairs_used,
            "train_n_pairs_skipped": self.train_n_pairs_skipped,
            "train_converged": self.train_converged,
            "train_final_loss": self.train_final_loss,
            "training_metadata_extra": dict(self.training_metadata_extra),
            "validation_evaluation": self.validation_evaluation,
            "validation_pairwise_accuracy": self.validation_pairwise_accuracy,
        }


@dataclass(frozen=True)
class TierComparisonResult:
    evidence_source: str
    status: str  # "compared" | "insufficient_data"
    notes: str
    candidates: tuple[CandidateResult, ...]
    selected_model: Optional[str]
    selection_reason: str
    selected_artifact_content_hash: Optional[str]
    test_evaluation: Optional[dict]

    def to_dict(self) -> dict:
        return {
            "evidence_source": self.evidence_source, "status": self.status,
            "notes": self.notes,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "selected_model": self.selected_model,
            "selection_reason": self.selection_reason,
            "selected_artifact_content_hash": self.selected_artifact_content_hash,
            "test_evaluation": self.test_evaluation,
        }


def _build_candidate_artifact(
        fitted: _FittedCandidate, train_dataset: TrainingDataset, *,
        model_version: str, feature_version: str, calibration_version: str,
        evidence_source: str, min_required: int, include_abstained: bool,
        now: Optional[datetime.datetime]) -> Artifact:
    """Fit role/score calibration for one already-fitted candidate and
    build its complete, self-consistent, hashed, in-memory `Artifact`.
    Shared by `compare_tier` (building every ELIGIBLE candidate for
    validation scoring) and `build_artifact_for_family` (the one explicit,
    deterministic path for re-deriving a single family's artifact for
    inspection/export -- see `scripts/score_v2/compare_models.py`).
    """
    role_calibration = calibration_mod.fit_role_calibration_for_score_fn(
        train_dataset, fitted.score_fn, include_abstained=include_abstained,
    )
    score_calibration = calibration_mod.fit_score_calibration_for_score_fn(
        train_dataset, fitted.score_fn, role_calibration, include_abstained=include_abstained,
    )
    return build_artifact(
        model_version=model_version, feature_version=feature_version,
        calibration_version=calibration_version, evidence_source=evidence_source,
        role_calibration=role_calibration, score_calibration=score_calibration,
        confidence_params=calibration_mod.default_confidence_params(),
        abstention_params=calibration_mod.default_abstention_params(),
        training_metadata={
            "trained_at": (now or datetime.datetime.now(datetime.timezone.utc)).isoformat(),
            "n_items": fitted.n_items,
            "n_items_excluded_abstain": fitted.n_items_excluded_abstain,
            "n_pairs_used": fitted.n_pairs_used,
            "n_pairs_skipped": fitted.n_pairs_skipped,
            "converged": fitted.converged, "final_loss": fitted.final_loss,
            "include_abstained": include_abstained,
            "min_pairs_required": min_required,
            **fitted.extra_training_metadata,
        },
        production_ready=False,
        release_notes=(
            "Comparison-only development artifact produced by "
            "score_v2.training.compare -- NOT production-trained, NOT a "
            "replacement for DAEMON Score v1, and not saved to disk unless "
            "a caller explicitly opts in."
        ),
        model_family=fitted.model_family, now=now,
        **fitted.shape_kwargs,
    )


def build_artifact_for_family(
        dataset: TrainingDataset, evidence_source: str, model_family: str, *,
        model_version: str, feature_version: str, calibration_version: str,
        train_split: str = DEFAULT_TRAIN_SPLIT, include_abstained: bool = False,
        min_pairs_by_family: Optional[Mapping[str, int]] = None,
        now: Optional[datetime.datetime] = None) -> Artifact:
    """Fit exactly one family on exactly one tier's TRAIN split and build
    its `Artifact` -- the one explicit, deterministic path
    `scripts/score_v2/compare_models.py --export-selected-dir` uses to
    obtain a comparison winner's artifact object for on-disk export
    (`compare_tier` itself never returns artifact objects, only their
    content hash, to avoid tempting a caller into treating a
    `TierComparisonResult` as a release artifact).

    Raises `ValueError` if `model_family` is unknown or if the family is
    not eligible (below its own minimum train-pairs threshold, or -- for
    `monotonic_tree` -- degenerates to a single leaf) on this data; this
    function never silently exports a below-threshold or degenerate fit.
    """
    if model_family not in MODEL_FAMILIES_IN_ORDER:
        raise ValueError(f"Unknown model_family {model_family!r}")
    if evidence_source not in SOURCE_PRIORITY:
        raise ValueError(f"Unknown evidence_source {evidence_source!r}")

    effective_min_pairs = dict(DEFAULT_MIN_PAIRS_BY_FAMILY)
    if min_pairs_by_family:
        effective_min_pairs.update(min_pairs_by_family)

    specs = feature_contract_for_tier(evidence_source)
    tier_dataset = dataset_for_tier(dataset, evidence_source)
    train_dataset = select_split(tier_dataset, train_split)
    if not train_dataset.feature_records:
        raise ValueError(
            f"No records carry split={train_split!r} for evidence tier "
            f"{evidence_source!r}; refusing to build an artifact from zero "
            "training records"
        )

    fitted = _FIT_DISPATCH[model_family](train_dataset, specs, include_abstained)
    min_required = effective_min_pairs[model_family]
    if fitted.ineligible_structural_reason is not None:
        raise ValueError(
            f"{model_family} is structurally ineligible on this data: "
            f"{fitted.ineligible_structural_reason}"
        )
    if fitted.n_pairs_used < min_required:
        raise ValueError(
            f"{model_family} has only {fitted.n_pairs_used} usable train "
            f"pairs, below its minimum of {min_required}; refusing to "
            "export a below-threshold fit"
        )

    return _build_candidate_artifact(
        fitted, train_dataset, model_version=model_version,
        feature_version=feature_version, calibration_version=calibration_version,
        evidence_source=evidence_source, min_required=min_required,
        include_abstained=include_abstained, now=now,
    )


def compare_tier(
        dataset: TrainingDataset, evidence_source: str, *,
        model_version: str, feature_version: str, calibration_version: str,
        train_split: str = DEFAULT_TRAIN_SPLIT,
        validation_split: str = DEFAULT_VALIDATION_SPLIT,
        test_split: str = DEFAULT_TEST_SPLIT,
        min_pairs_by_family: Optional[Mapping[str, int]] = None,
        include_abstained: bool = False,
        bootstrap_seed: int = 1337, bootstrap_resamples: int = 200,
        now: Optional[datetime.datetime] = None) -> TierComparisonResult:
    """Fit and compare all four model families for one evidence tier.

    See the module docstring for the full procedure. `dataset` may
    contain records/pairs for other tiers and other splits too -- both
    are filtered down here (`dataset_for_tier` then `select_split` three
    times), so callers can pass one combined, already-split-assigned
    multi-tier `TrainingDataset` and call this once per tier.
    """
    if evidence_source not in SOURCE_PRIORITY:
        raise ValueError(f"Unknown evidence_source {evidence_source!r}")

    effective_min_pairs = dict(DEFAULT_MIN_PAIRS_BY_FAMILY)
    if min_pairs_by_family:
        effective_min_pairs.update(min_pairs_by_family)

    specs = feature_contract_for_tier(evidence_source)
    tier_dataset = dataset_for_tier(dataset, evidence_source)
    train_dataset = select_split(tier_dataset, train_split)
    validation_dataset = select_split(tier_dataset, validation_split)
    test_dataset = select_split(tier_dataset, test_split)

    if not train_dataset.feature_records:
        return TierComparisonResult(
            evidence_source=evidence_source, status="insufficient_data",
            notes=(
                f"No records carry split={train_split!r} for evidence tier "
                f"{evidence_source!r} -- nothing to fit. Honestly reported; "
                "never silently substituted with the full dataset (pass "
                "split='none'-equivalent data explicitly if that is truly "
                "intended)."
            ),
            candidates=(), selected_model=None, selection_reason="no_train_records",
            selected_artifact_content_hash=None, test_evaluation=None,
        )

    # NOTE: boosting's OWN internal early stopping derives a deterministic
    # INNER split from `train_dataset` alone (see
    # `_fit_boosted_stumps_candidate`/`derive_inner_early_stop_split`) --
    # the OUTER `validation_dataset` below is reserved exclusively for
    # cross-family selection and must never be touched during fitting for
    # ANY family, boosting included.

    candidates: list[CandidateResult] = []
    fitted_artifacts: dict[str, tuple[_FittedCandidate, Optional[Artifact]]] = {}

    for model_family in MODEL_FAMILIES_IN_ORDER:
        fitted = _FIT_DISPATCH[model_family](train_dataset, specs, include_abstained)
        min_required = effective_min_pairs[model_family]
        below_threshold = fitted.n_pairs_used < min_required
        eligible = not below_threshold and fitted.ineligible_structural_reason is None

        ineligibility_reason = None
        if fitted.ineligible_structural_reason is not None:
            ineligibility_reason = fitted.ineligible_structural_reason
        elif below_threshold:
            ineligibility_reason = (
                f"train_n_pairs_used={fitted.n_pairs_used} below the minimum "
                f"{min_required} required for the {model_family!r} family"
            )

        validation_evaluation = None
        validation_pairwise_accuracy = None
        artifact: Optional[Artifact] = None

        if eligible:
            artifact = _build_candidate_artifact(
                fitted, train_dataset, model_version=model_version,
                feature_version=feature_version, calibration_version=calibration_version,
                evidence_source=evidence_source, min_required=min_required,
                include_abstained=include_abstained, now=now,
            )
            validation_evaluation = _evaluate_artifact_on_split(
                artifact, validation_dataset,
                bootstrap_seed=bootstrap_seed, bootstrap_resamples=bootstrap_resamples,
            )
            if validation_evaluation is not None:
                validation_pairwise_accuracy = (
                    validation_evaluation["pairwise_accuracy_overall"]["accuracy"]
                )

        fitted_artifacts[model_family] = (fitted, artifact)
        candidates.append(CandidateResult(
            model_family=model_family, eligible=eligible,
            ineligibility_reason=ineligibility_reason, min_pairs_required=min_required,
            n_parameters=fitted.n_parameters, n_items=fitted.n_items,
            n_items_excluded_abstain=fitted.n_items_excluded_abstain,
            train_n_pairs_used=fitted.n_pairs_used, train_n_pairs_skipped=fitted.n_pairs_skipped,
            train_converged=fitted.converged, train_final_loss=fitted.final_loss,
            training_metadata_extra=fitted.extra_training_metadata,
            validation_evaluation=validation_evaluation,
            validation_pairwise_accuracy=validation_pairwise_accuracy,
        ))

    # Selection: VALIDATION pairwise accuracy only -- never a training
    # metric. "no model wins on training metrics alone."
    selectable = [
        candidate for candidate in candidates
        if candidate.eligible and candidate.validation_pairwise_accuracy is not None
    ]
    if not selectable:
        return TierComparisonResult(
            evidence_source=evidence_source, status="compared",
            notes=(
                "No eligible candidate produced a scoreable validation "
                "pairwise accuracy (either no family met its minimum "
                f"train-pairs threshold, or split={validation_split!r} has "
                "no scoreable decisive pairs for this tier)."
            ),
            candidates=tuple(candidates), selected_model=None,
            selection_reason="no_selectable_candidate",
            selected_artifact_content_hash=None, test_evaluation=None,
        )

    best_accuracy = max(candidate.validation_pairwise_accuracy for candidate in selectable)
    tied = [
        candidate for candidate in selectable
        if candidate.validation_pairwise_accuracy == best_accuracy
    ]
    tied.sort(key=lambda candidate: (candidate.n_parameters, candidate.model_family))
    winner = tied[0]

    selection_reason = (
        f"highest validation pairwise accuracy ({best_accuracy:.4f}) among "
        f"{len(selectable)} eligible, scoreable candidate(s)"
        + (
            f"; tie-broken by fewer parameters then family name among "
            f"{len(tied)} tied candidate(s)"
            if len(tied) > 1 else ""
        )
    )

    _, winning_artifact = fitted_artifacts[winner.model_family]
    test_evaluation = _evaluate_artifact_on_split(
        winning_artifact, test_dataset,
        bootstrap_seed=bootstrap_seed, bootstrap_resamples=bootstrap_resamples,
    )

    return TierComparisonResult(
        evidence_source=evidence_source, status="compared",
        notes=(
            f"{len(candidates)} candidate families attempted "
            f"({len(selectable)} eligible and scoreable on validation); "
            "selection is validation-only, test evaluated only for the "
            "winner and never used to choose it."
        ),
        candidates=tuple(candidates), selected_model=winner.model_family,
        selection_reason=selection_reason,
        selected_artifact_content_hash=winning_artifact.content_hash,
        test_evaluation=test_evaluation,
    )


def compare_all_tiers(
        dataset: TrainingDataset, *, model_version: str, feature_version: str,
        calibration_version: str, **kwargs) -> dict[str, TierComparisonResult]:
    """Compare every evidence tier present in `dataset`.

    A tier with zero `FeatureRecord`s in `dataset` is skipped entirely
    (not compared with a fabricated empty result) -- mirrors
    `score_v2.training.export.train_all_tiers`.
    """
    tiers_with_data = {record.evidence_source for record in dataset.feature_records}
    results = {}
    for evidence_source in SOURCE_PRIORITY:
        if evidence_source not in tiers_with_data:
            continue
        results[evidence_source] = compare_tier(
            dataset, evidence_source, model_version=model_version,
            feature_version=feature_version, calibration_version=calibration_version,
            **kwargs,
        )
    return results
