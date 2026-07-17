"""Tests for score_v2/training/baseline.py -- the pairwise baseline trainer.

Sections:
  1. Robust normalization edge cases (empty, single value, zero MAD).
  2. Zero-pair training: honest neutral/zero baseline, not fabricated.
  3. Monotonic sign projection actually constrains fitted coefficients.
  4. Determinism (same input -> bit-identical output).
  5. Pairwise correction: training on separable synthetic data reduces
     loss and yields the domain-correct coefficient signs.
  6. Honest convergence: `converged` reflects a real stopping criterion,
     never "at least one pair existed"; the intercept is never trained
     (stays fixed at 0.0, no phantom gradient).
  7. Single-tier assertion: a mixed-tier dataset is rejected outright.
  8. Abstained records/pairs excluded by default; `include_abstained`
     overrides that explicitly.
"""

import pytest

from score_v2.feature_spec import DIRECTION_NEGATIVE, DIRECTION_POSITIVE, FEATURE_ALLOWLIST
from score_v2.training.baseline import (
    fit_pairwise_baseline,
    fit_robust_normalization,
)
from score_v2.training.dataset import (
    DatasetValidationError,
    PairLabel,
    TrainingDataset,
    build_feature_record,
)


def _gf(kills, deaths, assists, role="mid", abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": assists},
            "fight_influence": {
                "kill_events": kills, "death_events": deaths, "assist_events": assists,
                "first_blood": kills >= 6, "untraded_deaths": max(0, deaths - 2),
                "event_kill_participation": min(1.0, (kills + assists) / 10.0),
            },
            "baseline": {"role": role, "champion": "TestChamp", "patch": "14.1"},
        }},
    }


def _record(game_id, kills, deaths, assists, split="train", evidence_source="match_v5", abstain=False):
    gf = _gf(kills, deaths, assists, abstain=abstain)
    return build_feature_record(
        game_id=game_id, participant_id=1, evidence_source=evidence_source,
        features_for_game=gf, split=split,
    )


def _pair(left_ref, right_ref, choice, confidence=0.9):
    if choice == "left":
        winner_ref, relation = left_ref, "left_preferred"
    elif choice == "right":
        winner_ref, relation = right_ref, "right_preferred"
    else:
        winner_ref, relation = None, choice
    return PairLabel(
        pair_id=f"{left_ref}|{right_ref}", left_ref=left_ref, right_ref=right_ref,
        winner_ref=winner_ref, relation=relation, choice=choice, confidence=confidence,
        rationale_tags=("combat_impact",), reviewer_id="r1",
        created_at="2026-01-01T00:00:00+00:00",
    )


# ── 1. robust normalization ──────────────────────────────────────────────────

def test_fit_robust_normalization_empty_falls_back_to_noop():
    norm = fit_robust_normalization([])
    assert norm.center == 0.0
    assert norm.scale == 1.0


def test_fit_robust_normalization_single_value_falls_back_scale():
    norm = fit_robust_normalization([5.0])
    assert norm.center == 5.0
    assert norm.scale == 1.0  # MAD of a single point is 0 -> fallback


def test_fit_robust_normalization_typical_spread():
    norm = fit_robust_normalization([1.0, 2.0, 3.0, 4.0, 5.0])
    assert norm.center == 3.0
    assert norm.scale > 0.0


def test_fit_robust_normalization_ignores_none_values():
    norm = fit_robust_normalization([1.0, None, 3.0, None, 5.0])
    assert norm.center == 3.0


# ── 2. zero-pair training is honestly neutral ───────────────────────────────

def test_zero_pairs_yields_zero_coefficients_and_not_converged():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset)
    assert all(value == 0.0 for value in fitted.coefficients.values())
    assert fitted.intercept == 0.0
    assert fitted.converged is False
    assert fitted.n_pairs_used == 0
    assert fitted.final_loss is None


def test_insufficient_evidence_pairs_are_skipped_not_trained_on():
    r1, r2 = _record(1, 8, 1, 2), _record(2, 1, 8, 1)
    pairs = (
        _pair("1:1", "2:1", "insufficient_evidence"),
    )
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=pairs)
    fitted = fit_pairwise_baseline(dataset)
    assert fitted.n_pairs_used == 0
    assert fitted.n_pairs_skipped == 1


def test_tie_pairs_are_used_with_tie_weight_not_skipped():
    r1, r2 = _record(1, 8, 1, 2), _record(2, 1, 8, 1)
    pairs = (_pair("1:1", "2:1", "tie"),)
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=pairs)
    fitted = fit_pairwise_baseline(dataset)
    assert fitted.n_pairs_used == 1
    assert fitted.n_pairs_skipped == 0


def test_unmatched_pair_refs_are_skipped():
    r1 = _record(1, 8, 1, 2)
    pairs = (_pair("1:1", "999:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=(r1,), pair_labels=pairs)
    fitted = fit_pairwise_baseline(dataset)
    assert fitted.n_pairs_used == 0
    assert fitted.n_pairs_skipped == 1


# ── 3. monotonic sign projection ────────────────────────────────────────────

def test_fitted_coefficients_always_respect_declared_direction():
    # Deliberately adversarial/noisy labels: sometimes the lower-kill,
    # higher-death item is preferred anyway (label noise), which would
    # push an unconstrained regression toward a wrong-signed coefficient.
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([
            (8, 1), (1, 8), (7, 2), (2, 7), (6, 3), (3, 6), (5, 4), (4, 5),
        ], start=1)
    )
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "right")  # always prefer the SECOND (noisy/adversarial)
        for a, b in zip(range(1, 9), range(2, 9))
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_baseline(dataset, iterations=200)
    for spec in FEATURE_ALLOWLIST:
        coefficient = fitted.coefficients[spec.name]
        if spec.direction == DIRECTION_POSITIVE:
            assert coefficient >= 0.0, spec.name
        elif spec.direction == DIRECTION_NEGATIVE:
            assert coefficient <= 0.0, spec.name


# ── 4. determinism ───────────────────────────────────────────────────────────

def test_fit_pairwise_baseline_is_deterministic():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([(8, 1), (1, 8), (6, 2), (2, 6)], start=1)
    )
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)

    fitted_a = fit_pairwise_baseline(dataset, iterations=50)
    fitted_b = fit_pairwise_baseline(dataset, iterations=50)
    assert fitted_a.coefficients == fitted_b.coefficients
    assert fitted_a.intercept == fitted_b.intercept


# ── 5. pairwise correction reduces loss on separable data ───────────────────

def test_training_reduces_loss_on_separable_synthetic_data():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate(
            [(9, 0), (0, 9), (8, 1), (1, 8), (7, 1), (1, 7), (8, 0), (0, 8)], start=1,
        )
    )
    # Consistently prefer the higher-kill/lower-death item -- a genuinely
    # learnable, separable signal.
    pairs = tuple(
        _pair(f"{a}:1", f"{b}:1", "left")
        for a, b in [(1, 2), (3, 4), (5, 6), (7, 8)]
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)

    untrained = fit_pairwise_baseline(dataset, iterations=0)
    trained = fit_pairwise_baseline(dataset, iterations=300)

    assert untrained.final_loss is None or untrained.final_loss >= 0.693  # ln(2), chance-level
    assert trained.final_loss is not None
    assert trained.final_loss < 0.4  # meaningfully better than chance
    assert trained.coefficients["raw_kills"] > 0.0
    assert trained.coefficients["raw_deaths"] < 0.0


# ── 6. honest convergence / no trained intercept ────────────────────────────

def test_intercept_is_never_trained_stays_zero():
    records = tuple(
        _record(game_id, kills=k, deaths=d, assists=3)
        for game_id, (k, d) in enumerate([(9, 0), (0, 9), (8, 1), (1, 8)], start=1)
    )
    pairs = (_pair("1:1", "2:1", "left"), _pair("3:1", "4:1", "left"))
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_baseline(dataset, iterations=500)
    assert fitted.intercept == 0.0


def test_converged_true_only_when_a_real_tolerance_is_met():
    # A simple, quickly-separable two-item dataset with an aggressive
    # learning rate should converge (loss delta / gradient norm below
    # tolerance) well before exhausting a large iteration budget.
    records = (
        _record(1, kills=9, deaths=0, assists=0), _record(2, kills=0, deaths=9, assists=0),
    )
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    fitted = fit_pairwise_baseline(
        dataset, iterations=5000, learning_rate=0.2,
        loss_tolerance=1e-6, gradient_tolerance=1e-5,
    )
    assert fitted.converged is True
    assert fitted.iterations_run < 5000


def test_converged_false_when_iteration_budget_exhausted_without_tolerance():
    records = (
        _record(1, kills=9, deaths=0, assists=0), _record(2, kills=0, deaths=9, assists=0),
    )
    pairs = (_pair("1:1", "2:1", "left"),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    # A single iteration with a tiny learning rate cannot possibly reach
    # either tolerance -- must be honestly reported as not converged, not
    # "converged because a pair existed".
    fitted = fit_pairwise_baseline(
        dataset, iterations=1, learning_rate=1e-6,
        loss_tolerance=1e-12, gradient_tolerance=1e-12,
    )
    assert fitted.converged is False
    assert fitted.iterations_run == 1


def test_converged_false_with_zero_usable_pairs_even_with_large_budget():
    records = (_record(1, kills=8, deaths=1, assists=2),)
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    fitted = fit_pairwise_baseline(dataset, iterations=1000)
    assert fitted.converged is False
    assert fitted.iterations_run == 0


# ── 7. single-tier assertion ─────────────────────────────────────────────────

def test_fit_pairwise_baseline_rejects_mixed_tier_dataset():
    r1 = _record(1, 8, 1, 2, evidence_source="match_v5")
    r2 = _record(2, 8, 1, 2, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    with pytest.raises(DatasetValidationError):
        fit_pairwise_baseline(dataset)


# ── 8. abstained records excluded by default ────────────────────────────────

def test_abstained_records_excluded_from_training_by_default():
    normal = _record(1, kills=8, deaths=1, assists=2, abstain=False)
    abstained = _record(2, kills=8, deaths=1, assists=2, abstain=True)
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(normal, abstained), pair_labels=(pair,),
    )
    fitted = fit_pairwise_baseline(dataset)
    assert fitted.n_items == 1  # only the non-abstained record counted
    assert fitted.n_items_excluded_abstain == 1
    assert fitted.n_pairs_used == 0  # the pair references an excluded (abstained) ref
    assert fitted.n_pairs_skipped == 1


def test_include_abstained_overrides_exclusion_explicitly():
    normal = _record(1, kills=8, deaths=1, assists=2, abstain=False)
    abstained = _record(2, kills=1, deaths=8, assists=2, abstain=True)
    pair = _pair("1:1", "2:1", "left")
    dataset = TrainingDataset(
        schema_version=1, feature_records=(normal, abstained), pair_labels=(pair,),
    )
    fitted = fit_pairwise_baseline(dataset, include_abstained=True)
    assert fitted.n_items == 2
    assert fitted.n_items_excluded_abstain == 0
    assert fitted.n_pairs_used == 1
    assert fitted.n_pairs_skipped == 0
