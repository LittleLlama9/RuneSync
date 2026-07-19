import json
import sqlite3
import datetime
import threading
import time
from contextlib import contextmanager
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


def test_participant_puuids_returns_stored_identities(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())

    puuids = store.participant_puuids(123)

    assert puuids == {f"puuid-{i}" for i in range(1, 11)}
    assert store.participant_puuids(999) == set()


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


def test_score_v2_provenance_confidence_and_abstention_round_trip(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)
    scores = [
        {
            **score,
            "model_version": 2,
            "participant_confidence": 0.7,
            "rank_confidence": 0.6,
            "abstain": score["participant_id"] == 1,
            "abstain_reasons": (
                ["short_game"] if score["participant_id"] == 1 else []
            ),
            "coaching": (
                {
                    "eligible": True,
                    "primary_focus": "Reduce untraded deaths",
                    "challenges": [{"target_successes": 3, "window_games": 5}],
                    "recurring_patterns": [],
                    "withheld_reasons": [],
                }
                if score["participant_id"] == 1 else {}
            ),
        }
        for score in report["scores"]
    ]

    store.save_score_run(
        123, scores, 2, "2.0.0-evidence", "aggregate",
        calibration_version="2.0.0-test",
        model_artifact_hash="artifact-hash",
        artifact_model_version="2.0.0-test",
        model_family="linear",
        confidence={"production_ready": False},
    )

    saved = store.get_report(123)
    local = next(row for row in saved["participants"] if row["participant_id"] == 1)
    assert local["artifact_model_version"] == "2.0.0-test"
    assert local["model_family"] == "linear"
    assert local["participant_confidence"] == 0.7
    assert local["abstain"] is True
    assert local["abstain_reasons"] == ["short_game"]
    assert local["coaching"]["primary_focus"] == "Reduce untraded deaths"
    history = store.list_history()
    assert history[0]["participant_confidence"] == 0.7
    assert history[0]["abstain"] is True
    assert history[0]["abstain_reasons"] == ["short_game"]
    assert history[0]["coaching"]["challenges"][0]["target_successes"] == 3
    assert history[0]["team_best_other_rank"] == 2
    assert history[0]["team_worst_other_rank"] == 5


def test_explicit_score_run_report_does_not_change_active_run(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)
    v1_run = store.list_score_runs(123)[0]["id"]
    v2_run = store.save_score_run(
        123, report["scores"], 2, "features-v2", "aggregate",
        model_artifact_hash="artifact", input_hash="input",
    )

    saved = store.get_score_run_report(123, v1_run)

    assert saved["participants"][0]["model_version"] == 1
    assert store.get_match(123)["active_score_run_id"] == v2_run


def test_score_run_activation_never_downgrades_evidence_tier(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)
    lcu_run = store.save_score_run(
        123, report["scores"], 2, "features-v2", "lcu_timeline",
        model_artifact_hash="lcu-artifact", input_hash="lcu-input",
        activate=False,
    )
    assert store.activate_score_run_if_preferred(lcu_run) is True

    aggregate_run = store.save_score_run(
        123, report["scores"], 2, "features-v2", "aggregate",
        model_artifact_hash="aggregate-artifact", input_hash="aggregate-input",
        activate=False,
    )
    assert store.activate_score_run_if_preferred(aggregate_run) is False
    runs = store.list_score_runs(123)
    assert next(run for run in runs if run["id"] == lcu_run)["is_active"] is True
    assert next(run for run in runs if run["id"] == aggregate_run)["is_active"] is False


def test_concurrent_activation_cannot_demote_stronger_evidence(tmp_path):
    class DelayedSelectConnection:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, sql, parameters=()):
            result = self._conn.execute(sql, parameters)
            if "SELECT active_score_run_id FROM matches" in sql:
                delay = 0.02 if threading.current_thread().name == "strong" else 0.08
                time.sleep(delay)
            return result

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class DelayedSelectStore(HistoryStore):
        @contextmanager
        def _connect(self):
            with super()._connect() as conn:
                yield DelayedSelectConnection(conn)

    store = DelayedSelectStore(tmp_path / "history.db")
    report = _report()
    store.save_report(report)
    strong_run = store.save_score_run(
        123, report["scores"], 2, "features-v2", "match_v5",
        model_artifact_hash="match-v5-artifact", input_hash="match-v5-input",
        activate=False,
    )
    weak_run = store.save_score_run(
        123, report["scores"], 2, "features-v2", "live_client",
        model_artifact_hash="live-artifact", input_hash="live-input",
        activate=False,
    )

    errors = []

    def activate(run_id):
        try:
            store.activate_score_run_if_preferred(run_id)
        except Exception as exc:
            errors.append(exc)

    strong = threading.Thread(
        name="strong", target=activate, args=(strong_run,),
    )
    weak = threading.Thread(
        name="weak", target=activate, args=(weak_run,),
    )
    strong.start()
    time.sleep(0.01)
    weak.start()
    strong.join()
    weak.join()

    assert errors == []
    runs = store.list_score_runs(123)
    assert next(run for run in runs if run["id"] == strong_run)["is_active"] is True
    assert next(run for run in runs if run["id"] == weak_run)["is_active"] is False


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
    assert store.game_ids_missing_timeline("match_v5") == []
    assert store.game_ids_missing_timeline("lcu_timeline") == [123]
    with sqlite3.connect(store.path) as conn:
        compressed_size = conn.execute(
            "SELECT length(payload_zlib) FROM timeline_payloads WHERE id = ?",
            (first,),
        ).fetchone()[0]
    assert compressed_size < len(json.dumps(payload))


def test_timeline_fetch_backoff_does_not_starve_older_games(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    for game_id in (121, 122, 123):
        report = _report()
        report["match"]["game_id"] = game_id
        report["match"]["game_creation"] = game_id
        for participant in report["participants"]:
            participant["game_id"] = game_id
        store.save_report(report)
    now = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)

    store.record_timeline_fetch_failure(
        123, "lcu_timeline", "not_found", now=now,
    )

    assert store.game_ids_missing_timeline(
        "lcu_timeline", limit=1, now=now,
    ) == [122]
    assert store.game_ids_missing_timeline(
        "lcu_timeline", limit=3,
        now=now + datetime.timedelta(days=8),
    ) == [122, 121, 123]


def test_timeline_fetch_due_respects_cache_and_backoff(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())
    now = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)

    assert store.timeline_fetch_due(123, "match_v5", now=now) is True

    store.record_timeline_fetch_failure(123, "match_v5", "auth_rejected", now=now)
    assert store.timeline_fetch_due(123, "match_v5", now=now) is False
    assert store.timeline_fetch_due(
        123, "match_v5", now=now + datetime.timedelta(minutes=1),
    ) is False
    assert store.timeline_fetch_due(
        123, "match_v5", now=now + datetime.timedelta(minutes=6),
    ) is True


def test_timeline_fetch_due_is_false_once_payload_is_cached(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())

    store.save_timeline_payload(123, "match_v5", {"info": {"frames": []}})

    assert store.timeline_fetch_due(123, "match_v5") is False


def test_successful_timeline_clears_fetch_backoff(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())
    now = datetime.datetime(2026, 7, 17, tzinfo=datetime.timezone.utc)
    store.record_timeline_fetch_failure(
        123, "lcu_timeline", "unavailable", now=now,
    )

    store.save_timeline_payload(123, "lcu_timeline", {"frames": []})
    store.clear_timeline_fetch_failure(123, "lcu_timeline")

    with sqlite3.connect(store.path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM timeline_fetch_attempts"
        ).fetchone()[0] == 0


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


def test_get_feature_set_reads_back_newest_matching_row(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(_report())
    older = {"participants": {"1": {"combat_influence": 1.0}}}
    newer = {"participants": {"1": {"combat_influence": 2.0}}}

    store.save_feature_set(123, "features-v1", "aggregate", older)
    store.save_feature_set(123, "features-v1", "match_v5", newer)

    assert store.get_feature_set(123, evidence_source="aggregate")["features"] == older
    assert store.get_feature_set(123, evidence_source="match_v5")["features"] == newer
    assert store.get_feature_set(999) is None
    assert store.get_feature_set(123, evidence_source="live_client") is None

    all_sets = store.list_feature_sets(123)
    assert len(all_sets) == 2
    assert {row["evidence_source"] for row in all_sets} == {"aggregate", "match_v5"}
    assert store.list_feature_sets(999) == []


def test_recent_local_feature_blocks_are_same_role_past_only_and_nonabstained(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    for game_id in (121, 122, 123, 124):
        report = _report(game_id)
        if game_id == 122:
            report["match"]["local_role"] = "top"
            report["participants"][0]["role"] = "top"
        store.save_report(report)

    def features(value, abstain=False, completeness=1.0):
        return {
            "feature_version": "features-v2",
            "evidence_source": "lcu_timeline",
            "abstain": abstain,
            "chosen_source_completeness": completeness,
            "participants": {
                "1": {
                    "fight_influence": {"untraded_deaths": value},
                    "baseline": {"role": "mid"},
                },
            },
        }

    store.save_feature_set(121, "features-v2", "lcu_timeline", features(1))
    store.save_feature_set(122, "features-v2", "lcu_timeline", features(2))
    store.save_feature_set(
        123, "features-v2", "lcu_timeline", features(3, completeness=0.4),
    )
    store.save_feature_set(124, "features-v2", "lcu_timeline", features(4))

    blocks = store.list_recent_local_feature_blocks(
        124, "features-v2", "lcu_timeline",
        min_completeness=0.7,
    )

    assert [
        block["fight_influence"]["untraded_deaths"] for block in blocks
    ] == [1]


def test_recent_local_feature_blocks_page_past_ineligible_games(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    for game_id in range(100, 126):
        store.save_report(_report(game_id))
        completeness = 1.0 if game_id < 104 or game_id == 125 else 0.4
        store.save_feature_set(
            game_id, "features-v2", "lcu_timeline",
            {
                "chosen_source_completeness": completeness,
                "participants": {
                    "1": {
                        "fight_influence": {"untraded_deaths": game_id},
                    },
                },
            },
        )

    blocks = store.list_recent_local_feature_blocks(
        125, "features-v2", "lcu_timeline",
        limit=3, min_completeness=0.7,
    )

    assert [
        block["fight_influence"]["untraded_deaths"] for block in blocks
    ] == [103, 102, 101]


def test_recent_local_feature_blocks_exclude_creation_time_ties(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    for game_id in (40, 45, 50, 60):
        report = _report(game_id)
        report["match"]["game_creation"] = 900 if game_id == 40 else 1000
        store.save_report(report)
        store.save_feature_set(
            game_id, "features-v2", "lcu_timeline",
            {
                "chosen_source_completeness": 1.0,
                "participants": {
                    "1": {
                        "fight_influence": {"untraded_deaths": game_id},
                    },
                },
            },
        )

    blocks = store.list_recent_local_feature_blocks(
        50, "features-v2", "lcu_timeline",
    )

    assert [
        block["fight_influence"]["untraded_deaths"] for block in blocks
    ] == [40]


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


# ── live client capture (live_capture_sessions/events/snapshots) ────────────

def test_live_capture_session_starts_without_a_known_game(tmp_path):
    store = HistoryStore(tmp_path / "history.db")

    store.start_live_capture_session("sess-1", metadata={"expected_game_id": None})

    session = store.get_live_capture_session("sess-1")
    assert session["game_id"] is None
    assert session["status"] == "active"
    assert session["completeness"] == 0.0
    assert session["last_event_id"] == -1
    assert session["metadata"] == {"expected_game_id": None}


def test_live_capture_events_are_deduplicated_by_event_id(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("sess-1")
    events = [
        {"event_id": 0, "event_time": 0.0, "event_type": "GameStart", "payload": {"EventID": 0}},
        {"event_id": 1, "event_time": 10.0, "event_type": "MinionsSpawning", "payload": {"EventID": 1}},
    ]

    first_batch = store.record_live_capture_events("sess-1", events)
    # A later poll re-sends the full cumulative Events list -- only the
    # genuinely new event should be counted/inserted.
    second_batch = store.record_live_capture_events("sess-1", events + [
        {"event_id": 2, "event_time": 20.0, "event_type": "ChampionKill", "payload": {"EventID": 2}},
    ])

    assert first_batch == 2
    assert second_batch == 1
    stored = store.get_live_capture_events("sess-1")
    assert [e["event_id"] for e in stored] == [0, 1, 2]
    assert stored[2]["event_type"] == "ChampionKill"


def test_live_capture_snapshot_skips_identical_consecutive_payloads(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("sess-1")
    payload = {"players": [{"kills": 1}]}

    first = store.record_live_capture_snapshot("sess-1", 10.0, payload)
    duplicate = store.record_live_capture_snapshot("sess-1", 25.0, dict(payload))
    changed = store.record_live_capture_snapshot("sess-1", 40.0, {"players": [{"kills": 2}]})

    assert first is True
    assert duplicate is False  # identical content -- storage budget, not persisted again
    assert changed is True
    snapshots = store.get_live_capture_snapshots("sess-1")
    assert [s["snapshot_time"] for s in snapshots] == [10.0, 40.0]


def test_live_capture_reconciliation_matches_and_flags_mismatch(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("sess-unknown")
    store.start_live_capture_session("sess-known", game_id=555)

    assert store.reconcile_live_capture_session("sess-unknown", 555) == "reconciled"
    assert store.get_live_capture_session("sess-unknown")["game_id"] == 555

    assert store.reconcile_live_capture_session("sess-known", 555) == "reconciled"

    assert store.reconcile_live_capture_session("sess-known", 999) == "mismatch"
    mismatched = store.get_live_capture_session("sess-known")
    assert mismatched["game_id"] == 555
    assert mismatched["status"] == "reconciliation_mismatch"


def test_live_capture_reconciliation_rejects_unknown_session(tmp_path):
    store = HistoryStore(tmp_path / "history.db")

    with pytest.raises(ValueError):
        store.reconcile_live_capture_session("does-not-exist", 123)


def test_find_resumable_live_capture_session_prefers_matching_game(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("older", game_id=None, started_at="2026-07-16T00:00:00Z")
    store.start_live_capture_session("newer", game_id=777, started_at="2026-07-16T01:00:00Z")

    assert store.find_resumable_live_capture_session(777) == "newer"
    assert store.find_resumable_live_capture_session(111) == "older"

    store.finalize_live_capture_session("older", status="completed")
    store.finalize_live_capture_session("newer", status="completed")
    assert store.find_resumable_live_capture_session(777) is None


def test_finalize_and_list_live_capture_sessions_by_status(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("a")
    store.start_live_capture_session("b")
    store.finalize_live_capture_session("a", status="partial_no_data")

    active = store.list_live_capture_sessions(status="active")
    partial = store.list_live_capture_sessions(status="partial_no_data")

    assert [s["session_id"] for s in active] == ["b"]
    assert [s["session_id"] for s in partial] == ["a"]
    assert store.get_live_capture_session("a")["ended_at"] is not None


def test_live_capture_snapshot_payload_is_compressed(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("sess-1")
    payload = {"players": [{"championName": "Darius", "scores": {"kills": 1}}] * 10}

    store.record_live_capture_snapshot("sess-1", 5.0, payload)

    with sqlite3.connect(store.path) as conn:
        compressed_size = conn.execute(
            "SELECT length(payload_zlib) FROM live_capture_snapshots "
            "WHERE session_id = ?",
            ("sess-1",),
        ).fetchone()[0]
    assert compressed_size < len(json.dumps(payload))
    assert store.get_live_capture_snapshots("sess-1")[0]["payload"] == payload


def test_live_capture_events_survive_concurrent_writers(tmp_path):
    import threading

    store = HistoryStore(tmp_path / "history.db")
    store.start_live_capture_session("sess-1")
    errors = []

    def writer(offset):
        try:
            for i in range(20):
                event_id = offset * 100 + i
                store.record_live_capture_events("sess-1", [{
                    "event_id": event_id, "event_time": float(event_id),
                    "event_type": "Tick", "payload": {"EventID": event_id},
                }])
        except Exception as exc:  # pragma: no cover - failure path only
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(offset,)) for offset in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    stored = store.get_live_capture_events("sess-1")
    assert len(stored) == 100
    assert len({e["event_id"] for e in stored}) == 100
