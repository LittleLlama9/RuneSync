"""Role calibration and score-mapping calibration for one tier's baseline.

Two independent calibration layers sit between `baseline.FittedBaseline`'s
raw linear score and the final 0-100 DAEMON Score v2 number:

  1. **Role calibration** -- an empirical-Bayes-shrunk per-role offset
     subtracted from the raw linear score before mapping, so a role whose
     feature profile is structurally different (e.g. support's naturally
     lower kill/gold counts) is not penalized purely for being that role.
     `offset = mean(raw_linear_score | role) * (n / (n + shrinkage_k))` --
     a role with few (or zero) training rows shrinks its offset toward 0
     rather than trusting a noisy or nonexistent per-role mean.
  2. **Score mapping** -- `score = midpoint + half_range * tanh(adjusted /
     scale)`, centered on the semantic midpoint 50, bounded in
     `[clip_min, clip_max]` by construction (`tanh` saturates at +/-1).
     `scale` is fit from the training set's own robust spread of adjusted
     scores; it only falls back to a fixed default when there is no
     measurable spread (e.g. zero usable pairs).

Both are honest about small samples: see the module-level constants for
the exact fallback values used, and `training_metadata`/`release_notes` on
the resulting `Artifact` for how to tell a real fit from a fallback.
"""

from __future__ import annotations

import statistics
from typing import Callable, Mapping

from score_v2.artifact import RoleCalibration
from score_v2.feature_spec import extract_feature_vector
from score_v2.training.baseline import FittedBaseline
from score_v2.training.dataset import FeatureRecord, TrainingDataset

ROLE_NAMES = ("top", "jungle", "mid", "bot", "support", "unknown")
DEFAULT_ROLE_SHRINKAGE_K = 5.0
DEFAULT_SCORE_SCALE = 5.0
MIDPOINT = 50.0
CLIP_MIN = 0.0
CLIP_MAX = 100.0
_MAD_TO_STD = 1.4826
_MAD_EPSILON = 1e-6

ScoreFn = Callable[[FeatureRecord], float]


def raw_linear_score(record: FeatureRecord, fitted: FittedBaseline) -> float:
    """Recompute one record's raw (pre-role-offset) linear score.

    Shared by calibration fitting here and by
    `score_v2.training.evaluate` so both operate on identical numbers.
    """
    vector = extract_feature_vector(record.features, specs=fitted.specs)
    total = fitted.intercept
    for spec in fitted.specs:
        value = vector[spec.name]
        if not value.present:
            continue
        normalization = fitted.normalizations[spec.name]
        total += fitted.coefficients[spec.name] * normalization.apply(value.transformed)
    return total


def fit_role_calibration_for_score_fn(
        dataset: TrainingDataset, score_fn: ScoreFn,
        *, shrinkage_k: float = DEFAULT_ROLE_SHRINKAGE_K,
        include_abstained: bool = False) -> dict[str, RoleCalibration]:
    """Fit per-role offsets from `dataset`'s raw model scores, for ANY
    model family. `score_fn(record)` computes one record's raw
    (pre-role-offset) score -- the linear family passes
    `lambda record: raw_linear_score(record, fitted)`
    (see `fit_role_calibration` below); the non-linear families
    (`score_v2.training.gam`/`boosting`/`tree`) pass a closure over their
    own fitted shape's `model_shapes.evaluate_*` function instead. This
    keeps role/score calibration IDENTICAL in method across every family
    -- only how the raw score itself is computed differs.

    `include_abstained=False` (the default) excludes any `FeatureRecord`
    with `abstain=True` from the per-role mean -- a short-game/low-evidence
    record's feature values should not pull a role's calibration offset.
    """
    scores_by_role: dict[str, list[float]] = {role: [] for role in ROLE_NAMES}
    for record in dataset.feature_records:
        if record.abstain and not include_abstained:
            continue
        role = record.role if record.role in ROLE_NAMES else "unknown"
        scores_by_role[role].append(score_fn(record))

    calibration: dict[str, RoleCalibration] = {}
    for role, scores in scores_by_role.items():
        n = len(scores)
        if n == 0:
            calibration[role] = RoleCalibration(
                offset=0.0, sample_count=0, shrinkage_weight=0.0,
            )
            continue
        raw_mean = statistics.fmean(scores)
        shrinkage_weight = n / (n + shrinkage_k)
        calibration[role] = RoleCalibration(
            offset=raw_mean * shrinkage_weight, sample_count=n,
            shrinkage_weight=shrinkage_weight,
        )
    return calibration


def fit_score_calibration_for_score_fn(
        dataset: TrainingDataset, score_fn: ScoreFn,
        role_calibration: Mapping[str, RoleCalibration],
        *, default_scale: float = DEFAULT_SCORE_SCALE,
        scale_sigma_multiplier: float = 1.0,
        include_abstained: bool = False) -> dict:
    """Fit the score-mapping `scale` from `dataset`'s adjusted raw model
    scores, for ANY model family -- see `fit_role_calibration_for_score_fn`
    for what `score_fn` is.

    `include_abstained=False` (the default) excludes abstained records
    from the spread measurement, matching `fit_role_calibration_for_score_fn`.

    `scale_sigma_multiplier` (default 1.0 = unchanged) widens the fitted
    tanh `scale` by a constant factor. At 1.0 the mapping's inflection
    matches ~1 robust std of adjusted scores, so a heavy-tailed
    distribution pins a large mass to the 0/100 rails (the v2 "extreme
    magnitude" defect). A multiplier of ~2.0 moves the tanh knee out to
    ~2 std, keeping genuine ordering/spread while pulling the tails off the
    rails. Only the fitted spread is scaled; the fixed `default_scale`
    fallback (no measurable spread) is left alone so degenerate tiers stay
    on their documented neutral default.
    """
    if scale_sigma_multiplier <= 0:
        raise ValueError(
            f"scale_sigma_multiplier must be > 0, got {scale_sigma_multiplier}"
        )
    adjusted_scores = []
    for record in dataset.feature_records:
        if record.abstain and not include_abstained:
            continue
        role = record.role if record.role in ROLE_NAMES else "unknown"
        offset = role_calibration[role].offset if role in role_calibration else 0.0
        adjusted_scores.append(score_fn(record) - offset)

    scale = default_scale
    if len(adjusted_scores) >= 2:
        center = statistics.median(adjusted_scores)
        mad = statistics.median(abs(value - center) for value in adjusted_scores)
        candidate_scale = mad * _MAD_TO_STD * scale_sigma_multiplier
        if candidate_scale >= _MAD_EPSILON:
            scale = candidate_scale

    return {
        "midpoint": MIDPOINT, "scale": scale, "clip_min": CLIP_MIN, "clip_max": CLIP_MAX,
    }


def fit_role_calibration(
        dataset: TrainingDataset, fitted: FittedBaseline,
        *, shrinkage_k: float = DEFAULT_ROLE_SHRINKAGE_K,
        include_abstained: bool = False) -> dict[str, RoleCalibration]:
    """Fit per-role offsets from `dataset`'s raw linear scores.

    `include_abstained=False` (the default) excludes any `FeatureRecord`
    with `abstain=True` from the per-role mean -- a short-game/low-evidence
    record's feature values should not pull a role's calibration offset,
    matching `score_v2.training.baseline.fit_pairwise_baseline`'s default.

    A thin, behavior-preserving wrapper around
    `fit_role_calibration_for_score_fn` specialized to the linear family.
    """
    return fit_role_calibration_for_score_fn(
        dataset, lambda record: raw_linear_score(record, fitted),
        shrinkage_k=shrinkage_k, include_abstained=include_abstained,
    )


def fit_score_calibration(
        dataset: TrainingDataset, fitted: FittedBaseline,
        role_calibration: Mapping[str, RoleCalibration],
        *, default_scale: float = DEFAULT_SCORE_SCALE,
        scale_sigma_multiplier: float = 1.0,
        include_abstained: bool = False) -> dict:
    """Fit the score-mapping `scale` from `dataset`'s adjusted linear scores.

    `include_abstained=False` (the default) excludes abstained records
    from the spread measurement, matching `fit_role_calibration`.

    `scale_sigma_multiplier` (default 1.0 = unchanged) is forwarded to
    widen the fitted tanh knee -- see
    `fit_score_calibration_for_score_fn`.

    A thin, behavior-preserving wrapper around
    `fit_score_calibration_for_score_fn` specialized to the linear family.
    """
    return fit_score_calibration_for_score_fn(
        dataset, lambda record: raw_linear_score(record, fitted), role_calibration,
        default_scale=default_scale, scale_sigma_multiplier=scale_sigma_multiplier,
        include_abstained=include_abstained,
    )


def neutral_role_calibration(
        dataset: TrainingDataset, *, include_abstained: bool = False,
) -> dict[str, RoleCalibration]:
    """Genuinely neutral role calibration: every offset/shrinkage_weight is
    0.0 -- used when there is not enough pairwise supervision to trust a
    real fit (see `score_v2.training.export.train_tier`'s
    `"insufficient_data"` path). `sample_count` per role is still honestly
    reported (how many training rows exist), even though no calibration is
    actually applied.
    """
    counts: dict[str, int] = {role: 0 for role in ROLE_NAMES}
    for record in dataset.feature_records:
        if record.abstain and not include_abstained:
            continue
        role = record.role if record.role in ROLE_NAMES else "unknown"
        counts[role] += 1
    return {
        role: RoleCalibration(offset=0.0, sample_count=count, shrinkage_weight=0.0)
        for role, count in counts.items()
    }


def neutral_score_calibration(*, default_scale: float = DEFAULT_SCORE_SCALE) -> dict:
    """Genuinely neutral score mapping: the fixed default scale, never a
    value derived from an insufficiently-supported real fit.
    """
    return {
        "midpoint": MIDPOINT, "scale": default_scale, "clip_min": CLIP_MIN,
        "clip_max": CLIP_MAX,
    }


def default_confidence_params() -> dict:
    return {
        "missing_feature_penalty": 0.5,
        "evidence_quality_weight": 0.5,
        "interval_min_half_width": 3.0,
        "interval_max_half_width": 40.0,
    }


def default_abstention_params() -> dict:
    return {
        "short_game_seconds": 600.0,
        "min_present_feature_fraction": 0.3,
        "min_confidence_to_report": 0.15,
    }
