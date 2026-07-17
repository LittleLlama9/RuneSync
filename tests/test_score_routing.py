import datetime

import pytest

from performance_score import (
    SCORE_V2_MODEL_VERSION,
    ScoreRouter,
    ScoreRoutingError,
    load_score_v2_artifacts,
)
from score_v2.artifact import FeatureCoefficient, RoleCalibration, build_artifact
from score_v2.feature_spec import feature_contract_for_tier


FIXED_NOW = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)


def _artifact(source="aggregate", production_ready=False):
    coefficients = tuple(
        FeatureCoefficient(
            spec=spec,
            coefficient=0.5 if spec.direction > 0 else -0.5,
            robust_center=0.0,
            robust_scale=1.0,
        )
        for spec in feature_contract_for_tier(source)
    )
    return build_artifact(
        model_version="2.0.0-test",
        feature_version="2.0.0-evidence",
        calibration_version="2.0.0-test",
        evidence_source=source,
        intercept=0.0,
        coefficients=coefficients,
        role_calibration={
            role: RoleCalibration(
                offset=0.0, sample_count=20, shrinkage_weight=0.8,
            )
            for role in ("top", "jungle", "mid", "bot", "support", "unknown")
        },
        score_calibration={
            "midpoint": 50.0, "scale": 5.0,
            "clip_min": 0.0, "clip_max": 100.0,
        },
        confidence_params={
            "missing_feature_penalty": 0.5,
            "evidence_quality_weight": 0.5,
            "interval_min_half_width": 3.0,
            "interval_max_half_width": 40.0,
        },
        abstention_params={
            "short_game_seconds": 600.0,
            "min_present_feature_fraction": 0.3,
            "min_confidence_to_report": 0.15,
        },
        training_metadata={"status": "test"},
        production_ready=production_ready,
        release_notes="routing test artifact",
        now=FIXED_NOW,
    )


def _features(source="aggregate", abstain=False):
    participants = {}
    roles = ("top", "jungle", "mid", "bot", "support") * 2
    for index in range(10):
        participants[str(index + 1)] = {
            "raw": {
                "kills": 10 - index,
                "deaths": index,
                "assists": 5,
            },
            "baseline": {"role": roles[index]},
        }
    return {
        "feature_version": "2.0.0-evidence",
        "evidence_source": source,
        "chosen_source_completeness": 1.0,
        "duration_seconds": 1800.0,
        "abstain": abstain,
        "abstain_reason": "short_game" if abstain else None,
        "participants": participants,
    }


def test_empty_router_preserves_v1_only_state():
    router = ScoreRouter()
    assert router.enabled is False
    assert router.registered_sources == ()
    assert router.select_source(("match_v5", "aggregate")) is None


def test_nonproduction_artifact_requires_explicit_development_opt_in():
    with pytest.raises(ScoreRoutingError, match="not production-ready"):
        ScoreRouter({"aggregate": _artifact()})

    router = ScoreRouter(
        {"aggregate": _artifact()},
        allow_development_artifacts=True,
    )
    assert router.enabled is True


def test_router_selects_strongest_available_exact_tier():
    router = ScoreRouter(
        {
            "aggregate": _artifact("aggregate"),
            "lcu_timeline": _artifact("lcu_timeline"),
        },
        allow_development_artifacts=True,
    )
    assert router.select_source(("aggregate", "lcu_timeline", "match_v5")) == "lcu_timeline"
    assert router.select_source(("match_v5",)) is None


def test_router_source_selection_is_completeness_aware():
    router = ScoreRouter(
        {
            "lcu_timeline": _artifact("lcu_timeline"),
            "live_client": _artifact("live_client"),
        },
        allow_development_artifacts=True,
    )
    assert router.select_source(
        ("lcu_timeline", "live_client"),
        {"lcu_timeline": 0.25, "live_client": 0.9},
    ) == "live_client"


def test_router_builds_persistable_scores_with_confidence_and_provenance():
    artifact = _artifact()
    router = ScoreRouter(
        {"aggregate": artifact},
        allow_development_artifacts=True,
    )
    routed = router.score_feature_set(
        _features(),
        evidence=(
            {"kind": "capability_snapshot"},
            {"kind": "fight", "participant_id": 1, "t_ms": 1000},
            {"kind": "fight", "participant_id": 2, "t_ms": 2000},
        ),
        local_participant_id=1,
    )

    assert routed.model_version == SCORE_V2_MODEL_VERSION
    assert routed.artifact_model_version == artifact.model_version
    assert routed.model_artifact_hash == artifact.content_hash
    assert routed.model_family == "linear"
    assert len(routed.scores) == 10
    assert {score["match_rank"] for score in routed.scores} == set(range(1, 11))
    assert all(score["coaching_eligible"] is False for score in routed.scores)
    participant_one = next(
        score for score in routed.scores if score["participant_id"] == 1
    )
    assert participant_one["participant_confidence"] == 1.0
    assert participant_one["score_low"] <= participant_one["total_score"]
    assert participant_one["score_high"] >= participant_one["total_score"]
    assert {row["kind"] for row in participant_one["evidence"]} == {
        "capability_snapshot", "fight",
    }
    assert participant_one["coaching_eligible"] is False
    assert participant_one["coaching"]["primary_focus"] is None
    assert any(
        "Aggregate evidence" in reason
        for reason in participant_one["coaching"]["withheld_reasons"]
    )
    assert participant_one["observations"][0] == "Post-game totals: 10/0/5."
    assert routed.confidence["production_ready"] is False


def test_abstention_is_preserved_in_routed_score_rows():
    router = ScoreRouter(
        {"aggregate": _artifact()},
        allow_development_artifacts=True,
    )
    routed = router.score_feature_set(_features(abstain=True))
    assert all(score["abstain"] is True for score in routed.scores)
    assert all("short_game" in score["abstain_reasons"] for score in routed.scores)
    assert routed.confidence["abstained_participant_ids"] == list(range(1, 11))


def test_artifact_directory_loader_is_fail_closed(tmp_path):
    assert load_score_v2_artifacts(tmp_path / "missing") == {}

    artifact = _artifact()
    artifact.save(tmp_path / "aggregate.json")
    with pytest.raises(ScoreRoutingError, match="not production-ready"):
        load_score_v2_artifacts(tmp_path)

    loaded = load_score_v2_artifacts(
        tmp_path, require_production_ready=False,
    )
    assert loaded["aggregate"].content_hash == artifact.content_hash
