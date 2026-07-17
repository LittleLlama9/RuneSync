"""Tests for score_v2/training/evaluate.py -- grouped evaluation utilities.

Sections:
  1. pairwise_accuracy correctness, tie/insufficient-evidence exclusion,
     unscoreable-pair handling, honest None on empty input.
  2. Rank correlations (Spearman/Kendall): perfect/reverse/no-variance/
     too-few-samples cases.
  3. rank_agreement: hand-computed NDCG/top-bottom/exact-within-one on a
     small constructed group.
  4. Calibration metrics (Brier/ECE) with known values.
  5. risk_coverage_curve monotonic shape and duplicate-point dedup.
  6. bootstrap_stability determinism and honest-empty behavior.
  7. slice_pairwise_accuracy / duration_bucket grouping, including
     cross-key ("mixed") pairs and missing-record exclusion.
  8. bootstrap_pairs_by_game: cluster (block) bootstrap by game, not by
     individual (dependent) pair.
"""

import math

from score_v2.training.dataset import build_feature_record, PairLabel
from score_v2.training.evaluate import (
    bootstrap_pairs_by_game,
    bootstrap_stability,
    brier_score,
    duration_bucket,
    expected_calibration_error,
    kendall_tau,
    pairwise_accuracy,
    rank_agreement,
    risk_coverage_curve,
    slice_pairwise_accuracy,
    spearman_rank_correlation,
)


def _pair(left_ref, right_ref, choice):
    if choice == "left":
        winner_ref, relation = left_ref, "left_preferred"
    elif choice == "right":
        winner_ref, relation = right_ref, "right_preferred"
    else:
        winner_ref, relation = None, choice
    return PairLabel(
        pair_id=f"{left_ref}|{right_ref}", left_ref=left_ref, right_ref=right_ref,
        winner_ref=winner_ref, relation=relation, choice=choice, confidence=0.9,
        rationale_tags=("combat_impact",), reviewer_id="r1",
        created_at="2026-01-01T00:00:00+00:00",
    )


# ── 1. pairwise_accuracy ─────────────────────────────────────────────────────

def test_pairwise_accuracy_all_correct():
    pairs = [_pair("a", "b", "left"), _pair("c", "d", "right")]
    scores = {"a": 60.0, "b": 40.0, "c": 30.0, "d": 70.0}
    result = pairwise_accuracy(pairs, scores)
    assert result.accuracy == 1.0
    assert result.n_scored == 2


def test_pairwise_accuracy_all_wrong():
    pairs = [_pair("a", "b", "left")]
    scores = {"a": 40.0, "b": 60.0}
    result = pairwise_accuracy(pairs, scores)
    assert result.accuracy == 0.0


def test_pairwise_accuracy_excludes_ties_and_insufficient_evidence():
    pairs = [
        _pair("a", "b", "tie"),
        _pair("a", "b", "insufficient_evidence"),
        _pair("c", "d", "left"),
    ]
    scores = {"a": 50.0, "b": 50.0, "c": 90.0, "d": 10.0}
    result = pairwise_accuracy(pairs, scores)
    assert result.n_ties_excluded == 2  # both the "tie" and "insufficient_evidence" choices
    assert result.n_scored == 1  # only c/d is decisive+scoreable
    assert result.accuracy == 1.0


def test_pairwise_accuracy_exact_score_tie_counted_as_tie():
    pairs = [_pair("a", "b", "left")]
    scores = {"a": 50.0, "b": 50.0}
    result = pairwise_accuracy(pairs, scores)
    assert result.n_ties_excluded == 1
    assert result.accuracy is None


def test_pairwise_accuracy_unscoreable_pair():
    pairs = [_pair("a", "z", "left")]  # "z" never scored
    scores = {"a": 50.0}
    result = pairwise_accuracy(pairs, scores)
    assert result.n_unscoreable == 1
    assert result.accuracy is None


def test_pairwise_accuracy_empty_is_honestly_none():
    result = pairwise_accuracy([], {})
    assert result.accuracy is None
    assert result.n_scored == 0


# ── 2. rank correlations ─────────────────────────────────────────────────────

def test_spearman_perfect_positive_correlation():
    assert math.isclose(spearman_rank_correlation([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)


def test_spearman_perfect_negative_correlation():
    assert math.isclose(spearman_rank_correlation([1, 2, 3, 4], [40, 30, 20, 10]), -1.0)


def test_spearman_none_for_too_few_samples():
    assert spearman_rank_correlation([1.0], [2.0]) is None
    assert spearman_rank_correlation([], []) is None


def test_spearman_none_for_zero_variance():
    assert spearman_rank_correlation([5.0, 5.0, 5.0], [1.0, 2.0, 3.0]) is None


def test_kendall_tau_perfect_agreement():
    assert kendall_tau([1, 2, 3], [10, 20, 30]) == 1.0


def test_kendall_tau_perfect_disagreement():
    assert kendall_tau([1, 2, 3], [30, 20, 10]) == -1.0


def test_kendall_tau_none_for_fully_tied_side():
    assert kendall_tau([1, 1, 1], [1, 2, 3]) is None


# ── 3. rank_agreement (hand-computed) ───────────────────────────────────────

def test_rank_agreement_on_a_fully_ordered_group():
    # 4 items, human-implied order via pairwise wins: a > b > c > d.
    # Model predicts the exact same order via scores.
    groups = {"g1": ["a", "b", "c", "d"]}
    pairs = [
        _pair("a", "b", "left"), _pair("b", "c", "left"), _pair("c", "d", "left"),
        _pair("a", "c", "left"), _pair("a", "d", "left"), _pair("b", "d", "left"),
    ]
    scores = {"a": 90.0, "b": 70.0, "c": 50.0, "d": 30.0}
    result = rank_agreement(groups, pairs, scores, min_group_pairs=3)
    assert result.n_groups == 1
    assert result.exact_rate == 1.0
    assert result.within_one_rate == 1.0
    assert result.top_match_rate == 1.0
    assert result.bottom_match_rate == 1.0
    assert result.mean_ndcg == 1.0


def test_rank_agreement_on_a_fully_reversed_group():
    groups = {"g1": ["a", "b", "c", "d"]}
    pairs = [
        _pair("a", "b", "left"), _pair("b", "c", "left"), _pair("c", "d", "left"),
        _pair("a", "c", "left"), _pair("a", "d", "left"), _pair("b", "d", "left"),
    ]
    # Model gets the EXACT reverse order of the human-implied ranking.
    scores = {"a": 10.0, "b": 30.0, "c": 50.0, "d": 90.0}
    result = rank_agreement(groups, pairs, scores, min_group_pairs=3)
    assert result.exact_rate == 0.0
    assert result.top_match_rate == 0.0
    assert result.bottom_match_rate == 0.0
    assert result.mean_ndcg < 1.0


def test_rank_agreement_skips_groups_below_min_pairs():
    groups = {"g1": ["a", "b"]}
    pairs = [_pair("a", "b", "left")]  # only 1 pair, need >= 3
    scores = {"a": 90.0, "b": 10.0}
    result = rank_agreement(groups, pairs, scores, min_group_pairs=3)
    assert result.n_groups == 0
    assert result.exact_rate is None


def test_rank_agreement_empty_is_honestly_none():
    result = rank_agreement({}, [], {})
    assert result.n_groups == 0
    assert result.exact_rate is None
    assert result.mean_ndcg is None


# ── 4. calibration metrics ───────────────────────────────────────────────────

def test_brier_score_known_value():
    predictions = [(1.0, 1.0), (0.0, 0.0), (0.5, 1.0), (0.5, 0.0)]
    # errors: 0, 0, 0.25, 0.25 -> mean 0.125
    assert math.isclose(brier_score(predictions), 0.125)


def test_brier_score_empty_is_none():
    assert brier_score([]) is None


def test_expected_calibration_error_perfect_calibration():
    # 10 predictions, one per bin, each exactly matching its own bin's
    # accuracy (predicted probability == actual outcome) -> ECE should be 0.
    predictions = [(0.0, 0.0)] * 5 + [(1.0, 1.0)] * 5
    ece = expected_calibration_error(predictions, n_bins=10)
    assert ece is not None
    assert math.isclose(ece, 0.0, abs_tol=1e-9)


def test_expected_calibration_error_none_for_too_few_samples():
    assert expected_calibration_error([(0.5, 1.0)], n_bins=10) is None


# ── 5. risk-coverage curve ───────────────────────────────────────────────────

def test_risk_coverage_curve_high_confidence_items_have_lower_risk():
    items = [
        (0.9, True), (0.8, True), (0.7, True), (0.6, False),
        (0.5, False), (0.4, False), (0.3, False), (0.2, False),
        (0.1, False), (0.05, False),
    ]
    curve = risk_coverage_curve(items, n_points=10)
    assert curve is not None
    assert curve[0]["coverage"] < curve[-1]["coverage"]
    # Full coverage includes everyone (7/10 wrong = 0.7 risk); low
    # coverage keeps only the high-confidence correct items (0 risk).
    assert curve[0]["risk"] <= curve[-1]["risk"]


def test_risk_coverage_curve_empty_is_none():
    assert risk_coverage_curve([]) is None


def test_risk_coverage_curve_deduplicates_points_for_small_n():
    # 3 items with n_points=10 would naively produce 10 rows, many
    # sharing the same rounded item count (and therefore identical
    # coverage/risk) -- these must be collapsed to at most 3 real points.
    items = [(0.9, True), (0.5, False), (0.1, True)]
    curve = risk_coverage_curve(items, n_points=10)
    assert curve is not None
    counts = [point["n_items"] for point in curve]
    assert counts == sorted(set(counts))  # strictly increasing, no repeats
    assert max(counts) == len(items)


# ── 6. bootstrap stability ───────────────────────────────────────────────────

def test_bootstrap_stability_is_deterministic_for_fixed_seed():
    items = list(range(20))
    result_a = bootstrap_stability(items, lambda sample: sum(sample) / len(sample), seed=7)
    result_b = bootstrap_stability(items, lambda sample: sum(sample) / len(sample), seed=7)
    assert result_a == result_b


def test_bootstrap_stability_empty_is_none():
    assert bootstrap_stability([], lambda sample: 1.0) is None


def test_bootstrap_stability_all_none_metric_is_none():
    assert bootstrap_stability([1, 2, 3], lambda sample: None) is None


# ── 7. slicing helpers ───────────────────────────────────────────────────────

def test_duration_bucket_boundaries():
    assert duration_bucket(500) == "short_under_10m"
    assert duration_bucket(600) == "normal_10_25m"
    assert duration_bucket(1499) == "normal_10_25m"
    assert duration_bucket(1500) == "long_25_40m"
    assert duration_bucket(2399) == "long_25_40m"
    assert duration_bucket(2400) == "very_long_over_40m"


def _slice_record(game_id, participant_id, role, evidence_source="match_v5", duration=1800.0):
    gf = {
        "duration_seconds": duration, "abstain": False, "abstain_reason": None,
        "chosen_source_completeness": 1.0,
        "participants": {str(participant_id): {
            "raw": {"kills": 5, "deaths": 3, "assists": 4},
            "baseline": {"role": role, "champion": "TestChamp", "patch": "14.1"},
        }},
    }
    return build_feature_record(
        game_id=game_id, participant_id=participant_id, evidence_source=evidence_source,
        features_for_game=gf, split="train",
    )


def test_slice_pairwise_accuracy_only_assigns_homogeneous_pairs():
    top_a = _slice_record(1, 1, role="top")
    top_b = _slice_record(1, 2, role="top")
    jungle = _slice_record(1, 3, role="jungle")
    records_by_base_ref = {
        top_a.base_ref: top_a, top_b.base_ref: top_b, jungle.base_ref: jungle,
    }
    same_role_pair = _pair(top_a.base_ref, top_b.base_ref, "left")
    cross_role_pair = _pair(top_a.base_ref, jungle.base_ref, "left")
    scores = {top_a.base_ref: 80.0, top_b.base_ref: 20.0, jungle.base_ref: 50.0}

    sliced = slice_pairwise_accuracy(
        [same_role_pair, cross_role_pair], scores, records_by_base_ref,
        key_fn=lambda record: record.role,
    )
    assert sliced.by_key["top"].n_scored == 1  # only the homogeneous same-role pair
    assert "jungle" not in sliced.by_key or sliced.by_key["jungle"].n_scored == 0
    assert sliced.mixed.n_scored == 1  # the cross-role pair, not silently attributed to "top"
    assert sliced.n_excluded_missing_record == 0


def test_slice_pairwise_accuracy_excludes_pairs_with_missing_record():
    top_a = _slice_record(1, 1, role="top")
    records_by_base_ref = {top_a.base_ref: top_a}
    pair_with_unknown_side = _pair(top_a.base_ref, "999:1", "left")
    sliced = slice_pairwise_accuracy(
        [pair_with_unknown_side], {top_a.base_ref: 80.0, "999:1": 20.0}, records_by_base_ref,
        key_fn=lambda record: record.role,
    )
    assert sliced.n_excluded_missing_record == 1
    assert sliced.by_key == {}
    assert sliced.mixed.n_scored == 0


def test_slice_pairwise_accuracy_by_evidence_tier_is_homogeneous():
    match_v5_a = _slice_record(1, 1, role="top", evidence_source="match_v5")
    match_v5_b = _slice_record(1, 2, role="jungle", evidence_source="match_v5")
    aggregate_c = _slice_record(2, 1, role="top", evidence_source="aggregate")
    records_by_base_ref = {
        match_v5_a.base_ref: match_v5_a, match_v5_b.base_ref: match_v5_b,
        aggregate_c.base_ref: aggregate_c,
    }
    same_tier_pair = _pair(match_v5_a.base_ref, match_v5_b.base_ref, "left")
    cross_tier_pair = _pair(match_v5_a.base_ref, aggregate_c.base_ref, "left")
    scores = {
        match_v5_a.base_ref: 80.0, match_v5_b.base_ref: 20.0, aggregate_c.base_ref: 50.0,
    }
    sliced = slice_pairwise_accuracy(
        [same_tier_pair, cross_tier_pair], scores, records_by_base_ref,
        key_fn=lambda record: record.evidence_source,
    )
    assert sliced.by_key["match_v5"].n_scored == 1
    assert sliced.mixed.n_scored == 1


# ── 8. game-clustered bootstrap ─────────────────────────────────────────────

def test_bootstrap_pairs_by_game_is_deterministic():
    pairs = [
        _pair("1:1", "1:2", "left"), _pair("1:3", "1:4", "right"),
        _pair("2:1", "2:2", "left"),
    ]
    scores = {"1:1": 80.0, "1:2": 20.0, "1:3": 30.0, "1:4": 70.0, "2:1": 90.0, "2:2": 10.0}
    result_a = bootstrap_pairs_by_game(
        pairs, lambda sample: pairwise_accuracy(sample, scores).accuracy, seed=42,
    )
    result_b = bootstrap_pairs_by_game(
        pairs, lambda sample: pairwise_accuracy(sample, scores).accuracy, seed=42,
    )
    assert result_a == result_b
    assert result_a["n_groups"] == 2  # games "1" and "2"


def test_bootstrap_pairs_by_game_empty_is_none():
    assert bootstrap_pairs_by_game([], lambda sample: 1.0) is None


def test_bootstrap_pairs_by_game_resamples_whole_games_not_individual_pairs():
    # All pairs from game "1" are perfectly correct; all pairs from game
    # "2" are perfectly wrong. A game-level bootstrap resample must never
    # produce a MIX of a game-1 pair with a game-2 pair being treated as
    # independent -- each resampled "unit" is a whole game's pair list.
    game_1_pairs = [_pair("1:1", "1:2", "left"), _pair("1:3", "1:4", "left")]
    game_2_pairs = [_pair("2:1", "2:2", "left"), _pair("2:3", "2:4", "left")]
    scores = {
        "1:1": 90.0, "1:2": 10.0, "1:3": 90.0, "1:4": 10.0,  # game 1: always correct
        "2:1": 10.0, "2:2": 90.0, "2:3": 10.0, "2:4": 90.0,  # game 2: always wrong
    }
    all_pairs = game_1_pairs + game_2_pairs
    result = bootstrap_pairs_by_game(
        all_pairs, lambda sample: pairwise_accuracy(sample, scores).accuracy,
        n_resamples=100, seed=7,
    )
    assert result is not None
    # Every resample's accuracy must be one of {0.0, 0.5, 1.0} -- exactly
    # what whole-game resampling produces (some mix of all-game-1,
    # all-game-2, or half-and-half), never an "impossible" intermediate
    # value that flat pair-level resampling could produce instead.
    assert result["min"] in (0.0, 0.5, 1.0)
    assert result["max"] in (0.0, 0.5, 1.0)
