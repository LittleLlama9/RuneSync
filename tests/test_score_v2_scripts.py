"""End-to-end tests for the scripts/score_v2/*.py CLIs.

Builds a small local `HistoryStore` + corpus manifest, then runs
`build_training_dataset.py` -> `train_model.py` -> `evaluate_model.py` as
real subprocesses (not direct function calls), matching how a developer
would actually run this pipeline.

Sections:
  1. Zero pairwise labels (today's real corpus state): every stage
     succeeds and honestly reports "insufficient_data" / null metrics --
     no exception, no fabricated confidence.
  2. With synthetic pairwise labels wired through `corpus.review`'s real
     export path: training reaches "fitted" status and evaluation reports
     real (non-null) metrics.
  3. Malformed/tampered artifact rejection at the `evaluate_model.py` CLI
     boundary.
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from corpus.build_from_history import build_entry_from_history
from corpus.manifest import CorpusManifest, load_or_create_identity_salt
from corpus.review import PairwiseItem, ReviewLabelStore, build_presentation, make_label
from history_store import HistoryStore
from score_features import extract_game_features

REPO_ROOT = Path(__file__).resolve().parent.parent
ROLES = ["top", "jungle", "mid", "bot", "support"]


def _make_report(game_id, local_win, kill_seed):
    players = []
    scores = []
    for pid in range(1, 11):
        team = 100 if pid <= 5 else 200
        role = ROLES[(pid - 1) % 5]
        kills = (kill_seed + pid) % 9
        players.append({
            "participant_id": pid, "puuid": f"puuid-{game_id}-{pid}",
            "summoner_name": f"Player{pid}", "champion_id": 10 + pid,
            "champion_name": f"Champion{pid}", "team_id": team, "role": role,
            "win": local_win if team == 100 else not local_win,
            "kills": kills, "deaths": (pid % 5), "assists": (pid * 2) % 7,
            "gold_earned": 8000 + kills * 400, "cs": 120 + pid * 3,
            "champion_level": 15, "damage_to_champions": 15000,
            "damage_to_objectives": 3000, "damage_to_turrets": 1000,
            "damage_taken": 12000, "damage_mitigated": 8000, "healing": 500,
            "vision_score": 20, "wards_placed": 6, "wards_killed": 1,
            "items": [1001, 1002],
        })
        scores.append({
            "participant_id": pid, "model_version": 1, "total_score": float(101 - pid),
            "match_rank": pid, "components": {"combat": 75.0}, "observations": [],
        })
    return {
        "match": {
            "game_id": game_id, "queue_id": 420, "map_id": 11, "game_mode": "CLASSIC",
            "game_creation": 1_700_000_000 + game_id,
            "game_creation_date": "2026-07-01T00:00:00Z", "duration": 1800, "patch": "14.1",
            "local_participant_id": 1, "local_win": local_win, "local_champion_id": 11,
            "local_champion_name": "Champion1", "local_role": "top", "score_model_version": 1,
        },
        "participants": players, "scores": scores,
    }


def _run(script_name, *args):
    return subprocess.run(
        [sys.executable, f"scripts/score_v2/{script_name}", *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )


@pytest.fixture
def corpus_environment(tmp_path):
    db_path = tmp_path / "history.db"
    store = HistoryStore(db_path)
    game_ids = list(range(910001, 910007))
    for i, game_id in enumerate(game_ids):
        store.save_report(_make_report(game_id, local_win=(i % 2 == 0), kill_seed=i))
        extract_game_features(store, game_id)

    salt = load_or_create_identity_salt(tmp_path / "salt.bin")
    manifest = CorpusManifest()
    for game_id in game_ids:
        manifest.add_entry(
            build_entry_from_history(store, game_id, "aggregate", identity_salt=salt)
        )
    manifest_path = tmp_path / "manifest.json"
    manifest.save(manifest_path)

    return {
        "db_path": db_path, "manifest_path": manifest_path, "game_ids": game_ids,
        "store": store, "tmp_path": tmp_path,
    }


# ── 1. zero-label honest path ───────────────────────────────────────────────

def test_pipeline_with_zero_labels_is_honest(corpus_environment):
    tmp_path = corpus_environment["tmp_path"]
    dataset_path = tmp_path / "dataset.jsonl"

    result = _run(
        "build_training_dataset.py",
        "--history-db", str(corpus_environment["db_path"]),
        "--manifest", str(corpus_environment["manifest_path"]),
        "--split-seed", "test-seed", "--output", str(dataset_path),
    )
    assert result.returncode == 0, result.stderr
    assert dataset_path.exists()

    artifacts_dir = tmp_path / "artifacts"
    result = _run(
        "train_model.py", "--dataset", str(dataset_path),
        "--output-dir", str(artifacts_dir), "--model-version", "0.0.1-dev",
        "--calibration-version", "0.0.1-dev", "--split", "none",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["aggregate"]["status"] == "insufficient_data"
    assert summary["aggregate"]["production_ready"] is False

    report_path = tmp_path / "report.json"
    result = _run(
        "evaluate_model.py", "--dataset", str(dataset_path),
        "--artifacts-dir", str(artifacts_dir), "--split", "none",
        "--report-out", str(report_path),
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    evaluation = report["aggregate"]["evaluation"]
    assert evaluation["n_pairs"] == 0
    assert evaluation["pairwise_accuracy_overall"]["accuracy"] is None
    assert evaluation["spearman"] is None
    assert evaluation["brier"] is None


# ── 2. with real synthetic pairwise labels via corpus.review ───────────────

def test_pipeline_with_synthetic_pairwise_labels_reaches_fitted_status(corpus_environment):
    tmp_path = corpus_environment["tmp_path"]
    store = corpus_environment["store"]
    game_ids = corpus_environment["game_ids"]

    labels_path = tmp_path / "labels.jsonl"
    token_map_path = tmp_path / "token_map.json"
    label_store = ReviewLabelStore(labels_path)
    token_maps = {}

    for game_id in game_ids:
        stored = store.get_feature_set(game_id, evidence_source="aggregate")
        participants = stored["features"]["participants"]
        # Compare participant 1 (highest kills by construction, team 100)
        # against participant 6 (team 200) using the real
        # corpus.review blinding path -- synthetic preference, not a real
        # human reviewer, but exercised through the genuine export path.
        item_a = PairwiseItem(item_ref=f"{game_id}:1", features=participants["1"])
        item_b = PairwiseItem(item_ref=f"{game_id}:6", features=participants["6"])
        presentation, token_map = build_presentation(item_a, item_b, seed="test-seed")
        token_maps[presentation.pair_id] = token_map
        preferred_token = (
            presentation.left_token
            if token_map["left_ref"] == item_a.item_ref else presentation.right_token
        )
        choice = "left" if preferred_token == presentation.left_token else "right"
        label = make_label(
            pair_id=presentation.pair_id, reviewer_id="synthetic-test-reviewer",
            choice=choice, confidence=0.9, rationale_tags=("combat_impact",),
            presentation_seed="test-seed",
        )
        label_store.add_label(label)

    token_map_path.write_text(json.dumps(token_maps), encoding="utf-8")

    dataset_path = tmp_path / "dataset.jsonl"
    result = _run(
        "build_training_dataset.py",
        "--history-db", str(corpus_environment["db_path"]),
        "--manifest", str(corpus_environment["manifest_path"]),
        "--split-seed", "test-seed", "--output", str(dataset_path),
        "--labels", str(labels_path), "--token-map", str(token_map_path),
    )
    assert result.returncode == 0, result.stderr
    dataset_text = dataset_path.read_text(encoding="utf-8")
    assert '"kind": "pair_label"' in dataset_text

    artifacts_dir = tmp_path / "artifacts"
    result = _run(
        "train_model.py", "--dataset", str(dataset_path),
        "--output-dir", str(artifacts_dir), "--model-version", "0.0.1-dev",
        "--calibration-version", "0.0.1-dev", "--split", "none",
        "--min-pairs-for-nontrivial-fit", "3",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["aggregate"]["status"] == "fitted"
    assert summary["aggregate"]["n_pairs_used"] == len(game_ids)
    assert summary["aggregate"]["production_ready"] is False


# ── 3. malformed/tampered artifact rejection ────────────────────────────────

def test_evaluate_model_rejects_tampered_artifact(corpus_environment, tmp_path):
    dataset_path = tmp_path / "dataset.jsonl"
    result = _run(
        "build_training_dataset.py",
        "--history-db", str(corpus_environment["db_path"]),
        "--manifest", str(corpus_environment["manifest_path"]),
        "--split-seed", "test-seed", "--output", str(dataset_path),
    )
    assert result.returncode == 0, result.stderr

    artifacts_dir = tmp_path / "artifacts"
    result = _run(
        "train_model.py", "--dataset", str(dataset_path),
        "--output-dir", str(artifacts_dir), "--model-version", "0.0.1-dev",
        "--calibration-version", "0.0.1-dev", "--split", "none",
    )
    assert result.returncode == 0, result.stderr

    artifact_path = artifacts_dir / "aggregate.json"
    data = json.loads(artifact_path.read_text(encoding="utf-8"))
    data["intercept"] = 12345.0  # tamper without recomputing content_hash
    artifact_path.write_text(json.dumps(data), encoding="utf-8")

    result = _run(
        "evaluate_model.py", "--dataset", str(dataset_path),
        "--artifacts-dir", str(artifacts_dir), "--split", "none",
    )
    assert result.returncode == 0  # script itself still exits cleanly...
    report = json.loads(result.stdout)
    assert "error" in report["aggregate"]  # ...but the tampered tier is rejected
    assert "REJECTED" in result.stderr


def test_evaluate_model_rejects_malformed_json_artifact(corpus_environment, tmp_path):
    dataset_path = tmp_path / "dataset.jsonl"
    result = _run(
        "build_training_dataset.py",
        "--history-db", str(corpus_environment["db_path"]),
        "--manifest", str(corpus_environment["manifest_path"]),
        "--split-seed", "test-seed", "--output", str(dataset_path),
    )
    assert result.returncode == 0, result.stderr

    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    (artifacts_dir / "aggregate.json").write_text("{not valid json", encoding="utf-8")

    result = _run(
        "evaluate_model.py", "--dataset", str(dataset_path),
        "--artifacts-dir", str(artifacts_dir), "--split", "none",
    )
    assert result.returncode == 0
    report = json.loads(result.stdout)
    assert "error" in report["aggregate"]
