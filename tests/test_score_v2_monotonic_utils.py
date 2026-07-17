"""Tests for score_v2/training/monotonic_utils.py -- shared pairwise-fit
preparation utilities used by every model family (linear reuses none of
this directly, since it predates this module, but GAM/boosting/tree all
depend on it).

Sections:
  1. isotonic_projection (PAVA) correctness for both directions and the
     unconstrained/degenerate cases.
  2. prepare_pairwise_data: single-tier enforcement, abstain exclusion,
     missing-feature-is-None semantics.
  3. prepare_pairwise_eval_data: the leakage-safety guarantee -- reuses
     already-fit normalization, never derives new statistics from the
     split being prepared, even when that split's own distribution would
     produce very different center/scale.
  4. pairwise_target_and_weight / binary_cross_entropy sanity.
"""

import pytest

from score_v2.feature_spec import DIRECTION_NEGATIVE, DIRECTION_POSITIVE, FEATURE_ALLOWLIST
from score_v2.training.baseline import RobustNormalization, fit_robust_normalization
from score_v2.training.dataset import DatasetValidationError, PairLabel, TrainingDataset, build_feature_record
from score_v2.training.monotonic_utils import (
    binary_cross_entropy,
    isotonic_projection,
    pairwise_target_and_weight,
    prepare_pairwise_data,
    prepare_pairwise_eval_data,
)


def _gf(kills, deaths, assists, split_marker=None, abstain=False):
    return {
        "duration_seconds": 1800.0, "abstain": abstain, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {"1": {
            "raw": {"kills": kills, "deaths": deaths, "assists": assists},
            "baseline": {"role": "mid", "champion": "TestChamp", "patch": "14.1"},
        }},
    }


def _record(game_id, kills, deaths, assists=3, split="train", evidence_source="match_v5", abstain=False):
    return build_feature_record(
        game_id=game_id, participant_id=1, evidence_source=evidence_source,
        features_for_game=_gf(kills, deaths, assists, abstain=abstain), split=split,
    )


def _pair(left_ref, right_ref, choice="left", confidence=0.9):
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


# ── 1. isotonic_projection (PAVA) ────────────────────────────────────────────

def test_isotonic_projection_non_decreasing_merges_violators():
    assert isotonic_projection([1, 3, 2], 1) == [1.0, 2.5, 2.5]


def test_isotonic_projection_non_increasing_via_negate_trick():
    assert isotonic_projection([3, 1, 2], -1) == [3.0, 1.5, 1.5]


def test_isotonic_projection_already_monotonic_is_unchanged():
    assert isotonic_projection([1, 2, 3], 1) == [1.0, 2.0, 3.0]
    assert isotonic_projection([3, 2, 1], -1) == [3.0, 2.0, 1.0]


def test_isotonic_projection_unconstrained_direction_returns_input_unchanged():
    assert isotonic_projection([3, 1, 2], 0) == [3, 1, 2]


def test_isotonic_projection_single_value_or_empty_is_a_noop():
    assert isotonic_projection([5.0], 1) == [5.0]
    assert isotonic_projection([], 1) == []


def test_isotonic_projection_result_is_always_monotonic_for_random_input():
    import random
    rng = random.Random(99)
    for _ in range(20):
        values = [rng.uniform(-10, 10) for _ in range(8)]
        up = isotonic_projection(values, 1)
        down = isotonic_projection(values, -1)
        assert all(up[i] <= up[i + 1] + 1e-9 for i in range(len(up) - 1))
        assert all(down[i] >= down[i + 1] - 1e-9 for i in range(len(down) - 1))


# ── 2. prepare_pairwise_data ─────────────────────────────────────────────────

def test_prepare_pairwise_data_rejects_mixed_tier_dataset():
    r1 = _record(1, 8, 1, evidence_source="match_v5")
    r2 = _record(2, 8, 1, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    with pytest.raises(DatasetValidationError):
        prepare_pairwise_data(dataset, specs=FEATURE_ALLOWLIST)


def test_prepare_pairwise_data_excludes_abstained_by_default():
    normal = _record(1, 8, 1, abstain=False)
    abstained = _record(2, 8, 1, abstain=True)
    dataset = TrainingDataset(schema_version=1, feature_records=(normal, abstained), pair_labels=())
    prepared = prepare_pairwise_data(dataset, specs=FEATURE_ALLOWLIST)
    assert prepared.n_items == 1
    assert prepared.n_items_excluded_abstain == 1
    assert "2:1" not in prepared.normalized_by_ref


def test_prepare_pairwise_data_include_abstained_overrides():
    normal = _record(1, 8, 1, abstain=False)
    abstained = _record(2, 8, 1, abstain=True)
    dataset = TrainingDataset(schema_version=1, feature_records=(normal, abstained), pair_labels=())
    prepared = prepare_pairwise_data(dataset, specs=FEATURE_ALLOWLIST, include_abstained=True)
    assert prepared.n_items == 2
    assert prepared.n_items_excluded_abstain == 0


def test_prepare_pairwise_data_missing_feature_is_none_not_a_guess():
    # aggregate contract only reads raw/{kills,deaths,assists} -- a
    # match_v5-only feature like fight_kill_events is structurally absent.
    record = _record(1, 8, 1, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(record,), pair_labels=())
    prepared = prepare_pairwise_data(dataset, specs=FEATURE_ALLOWLIST)
    assert prepared.normalized_by_ref["1:1"]["fight_kill_events"] is None
    assert prepared.normalized_by_ref["1:1"]["raw_kills"] is not None


def test_prepare_pairwise_data_skips_insufficient_evidence_and_unmatched_refs():
    r1, r2 = _record(1, 8, 1), _record(2, 1, 8)
    pairs = (
        _pair("1:1", "2:1", "insufficient_evidence"),
        _pair("1:1", "999:1", "left"),
    )
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=pairs)
    prepared = prepare_pairwise_data(dataset, specs=FEATURE_ALLOWLIST)
    assert prepared.usable_pairs == ()
    assert prepared.n_pairs_skipped == 2


# ── 3. prepare_pairwise_eval_data: no validation/test leakage ───────────────

def test_prepare_pairwise_eval_data_reuses_train_normalization_not_its_own():
    train_records = tuple(_record(i, kills=k, deaths=1) for i, k in enumerate([1, 2, 3], start=1))
    train_dataset = TrainingDataset(schema_version=1, feature_records=train_records, pair_labels=())
    train_prepared = prepare_pairwise_data(train_dataset, specs=FEATURE_ALLOWLIST)
    train_kills_normalization = train_prepared.normalizations["raw_kills"]

    # A validation split with a WILDLY different kills distribution --
    # if eval prep fit its own statistics, center/scale would differ
    # sharply from the train-fit ones.
    validation_records = tuple(
        _record(100 + i, kills=k, deaths=1, split="validation") for i, k in enumerate([50, 60, 70], start=1)
    )
    validation_dataset = TrainingDataset(
        schema_version=1, feature_records=validation_records, pair_labels=(),
    )
    eval_prepared = prepare_pairwise_eval_data(
        validation_dataset, specs=FEATURE_ALLOWLIST, normalizations=train_prepared.normalizations,
    )
    assert eval_prepared.normalizations["raw_kills"] == train_kills_normalization
    assert eval_prepared.normalizations["raw_kills"] is train_kills_normalization


def test_prepare_pairwise_eval_data_rejects_mixed_tier_dataset():
    r1 = _record(1, 8, 1, evidence_source="match_v5")
    r2 = _record(2, 8, 1, evidence_source="aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=(r1, r2), pair_labels=())
    normalizations = {
        spec.name: RobustNormalization(center=0.0, scale=1.0) for spec in FEATURE_ALLOWLIST
    }
    with pytest.raises(DatasetValidationError):
        prepare_pairwise_eval_data(dataset, specs=FEATURE_ALLOWLIST, normalizations=normalizations)


# ── 4. pairwise_target_and_weight / binary_cross_entropy ───────────────────

def test_pairwise_target_and_weight_left_right_tie():
    assert pairwise_target_and_weight("left", 0.8, tie_weight=0.3) == (1.0, 0.8)
    assert pairwise_target_and_weight("right", 0.8, tie_weight=0.3) == (0.0, 0.8)
    assert pairwise_target_and_weight("tie", 0.8, tie_weight=0.3) == (0.5, 0.3)


def test_pairwise_target_and_weight_clamps_confidence():
    assert pairwise_target_and_weight("left", 1.5, tie_weight=0.3) == (1.0, 1.0)
    assert pairwise_target_and_weight("left", -0.5, tie_weight=0.3) == (1.0, 0.0)


def test_pairwise_target_and_weight_rejects_unexpected_choice():
    with pytest.raises(ValueError):
        pairwise_target_and_weight("insufficient_evidence", 0.9, tie_weight=0.3)


def test_binary_cross_entropy_perfect_prediction_near_zero():
    assert binary_cross_entropy(0.999999, 1.0) < 1e-3
    assert binary_cross_entropy(0.000001, 0.0) < 1e-3


def test_binary_cross_entropy_worst_prediction_is_large():
    assert binary_cross_entropy(0.000001, 1.0) > 10.0
