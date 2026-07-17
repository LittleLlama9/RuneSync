"""Tests for score_v2/runtime.py -- the dependency-free scorer.

Sections:
  1. Tier routing (`select_artifact`): exact-key lookup only, no implicit
     substitution across tiers, mismatched evidence_source rejected.
  2. Participant/order invariance: identical result regardless of dict
     insertion order in `game_features["participants"]`.
  3. Missing-feature confidence reduction and interval widening.
  4. Abstention: short game, insufficient features, low confidence.
  5. Uncertainty shrinkage toward the semantic midpoint (50).
  6. Tampered-artifact rejection (re-verified at score time too).
  7. `score_participant` enforces `game_features["evidence_source"] ==
     artifact.evidence_source`.
  8. `score_game`'s genuine group-level `rank_confidence` (score
     gaps/interval overlap), distinct from per-participant `confidence`.
"""

import dataclasses
import datetime

import pytest

from score_v2.artifact import FeatureCoefficient, RoleCalibration, build_artifact
from score_v2.feature_spec import feature_contract_for_tier
from score_v2.runtime import (
    ArtifactUnavailableError,
    EvidenceTierMismatchError,
    score_game,
    score_participant,
    select_artifact,
)

FIXED_NOW = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)


def _coefficients(evidence_source="match_v5", magnitude=0.4):
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


def _artifact(evidence_source="match_v5", **overrides):
    kwargs = dict(
        model_version="0.0.1-dev", feature_version="2.0.0-evidence",
        calibration_version="0.0.1-dev", evidence_source=evidence_source,
        intercept=0.0, coefficients=_coefficients(evidence_source),
        role_calibration={
            "mid": RoleCalibration(offset=0.0, sample_count=10, shrinkage_weight=0.5),
            "unknown": RoleCalibration(offset=0.0, sample_count=0, shrinkage_weight=0.0),
        },
        score_calibration={"midpoint": 50.0, "scale": 5.0, "clip_min": 0.0, "clip_max": 100.0},
        confidence_params={
            "missing_feature_penalty": 0.5, "evidence_quality_weight": 0.5,
            "interval_min_half_width": 3.0, "interval_max_half_width": 40.0,
        },
        abstention_params={
            "short_game_seconds": 600.0, "min_present_feature_fraction": 0.3,
            "min_confidence_to_report": 0.15,
        },
        training_metadata={"n_pairs_used": 40}, evaluation_metadata=None,
        production_ready=False, release_notes="dev artifact for tests", now=FIXED_NOW,
    )
    kwargs.update(overrides)
    return build_artifact(**kwargs)


def _full_block(role="mid", kills=6, deaths=3):
    return {
        "raw": {"kills": kills, "deaths": deaths, "assists": 5},
        "fight_influence": {
            "kill_events": kills, "death_events": deaths, "assist_events": 5,
            "first_blood": True, "untraded_deaths": 1, "event_kill_participation": 0.55,
        },
        "objective_participation": {
            "epic_monster_secures": 1, "epic_monster_assists": 0, "grub_secures": 1,
            "grub_assists": 0, "objective_fight_involvements": 2, "turret_kills": 2,
            "turret_assists": 1, "turret_plates": 2, "inhibitor_kills": 0,
        },
        "structure_pressure": {"structure_secures": 2},
        "enablement_suppression": {"ally_enablement_assists": 1, "suppression_weight": 2.0},
        "vision_influence": {"available": False, "reason": "no ward events"},
        "death_tempo": {"death_count": deaths, "rapid_death_pairs": 0},
        "resource_conversion": {
            "available": True, "lane_opponent": 2, "lead_windows": 3,
            "converted_lead_windows": 2, "conversion_rate": 0.67,
        },
        "live_state": {"available": False, "reason": "no live snapshots"},
        "baseline": {"role": role, "champion": "TestChamp", "patch": "14.1"},
    }


def _game_features(
        participants, *, evidence_source="match_v5", abstain=False, abstain_reason=None,
        completeness=1.0):
    return {
        "evidence_source": evidence_source, "abstain": abstain, "abstain_reason": abstain_reason,
        "chosen_source_completeness": completeness, "participants": participants,
    }


# ── 1. tier routing ──────────────────────────────────────────────────────────

def test_select_artifact_exact_match():
    artifact = _artifact("match_v5")
    result = select_artifact({"match_v5": artifact}, "match_v5")
    assert result is artifact


def test_select_artifact_missing_tier_raises():
    with pytest.raises(ArtifactUnavailableError):
        select_artifact({"match_v5": _artifact("match_v5")}, "lcu_timeline")


def test_select_artifact_never_substitutes_a_different_tier():
    # Even though a match_v5 artifact exists, requesting aggregate must
    # not silently receive it.
    artifacts = {"match_v5": _artifact("match_v5")}
    with pytest.raises(ArtifactUnavailableError):
        select_artifact(artifacts, "aggregate")


def test_select_artifact_rejects_mismatched_registration():
    # Registered under "aggregate" but the artifact itself declares
    # "match_v5" -- must be refused rather than trusted.
    mismatched = _artifact("match_v5")
    with pytest.raises(ArtifactUnavailableError):
        select_artifact({"aggregate": mismatched}, "aggregate")


# ── 2. participant / order invariance ───────────────────────────────────────

def test_score_participant_is_invariant_to_dict_insertion_order():
    artifact = _artifact()
    block_1, block_2 = _full_block(kills=6), _full_block(kills=2, deaths=6)

    game_a = _game_features({"1": block_1, "2": block_2})
    game_b = _game_features({"2": block_2, "1": block_1})  # reversed insertion order

    result_a = score_participant(artifact, game_a, 1)
    result_b = score_participant(artifact, game_b, 1)
    assert result_a.to_dict() == result_b.to_dict()


def test_score_game_scores_every_participant_independently():
    artifact = _artifact()
    participants = {"1": _full_block(kills=8), "2": _full_block(kills=1, deaths=7)}
    game = _game_features(participants)
    results = score_game({"match_v5": artifact}, game)
    assert set(results) == {1, 2}
    assert results[1].result.score > results[2].result.score  # more kills, fewer deaths


# ── 3. missing-feature confidence reduction ─────────────────────────────────

def test_missing_features_reduce_confidence_and_widen_interval():
    artifact = _artifact()
    full_game = _game_features({"1": _full_block()})
    sparse_block = {"raw": {"kills": 6, "deaths": 3, "assists": 5}}
    sparse_game = _game_features({"1": sparse_block})

    full_result = score_participant(artifact, full_game, 1)
    sparse_result = score_participant(artifact, sparse_game, 1)

    assert sparse_result.present_feature_count < full_result.present_feature_count
    assert sparse_result.confidence < full_result.confidence
    full_width = full_result.score_interval[1] - full_result.score_interval[0]
    sparse_width = sparse_result.score_interval[1] - sparse_result.score_interval[0]
    assert sparse_width > full_width


def test_missing_feature_names_are_reported():
    artifact = _artifact()
    block = _full_block()
    del block["fight_influence"]
    game = _game_features({"1": block})
    result = score_participant(artifact, game, 1)
    assert "fight_kill_events" in result.missing_features
    assert "fight_death_events" in result.missing_features


# ── 4. abstention ────────────────────────────────────────────────────────────

def test_short_game_abstention_propagates_from_game_features():
    artifact = _artifact()
    game = _game_features({"1": _full_block()}, abstain=True, abstain_reason="short_game")
    result = score_participant(artifact, game, 1)
    assert result.abstain is True
    assert "short_game" in result.abstain_reasons


def test_insufficient_features_triggers_abstention():
    artifact = _artifact()
    # Only raw.kills present -- well under the 0.3 min_present_feature_fraction.
    game = _game_features({"1": {"raw": {"kills": 6}}})
    result = score_participant(artifact, game, 1)
    assert result.abstain is True
    assert "insufficient_features" in result.abstain_reasons


def test_low_confidence_triggers_abstention_via_low_evidence_quality():
    artifact = _artifact(confidence_params={
        "missing_feature_penalty": 0.5, "evidence_quality_weight": 0.95,
        "interval_min_half_width": 3.0, "interval_max_half_width": 40.0,
    })
    game = _game_features({"1": _full_block()}, completeness=0.01)
    result = score_participant(artifact, game, 1)
    assert result.confidence < 0.15
    assert "low_confidence" in result.abstain_reasons


def test_fully_supported_participant_does_not_abstain():
    artifact = _artifact()
    game = _game_features({"1": _full_block()})
    result = score_participant(artifact, game, 1)
    assert result.abstain is False
    assert result.abstain_reasons == ()


def test_unknown_participant_id_raises():
    artifact = _artifact()
    game = _game_features({"1": _full_block()})
    with pytest.raises(ValueError):
        score_participant(artifact, game, 999)


# ── 5. uncertainty shrinkage toward 50 ──────────────────────────────────────

def test_lower_confidence_shrinks_score_closer_to_midpoint():
    artifact = _artifact()
    high_quality_game = _game_features({"1": _full_block(kills=10, deaths=0)}, completeness=1.0)
    low_quality_game = _game_features({"1": _full_block(kills=10, deaths=0)}, completeness=0.2)

    high_result = score_participant(artifact, high_quality_game, 1)
    low_result = score_participant(artifact, low_quality_game, 1)

    assert low_result.confidence < high_result.confidence
    assert abs(low_result.score - 50.0) < abs(high_result.score - 50.0)


def test_score_interval_is_within_clip_bounds():
    artifact = _artifact()
    game = _game_features({"1": _full_block(kills=10, deaths=0)})
    result = score_participant(artifact, game, 1)
    low, high = result.score_interval
    assert 0.0 <= low <= result.score <= high <= 100.0


# ── 6. tamper rejection at score time ───────────────────────────────────────

def test_score_participant_reverifies_artifact_hash():
    artifact = _artifact()
    tampered = dataclasses.replace(artifact, intercept=999.0)  # hash now stale
    game = _game_features({"1": _full_block()})
    with pytest.raises(Exception):
        score_participant(tampered, game, 1)


# ── 7. evidence-tier enforcement ─────────────────────────────────────────────

def test_score_participant_rejects_mismatched_evidence_source():
    artifact = _artifact("match_v5")
    game = _game_features({"1": _full_block()}, evidence_source="aggregate")
    with pytest.raises(EvidenceTierMismatchError):
        score_participant(artifact, game, 1)


def test_score_participant_rejects_missing_evidence_source():
    artifact = _artifact("match_v5")
    game = _game_features({"1": _full_block()})
    del game["evidence_source"]
    with pytest.raises(EvidenceTierMismatchError):
        score_participant(artifact, game, 1)


def test_score_participant_accepts_matching_evidence_source():
    artifact = _artifact("aggregate")
    game = _game_features({"1": _full_block()}, evidence_source="aggregate")
    result = score_participant(artifact, game, 1)
    assert result.evidence_source == "aggregate"


# ── 8. score_game rank / rank_confidence ─────────────────────────────────────

def test_score_game_assigns_ranks_by_score_descending():
    artifact = _artifact()
    participants = {
        "1": _full_block(kills=10, deaths=0),
        "2": _full_block(kills=5, deaths=5),
        "3": _full_block(kills=0, deaths=10),
    }
    game = _game_features(participants)
    ranked = score_game({"match_v5": artifact}, game)
    ranks_by_pid = {pid: ranked[pid].rank for pid in ranked}
    scores_by_pid = {pid: ranked[pid].result.score for pid in ranked}
    ordered_by_rank = sorted(ranks_by_pid, key=lambda pid: ranks_by_pid[pid])
    ordered_by_score = sorted(scores_by_pid, key=lambda pid: -scores_by_pid[pid])
    assert ordered_by_rank == ordered_by_score
    assert sorted(ranks_by_pid.values()) == [1, 2, 3]


def test_score_game_solo_participant_has_full_rank_confidence():
    artifact = _artifact()
    game = _game_features({"1": _full_block()})
    ranked = score_game({"match_v5": artifact}, game)
    assert ranked[1].rank_confidence == 1.0


def test_score_game_widely_separated_scores_have_high_rank_confidence():
    artifact = _artifact()
    participants = {
        "1": _full_block(kills=10, deaths=0),
        "2": _full_block(kills=0, deaths=10),
    }
    game = _game_features(participants)
    ranked = score_game({"match_v5": artifact}, game)
    assert ranked[1].rank_confidence > 0.5
    assert ranked[2].rank_confidence > 0.5


def test_score_game_nearly_identical_scores_have_low_rank_confidence():
    artifact = _artifact()
    participants = {
        "1": _full_block(kills=5, deaths=3),
        "2": _full_block(kills=5, deaths=3),
    }
    game = _game_features(participants)
    ranked = score_game({"match_v5": artifact}, game)
    assert ranked[1].rank_confidence < 0.2
    assert ranked[2].rank_confidence < 0.2


def test_rank_confidence_is_distinct_from_participant_confidence():
    """Two participants can share the same per-participant `confidence`
    (same evidence completeness) while having very different
    `rank_confidence` (depends on the OTHER participant's score too)."""
    artifact = _artifact()
    participants = {
        "1": _full_block(kills=10, deaths=0),
        "2": _full_block(kills=0, deaths=10),
        "3": _full_block(kills=10, deaths=0),  # same profile as participant 1
    }
    game = _game_features(participants)
    ranked = score_game({"match_v5": artifact}, game)
    # 1 and 3 have identical evidence -> identical `confidence`.
    assert ranked[1].result.confidence == ranked[3].result.confidence
    # But their rank_confidence differs from a widely-separated neighbor:
    # 1 and 3 are adjacent (tied) in the sorted order, so their
    # rank_confidence against each other is low.
    assert ranked[1].rank_confidence < 0.2
    assert ranked[3].rank_confidence < 0.2


def test_ranked_score_result_to_dict_includes_rank_fields():
    artifact = _artifact()
    game = _game_features({"1": _full_block()})
    ranked = score_game({"match_v5": artifact}, game)
    payload = ranked[1].to_dict()
    assert payload["rank"] == 1
    assert "rank_confidence" in payload
    assert "score" in payload
    assert "rank_confidence" not in ranked[1].result.to_dict()  # not on ScoreResult itself
