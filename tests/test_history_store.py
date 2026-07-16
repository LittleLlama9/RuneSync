import json
import sqlite3
from pathlib import Path

import pytest

import history_store
from history_store import HistoryStore, SCHEMA_VERSION


def _report(game_id=123, local_win=True):
    players = []
    scores = []
    for participant_id in range(1, 11):
        local = participant_id == 1
        players.append({
            "participant_id": participant_id,
            "puuid": f"puuid-{participant_id}",
            "summoner_name": f"Player {participant_id}",
            "champion_id": 10 + participant_id,
            "champion_name": "Sion" if local else f"Champion {participant_id}",
            "team_id": 100 if participant_id <= 5 else 200,
            "role": "mid" if local else "top",
            "win": local_win if participant_id <= 5 else not local_win,
            "kills": participant_id,
            "deaths": 2,
            "assists": 3,
            "gold_earned": 10000,
            "cs": 180,
            "champion_level": 16,
            "damage_to_champions": 20000,
            "damage_to_objectives": 5000,
            "damage_to_turrets": 2000,
            "damage_taken": 15000,
            "damage_mitigated": 10000,
            "healing": 1000,
            "vision_score": 20,
            "wards_placed": 8,
            "wards_killed": 2,
            "items": [1054, 6664],
        })
        scores.append({
            "participant_id": participant_id,
            "model_version": 1,
            "total_score": float(101 - participant_id),
            "match_rank": participant_id,
            "components": {"combat": 75.0},
            "observations": ["Combat was the strongest component."],
        })
    return {
        "match": {
            "game_id": game_id,
            "queue_id": 420,
            "map_id": 11,
            "game_mode": "CLASSIC",
            "game_creation": 1000000 + game_id,
            "game_creation_date": "2026-07-14T00:00:00Z",
            "duration": 1800,
            "patch": "16.13.1",
            "local_participant_id": 1,
            "local_win": local_win,
            "local_champion_id": 14,
            "local_champion_name": "Sion",
            "local_role": "mid",
            "score_model_version": 1,
        },
        "participants": players,
        "scores": scores,
    }


def _create_v1_database(path: Path) -> None:
    report = _report()
    match = report["match"]
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE matches (
                game_id INTEGER PRIMARY KEY,
                queue_id INTEGER NOT NULL,
                map_id INTEGER NOT NULL,
                game_mode TEXT NOT NULL,
                game_creation INTEGER NOT NULL,
                game_creation_date TEXT NOT NULL,
                duration INTEGER NOT NULL,
                patch TEXT NOT NULL,
                local_participant_id INTEGER NOT NULL,
                local_win INTEGER NOT NULL,
                local_champion_id INTEGER NOT NULL,
                local_champion_name TEXT NOT NULL,
                local_role TEXT NOT NULL,
                score_model_version INTEGER NOT NULL,
                imported_at TEXT NOT NULL
            );
            CREATE TABLE participants (
                game_id INTEGER NOT NULL REFERENCES matches(game_id) ON DELETE CASCADE,
                participant_id INTEGER NOT NULL,
                puuid TEXT NOT NULL,
                summoner_name TEXT NOT NULL,
                champion_id INTEGER NOT NULL,
                champion_name TEXT NOT NULL,
                team_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                win INTEGER NOT NULL,
                kills INTEGER NOT NULL,
                deaths INTEGER NOT NULL,
                assists INTEGER NOT NULL,
                gold_earned INTEGER NOT NULL,
                cs INTEGER NOT NULL,
                champion_level INTEGER NOT NULL,
                damage_to_champions INTEGER NOT NULL,
                damage_to_objectives INTEGER NOT NULL,
                damage_to_turrets INTEGER NOT NULL,
                damage_taken INTEGER NOT NULL,
                damage_mitigated INTEGER NOT NULL,
                healing INTEGER NOT NULL,
                vision_score INTEGER NOT NULL,
                wards_placed INTEGER NOT NULL,
                wards_killed INTEGER NOT NULL,
                items_json TEXT NOT NULL,
                PRIMARY KEY (game_id, participant_id)
            );
            CREATE TABLE scores (
                game_id INTEGER NOT NULL,
                participant_id INTEGER NOT NULL,
                model_version INTEGER NOT NULL,
                total_score REAL NOT NULL,
                match_rank INTEGER NOT NULL,
                components_json TEXT NOT NULL,
                observations_json TEXT NOT NULL,
                PRIMARY KEY (game_id, participant_id),
                FOREIGN KEY (game_id, participant_id)
                    REFERENCES participants(game_id, participant_id)
                    ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', '1')"
        )
        conn.execute(
            """
            INSERT INTO matches VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                match["game_id"], match["queue_id"], match["map_id"],
                match["game_mode"], match["game_creation"],
                match["game_creation_date"], match["duration"], match["patch"],
                match["local_participant_id"], int(match["local_win"]),
                match["local_champion_id"], match["local_champion_name"],
                match["local_role"], match["score_model_version"],
                "2026-07-16T00:00:00+00:00",
            ),
        )
        for player in report["participants"]:
            conn.execute(
                """
                INSERT INTO participants VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    match["game_id"], player["participant_id"], player["puuid"],
                    player["summoner_name"], player["champion_id"],
                    player["champion_name"], player["team_id"], player["role"],
                    int(player["win"]), player["kills"], player["deaths"],
                    player["assists"], player["gold_earned"], player["cs"],
                    player["champion_level"], player["damage_to_champions"],
                    player["damage_to_objectives"], player["damage_to_turrets"],
                    player["damage_taken"], player["damage_mitigated"],
                    player["healing"], player["vision_score"],
                    player["wards_placed"], player["wards_killed"],
                    json.dumps(player["items"]),
                ),
            )
        for score in report["scores"]:
            conn.execute(
                "INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    match["game_id"], score["participant_id"],
                    score["model_version"], score["total_score"],
                    score["match_rank"], json.dumps(score["components"]),
                    json.dumps(score["observations"]),
                ),
            )


def test_store_round_trip_and_schema(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())

    assert store.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert store.has_game(123)
    report = store.get_report(123)
    assert report["match"]["local_champion_name"] == "Sion"
    assert len(report["participants"]) == 10
    local = next(
        player for player in report["participants"]
        if player["participant_id"] == report["match"]["local_participant_id"]
    )
    assert local["match_rank"] == 1
    assert local["items"] == [1054, 6664]
    runs = store.list_score_runs(123)
    assert len(runs) == 1
    assert runs[0]["is_active"] is True
    assert runs[0]["evidence_source"] == "aggregate_legacy"


def test_save_report_is_idempotent(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)
    report["participants"][0]["kills"] = 99
    store.save_report(report)

    saved = store.get_report(123)
    local = next(p for p in saved["participants"] if p["participant_id"] == 1)
    assert local["kills"] == 99
    assert len(saved["participants"]) == 10
    assert len(store.list_score_runs(123)) == 2


def test_score_runs_are_immutable_and_can_switch_active_version(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)
    v1_run = store.list_score_runs(123)[0]["id"]
    scores = []
    for score in report["scores"]:
        scores.append({
            **score,
            "model_version": 2,
            "total_score": score["total_score"] - 10,
            "score_low": score["total_score"] - 12,
            "score_high": score["total_score"] - 8,
            "rank_confidence": 0.8,
            "coaching_eligible": score["participant_id"] == 1,
            "evidence": [{"kind": "fight", "time": 900}],
        })

    v2_run = store.save_score_run(
        123,
        scores,
        model_version=2,
        feature_version="timeline-v1",
        evidence_source="match_v5",
        calibration_version="cal-v1",
        model_artifact_hash="artifact-hash",
        input_hash="input-hash",
        confidence={"evidence_quality": "full"},
    )

    assert v2_run != v1_run
    runs = store.list_score_runs(123)
    assert {run["id"] for run in runs} == {v1_run, v2_run}
    assert next(run for run in runs if run["id"] == v2_run)["is_active"] is True
    assert next(run for run in runs if run["id"] == v1_run)["is_active"] is False
    saved = store.get_report(123)
    local = next(p for p in saved["participants"] if p["participant_id"] == 1)
    assert local["model_version"] == 2
    assert local["evidence_source"] == "match_v5"
    assert local["total_score"] == 90.0
    assert local["score_low"] == 88.0
    assert local["rank_confidence"] == 0.8
    assert local["coaching_eligible"] is True
    assert local["evidence"] == [{"kind": "fight", "time": 900}]


def test_duplicate_score_run_reuses_same_immutable_record(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)

    first = store.save_score_run(
        123, report["scores"], 2, "aggregate-v2", "aggregate",
        input_hash="same-input",
    )
    second = store.save_score_run(
        123, report["scores"], 2, "aggregate-v2", "aggregate",
        input_hash="same-input",
    )

    assert first == second
    assert len(store.list_score_runs(123)) == 2


def test_recalibrated_score_run_is_a_distinct_record(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)

    first = store.save_score_run(
        123, report["scores"], 2, "timeline-v1", "match_v5",
        calibration_version="cal-1", model_artifact_hash="artifact-1",
        input_hash="same-input",
    )
    second = store.save_score_run(
        123, report["scores"], 2, "timeline-v1", "match_v5",
        calibration_version="cal-2", model_artifact_hash="artifact-2",
        input_hash="same-input",
    )

    assert first != second
    assert len(store.list_score_runs(123)) == 3


def test_incomplete_score_run_is_rejected_without_hiding_match(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)

    with pytest.raises(ValueError, match="missing"):
        store.save_score_run(
            123, report["scores"][:-1], 2, "timeline-v1", "match_v5",
        )

    assert len(store.list_history()) == 1
    assert len(store.list_score_runs(123)) == 1


def test_active_score_run_cannot_be_deleted_directly(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())
    run_id = store.list_score_runs(123)[0]["id"]

    with sqlite3.connect(store.path) as conn:
        with pytest.raises(sqlite3.IntegrityError, match="active score run"):
            conn.execute("DELETE FROM score_runs WHERE id = ?", (run_id,))


def test_timeline_payload_round_trip_is_compressed_and_deduplicated(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())
    payload = {
        "metadata": {"matchId": "NA1_123"},
        "info": {"frames": [{"timestamp": 60000, "events": []}]},
    }

    first = store.save_timeline_payload(123, "match_v5", payload)
    second = store.save_timeline_payload(123, "match_v5", payload)

    assert first == second
    saved = store.get_timeline_payload(123, "match_v5")
    assert saved["payload"] == payload
    assert saved["completeness"] == 1.0
    with sqlite3.connect(store.path) as conn:
        compressed_size = conn.execute(
            "SELECT length(payload_zlib) FROM timeline_payloads WHERE id = ?",
            (first,),
        ).fetchone()[0]
    assert compressed_size < len(json.dumps(payload))


def test_feature_sets_are_content_addressed(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())
    features = {"participants": {"1": {"combat_influence": 1.2}}}

    first = store.save_feature_set(
        123, "features-v1", "match_v5", features,
        evidence=[{"time": 600, "kind": "fight"}],
    )
    second = store.save_feature_set(
        123, "features-v1", "match_v5", features,
        evidence=[{"time": 900, "kind": "objective"}],
    )

    assert first == second


def test_report_orders_each_team_like_league_scoreboard(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    shuffled_roles = ["support", "mid", "top", "bot", "jungle"]
    for index, player in enumerate(report["participants"]):
        player["role"] = shuffled_roles[index % 5]
    store.save_report(report)

    saved = store.get_report(123)
    assert [player["role"] for player in saved["participants"][:5]] == [
        "top", "jungle", "mid", "bot", "support",
    ]
    assert [player["role"] for player in saved["participants"][5:]] == [
        "top", "jungle", "mid", "bot", "support",
    ]


def test_summary_and_history_queries(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report(123, True))
    store.save_report(_report(124, False))

    summary = store.get_summary()
    assert summary["overall"] == {"games": 2, "wins": 1, "win_rate": 50.0}
    assert summary["recent20"]["win_rate"] == 50.0
    assert summary["champions"][0]["name"] == "Sion"
    assert summary["roles"][0]["name"] == "mid"
    assert summary["performance"] == {
        "average_score": 100.0,
        "best_rank": 1,
        "average_rank": 1.0,
    }

    rows = store.list_history()
    assert [row["game_id"] for row in rows] == [124, 123]
    assert rows[0]["total_score"] == 100.0


def test_history_path_falls_back_when_appdata_is_unwritable(monkeypatch, tmp_path):
    original_mkdir = Path.mkdir
    appdata = tmp_path / "blocked"
    fallback = tmp_path / "fallback"
    calls = {"n": 0}

    def flaky_mkdir(path, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("blocked")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(history_store.tempfile, "gettempdir", lambda: str(fallback))
    monkeypatch.setattr(Path, "mkdir", flaky_mkdir)

    assert history_store.default_history_path() == fallback / "RuneSync" / "history.db"


def test_schema_upgrade_backs_up_and_migrates_legacy_scores(tmp_path):
    path = tmp_path / "history.db"
    _create_v1_database(path)

    migrated = HistoryStore(path)

    assert migrated.get_meta("schema_version") == str(SCHEMA_VERSION)
    assert (tmp_path / "history.db.schema-v1.bak").exists()
    with sqlite3.connect(path) as conn:
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(matches)")
        }
    assert "active_score_run_id" in columns
    runs = migrated.list_score_runs(123)
    assert len(runs) == 1
    assert runs[0]["evidence_source"] == "aggregate_legacy"
    assert runs[0]["is_active"] is True
