import datetime
import json
import subprocess
import sys
from pathlib import Path

import pytest

from history_store import HistoryStore
from performance_score import score_match
from score_features import FEATURE_VERSION
from score_v2.artifact import FeatureCoefficient, RoleCalibration, build_artifact
from score_v2.feature_spec import feature_contract_for_tier
from score_v2.shadow import ShadowReportError, build_shadow_report


ROOT = Path(__file__).parent.parent
FIXED_NOW = datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc)


def _players():
    roles = ("top", "jungle", "mid", "bot", "support") * 2
    players = []
    for index in range(10):
        participant_id = index + 1
        players.append({
            "participant_id": participant_id,
            "puuid": f"puuid-{participant_id}",
            "summoner_name": f"Player {participant_id}",
            "champion_id": 100 + participant_id,
            "champion_name": (
                "Winner" if participant_id == 1
                else "Loser" if participant_id == 2
                else f"Champion {participant_id}"
            ),
            "team_id": 100 if participant_id <= 5 else 200,
            "role": roles[index],
            "win": participant_id <= 5,
            "kills": 12 - index,
            "deaths": index,
            "assists": 5,
            "gold_earned": 10000,
            "cs": 180,
            "champion_level": 16,
            "damage_to_champions": 20000,
            "damage_to_objectives": 3000,
            "damage_to_turrets": 1200,
            "damage_taken": 15000,
            "damage_mitigated": 8000,
            "healing": 500,
            "vision_score": 20,
            "wards_placed": 8,
            "wards_killed": 2,
            "items": [1054, 6664],
        })
    return players


def _save_game(store, game_id=123, duration=1800):
    players = _players()
    store.save_report({
        "match": {
            "game_id": game_id,
            "queue_id": 420,
            "map_id": 11,
            "game_mode": "CLASSIC",
            "game_creation": 1000000 + game_id,
            "game_creation_date": "2026-07-18T00:00:00Z",
            "duration": duration,
            "patch": "16.13.1",
            "local_participant_id": 1,
            "local_win": True,
            "local_champion_id": 101,
            "local_champion_name": "Winner",
            "local_role": "top",
            "score_model_version": 1,
        },
        "participants": players,
        "scores": score_match(players, duration),
    })


def _features(duration=1800, abstain=False):
    participants = {}
    roles = ("top", "jungle", "mid", "bot", "support") * 2
    for index in range(10):
        participants[str(index + 1)] = {
            "raw": {
                "kills": 12 - index,
                "deaths": index,
                "assists": 5,
            },
            "baseline": {"role": roles[index]},
        }
    return {
        "feature_version": FEATURE_VERSION,
        "evidence_source": "aggregate",
        "chosen_source_completeness": 1.0,
        "duration_seconds": float(duration),
        "abstain": abstain,
        "abstain_reason": "short_game" if abstain else None,
        "participants": participants,
    }


def _artifact(training_status="test"):
    coefficients = tuple(
        FeatureCoefficient(
            spec=spec,
            coefficient=0.4 * spec.direction,
            robust_center=0.0,
            robust_scale=1.0,
        )
        for spec in feature_contract_for_tier("aggregate")
    )
    return build_artifact(
        model_version="2.0.0-shadow-test",
        feature_version=FEATURE_VERSION,
        calibration_version="2.0.0-shadow-test",
        evidence_source="aggregate",
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
        training_metadata={"status": training_status},
        production_ready=False,
        release_notes="shadow test artifact",
        now=FIXED_NOW,
    )


def test_shadow_compares_without_saving_or_activating_score_runs(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    _save_game(store)
    store.save_feature_set(
        123, FEATURE_VERSION, "aggregate", _features(),
    )
    active_before = store.get_match(123)["active_score_run_id"]
    run_count_before = len(store.list_score_runs(123))

    report = build_shadow_report(
        store,
        {"aggregate": _artifact()},
        allow_development_artifacts=True,
        generated_at=FIXED_NOW,
        adversarial_cases=({
            "case_id": "winner-over-loser",
            "game_id": 123,
            "expectation": {
                "type": "pairwise_minimum_gap",
                "winner": "Winner",
                "loser": "Loser",
                "min_gap": 0.0,
            },
        },),
    )

    assert report["mode"] == "shadow_comparison"
    assert report["summary"]["status_counts"] == {"scored": 1}
    assert report["summary"]["participants_compared"] == 10
    assert report["safety"]["saved_score_runs"] is False
    assert report["safety"]["release_eligible"] is False
    assert report["games"][0]["active_score_run_unchanged"] is True
    assert report["adversarial_cases"][0]["passed"] is True
    assert all(
        "summoner_name" not in row
        for row in report["games"][0]["participants"]
    )
    assert store.get_match(123)["active_score_run_id"] == active_before
    assert len(store.list_score_runs(123)) == run_count_before


def test_shadow_inventory_can_backfill_features_without_artifacts(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    _save_game(store)

    report = build_shadow_report(
        store, backfill_features=True, generated_at=FIXED_NOW,
    )

    assert report["mode"] == "evidence_inventory"
    assert report["summary"]["status_counts"] == {
        "evidence_ready_no_artifact": 1,
    }
    assert report["games"][0]["source"] == "aggregate"
    assert store.get_feature_set(
        123, feature_version=FEATURE_VERSION, evidence_source="aggregate",
    ) is not None
    assert len(store.list_score_runs(123)) == 1


def test_shadow_rejects_neutral_insufficient_data_artifacts(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    _save_game(store)
    store.save_feature_set(
        123, FEATURE_VERSION, "aggregate", _features(),
    )

    with pytest.raises(ShadowReportError, match="insufficient-data"):
        build_shadow_report(
            store,
            {"aggregate": _artifact("insufficient_data")},
            allow_development_artifacts=True,
        )


def test_shadow_reports_short_game_adversarial_abstention(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    _save_game(store, duration=510)
    store.save_feature_set(
        123, FEATURE_VERSION, "aggregate",
        _features(duration=510, abstain=True),
    )

    report = build_shadow_report(
        store,
        {"aggregate": _artifact()},
        allow_development_artifacts=True,
        generated_at=FIXED_NOW,
        adversarial_cases=({
            "case_id": "short-game",
            "game_id": 123,
            "expectation": {"type": "insufficient_evidence"},
        },),
    )

    assert report["summary"]["nonabstained_coverage"] == 0.0
    assert report["adversarial_cases"][0]["passed"] is True


def test_shadow_cli_writes_inventory_report(tmp_path):
    db_path = tmp_path / "history.db"
    store = HistoryStore(db_path)
    _save_game(store)
    output = tmp_path / "inventory.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "score_v2" / "run_shadow.py"),
            "--history-db", str(db_path),
            "--output", str(output),
            "--backfill-features",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["mode"] == "evidence_inventory"
    assert payload["safety"]["changed_active_score_runs"] is False


def test_shadow_cli_rejects_explicit_empty_artifact_directory(tmp_path):
    db_path = tmp_path / "history.db"
    store = HistoryStore(db_path)
    _save_game(store)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "score_v2" / "run_shadow.py"),
            "--history-db", str(db_path),
            "--output", str(tmp_path / "report.json"),
            "--artifacts-dir", str(artifacts),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "No valid exact-tier artifacts" in completed.stderr


def test_shadow_cli_reports_artifact_routing_errors_without_traceback(tmp_path):
    db_path = tmp_path / "history.db"
    store = HistoryStore(db_path)
    _save_game(store)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _artifact().save(artifacts / "aggregate.json")

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "score_v2" / "run_shadow.py"),
            "--history-db", str(db_path),
            "--output", str(tmp_path / "report.json"),
            "--artifacts-dir", str(artifacts),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stderr.startswith("FAILED: ")
    assert "not production-ready" in completed.stderr
    assert "Traceback" not in completed.stderr
