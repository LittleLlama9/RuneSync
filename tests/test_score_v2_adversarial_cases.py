"""DAEMON Score v2 pipeline against the two verified adversarial cases.

These are the same two `verified_local` cases in
`corpus/data/adversarial_cases.json` that `test_adversarial_cases.py`
already checks structurally (Sion 8:30 short-game abstention, and the
K'Sante/Seraphine/Vel'Koz compound ranking regression). This file checks
them against the actual `score_v2` runtime/training pipeline, using the
same real sanitized fixtures `test_score_features.py` uses -- and is
deliberately honest about the difference between what the pipeline CAN do
given supervision and what today's real, unlabeled local corpus actually
supports:

  * `test_sion_short_game_triggers_runtime_abstention` needs no training
    signal at all -- `score_features.py`'s own short-game abstention
    flows straight through `score_v2.runtime`, so it is checked against
    an artifact with zero fitted signal (exactly today's honest state).
  * `test_ksante_ranks_above_seraphine_and_velkoz_with_synthetic_supervision`
    demonstrates the pipeline CAN resolve the regression once real
    supervision exists, using a handful of hand-built, clearly-labeled
    SYNTHETIC pairwise preferences (not real reviewer data) fed through
    the genuine `corpus.review` blinding/export path.
  * `test_ksante_case_is_not_resolved_by_todays_unlabeled_corpus` trains
    the exact same real feature data with ZERO pairwise labels (today's
    actual corpus, since bulk Match-V5/review labeling remains blocked --
    see the vault decision gating final Score v2 validation on Match-V5
    authorization) and asserts the honest result: every score is tied at
    the neutral midpoint. The compound case's `min_gap=0.0` expectation
    is tie-tolerant and technically still "passes" in this state, but the
    test explicitly asserts the gap is exactly zero so nobody mistakes
    this for real discrimination -- this is a vacuous pass, not a
    genuine resolution, and the test says so.
"""

import copy
import json
from pathlib import Path

import score_features as sf
from corpus.adversarial_cases import evaluate_case, load_library
from score_v2.runtime import score_participant
from score_v2.training.dataset import PairLabel, TrainingDataset, build_feature_record
from score_v2.training.export import train_tier

FIXTURES = Path(__file__).parent / "fixtures"

KSANTE_GAME_ID = 5602827182
KSANTE_DURATION = 1861
SION_GAME_ID = 5601631110
SION_DURATION = 510

KSANTE_PID = 8
VELKOZ_PID = 9
SERAPHINE_PID = 10


def _load(name):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


def _ksante_participants():
    return copy.deepcopy(_load("aggregate_participants_5602827182.json"))["participants"]


def _ksante_timeline():
    fixture = _load("lcu_timeline_5602827182.json")
    return {"frames": fixture["frames"]}


def _sion_participants():
    return copy.deepcopy(_load("aggregate_participants_5601631110.json"))["participants"]


def _ksante_features():
    caps = sf.EvidenceCapabilities(lcu_timeline=True, lcu_timeline_completeness=1.0)
    features, _ = sf.compute_feature_set(
        _ksante_participants(), KSANTE_DURATION, caps, sf.LCU_TIMELINE,
        timeline=_ksante_timeline(),
    )
    return features


def _sion_features():
    caps = sf.EvidenceCapabilities()  # only aggregate
    features, _ = sf.compute_feature_set(
        _sion_participants(), SION_DURATION, caps, sf.AGGREGATE,
    )
    return features


def _feature_records(features_for_game, evidence_source, split="train"):
    return tuple(
        build_feature_record(
            game_id=KSANTE_GAME_ID, participant_id=int(pid), evidence_source=evidence_source,
            features_for_game=features_for_game, split=split,
        )
        for pid in features_for_game["participants"]
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
        winner_ref=winner_ref, relation=relation,
        choice=choice, confidence=confidence, rationale_tags=("combat_impact",),
        reviewer_id="synthetic-test-reviewer", created_at="2026-01-01T00:00:00+00:00",
    )


def _load_ksante_case():
    cases = load_library()
    return next(
        case for case in cases
        if case.case_id == "verified-5602827182-ksante-seraphine-velkoz"
    )


def _load_sion_case():
    cases = load_library()
    return next(
        case for case in cases
        if case.case_id == "verified-5601631110-short-game-insufficient-evidence"
    )


# ── Sion (game 5601631110): short-game abstention, no training needed ──────

def test_sion_short_game_triggers_runtime_abstention():
    features = _sion_features()
    assert features["abstain"] is True  # score_features.py's own signal

    records = _feature_records(features, "aggregate")
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())
    # Zero supervision needed for this case -- abstention is driven by
    # game_features["abstain"], not by the fitted coefficients.
    result = train_tier(
        dataset, "aggregate", model_version="0.0.1-dev", feature_version=sf.FEATURE_VERSION,
        calibration_version="0.0.1-dev",
    )

    local_pid = next(
        int(pid) for pid, block in features["participants"].items()
        if block["baseline"]["champion"] == "Sion"
    )
    score_result = score_participant(result.artifact, features, local_pid)
    assert score_result.abstain is True
    assert "short_game" in score_result.abstain_reasons

    case = _load_sion_case()
    case_result = evaluate_case(case, duration_seconds=SION_DURATION)
    assert case_result.passed is True


# ── K'Sante/Seraphine/Vel'Koz (game 5602827182): compound ranking case ──────

def test_ksante_ranks_above_seraphine_and_velkoz_with_synthetic_supervision():
    features = _ksante_features()
    records = _feature_records(features, "lcu_timeline")

    ksante_ref = f"{KSANTE_GAME_ID}:{KSANTE_PID}"
    velkoz_ref = f"{KSANTE_GAME_ID}:{VELKOZ_PID}"
    seraphine_ref = f"{KSANTE_GAME_ID}:{SERAPHINE_PID}"

    # Hand-built SYNTHETIC preferences (not real reviewer data) matching
    # this case's known expectation, run through this test only -- a
    # minimal amount of supervision to demonstrate the pipeline resolves
    # the case once supervision exists.
    pairs = (
        _pair(ksante_ref, seraphine_ref, "left"),
        _pair(ksante_ref, velkoz_ref, "left"),
    )
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=pairs)
    result = train_tier(
        dataset, "lcu_timeline", model_version="0.0.1-dev", feature_version=sf.FEATURE_VERSION,
        calibration_version="0.0.1-dev", min_pairs_for_nontrivial_fit=2,
    )
    assert result.status == "fitted"

    scores = {
        int(pid): score_participant(result.artifact, features, int(pid)).score
        for pid in features["participants"]
    }
    champion_scores = {
        "K'Sante": scores[KSANTE_PID], "Seraphine": scores[SERAPHINE_PID],
        "Vel'Koz": scores[VELKOZ_PID],
    }

    case = _load_ksante_case()
    case_result = evaluate_case(case, scores=champion_scores)
    assert case_result.passed is True
    for sub_result in case_result.sub_results:
        assert sub_result.passed is True

    # Genuine (non-vacuous) resolution: a real, strictly positive gap,
    # not merely "not worse".
    assert champion_scores["K'Sante"] > champion_scores["Seraphine"]
    assert champion_scores["K'Sante"] > champion_scores["Vel'Koz"]


def test_ksante_case_is_not_resolved_by_todays_unlabeled_corpus():
    """Honest limitation: with the real corpus's current ZERO pairwise
    labels (bulk Match-V5/review labeling is still blocked -- see the
    vault decision gating final Score v2 validation on Match-V5
    authorization), the fitted coefficients are the neutral L2 prior, so
    every participant lands on the exact same midpoint score. The
    compound case's `min_gap=0.0` expectation is tie-tolerant and still
    reports `passed=True`, but that is a vacuous pass (zero real
    discrimination) -- NOT a resolution of the regression, and this test
    asserts the zero-gap directly so that distinction cannot be missed.
    """
    features = _ksante_features()
    records = _feature_records(features, "lcu_timeline")
    dataset = TrainingDataset(schema_version=1, feature_records=records, pair_labels=())

    result = train_tier(
        dataset, "lcu_timeline", model_version="0.0.1-dev", feature_version=sf.FEATURE_VERSION,
        calibration_version="0.0.1-dev",
    )
    assert result.status == "insufficient_data"

    scores = {
        int(pid): score_participant(result.artifact, features, int(pid)).score
        for pid in features["participants"]
    }
    assert scores[KSANTE_PID] == scores[SERAPHINE_PID] == scores[VELKOZ_PID] == 50.0

    case = _load_ksante_case()
    champion_scores = {
        "K'Sante": scores[KSANTE_PID], "Seraphine": scores[SERAPHINE_PID],
        "Vel'Koz": scores[VELKOZ_PID],
    }
    case_result = evaluate_case(case, scores=champion_scores)
    # Vacuous pass -- see the docstring above. Explicitly NOT claimed as a
    # genuine resolution.
    assert case_result.passed is True
    assert champion_scores["K'Sante"] - champion_scores["Seraphine"] == 0.0
    assert champion_scores["K'Sante"] - champion_scores["Vel'Koz"] == 0.0
