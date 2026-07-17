import json
import sqlite3
import threading
import time
import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import match_history
from history_store import HistoryStore
from lcu import LCUConnectionError
from match_history import (
    MATCH_V5_SOURCE, TEAM_ROLES, MatchHistoryService, UnsupportedMatch,
    _resolve_roles, _timeline_role, normalize_match,
)
from mock_lcu import DraftState
from riot_api import (
    RiotApiAuthError, RiotApiRateLimitError, RiotApiTransientError,
)
from riot_provider_status import ProviderStatus, RiotProviderStatusTracker
from secret_store import RiotSecretStore, SecretStoreCorruptError
from score_v2.artifact import FeatureCoefficient, RoleCalibration, build_artifact
from score_v2.feature_spec import feature_contract_for_tier
from score_features import FEATURE_VERSION
from timeline_provider import PRIVATE_MATCH_V5_ENV


CHAMPIONS = {
    14: "Sion", 86: "Garen", 154: "Zac", 360: "Samira", 902: "Milio",
    157: "Yasuo", 48: "Trundle", 800: "Mel", 161: "Vel'Koz", 145: "Kai'Sa",
}
FIXTURES = Path(__file__).parent / "fixtures"


def _score_v2_artifact(source="aggregate"):
    return build_artifact(
        model_version="2.0.0-test",
        feature_version="2.0.0-evidence",
        calibration_version="2.0.0-test",
        evidence_source=source,
        intercept=0.0,
        coefficients=tuple(
            FeatureCoefficient(
                spec=spec,
                coefficient=0.5 if spec.direction > 0 else -0.5,
                robust_center=0.0,
                robust_scale=1.0,
            )
            for spec in feature_contract_for_tier(source)
        ),
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
        production_ready=False,
        release_notes="routing integration test",
        now=datetime.datetime(2026, 7, 18, tzinfo=datetime.timezone.utc),
    )


def _game(game_id=123, queue_id=420, duration=1800, platform_id="NA1"):
    champions = list(CHAMPIONS)
    lanes = [
        ("TOP", "SOLO"), ("JUNGLE", "NONE"), ("MIDDLE", "SOLO"),
        ("BOTTOM", "CARRY"), ("BOTTOM", "SUPPORT"),
    ] * 2
    participants = []
    identities = []
    for index, champion_id in enumerate(champions):
        participant_id = index + 1
        team_id = 100 if index < 5 else 200
        lane, role = lanes[index]
        participants.append({
            "participantId": participant_id,
            "championId": champion_id,
            "teamId": team_id,
            "timeline": {"lane": lane, "role": role},
            "stats": {
                "win": team_id == 100,
                "kills": 5 + index,
                "deaths": 3,
                "assists": 7,
                "goldEarned": 10000 + index * 100,
                "totalMinionsKilled": 150,
                "neutralMinionsKilled": 20 if role == "NONE" else 0,
                "champLevel": 16,
                "totalDamageDealtToChampions": 18000 + index * 1000,
                "damageDealtToObjectives": 5000 + index * 100,
                "damageDealtToTurrets": 2000 + index * 50,
                "totalDamageTaken": 15000,
                "damageSelfMitigated": 10000,
                "totalHeal": 1000,
                "visionScore": 20 + index,
                "wardsPlaced": 8,
                "wardsKilled": 2,
                "item0": 1054,
                "item1": 6664,
            },
        })
        identities.append({
            "participantId": participant_id,
            "player": {
                "puuid": f"puuid-{participant_id}",
                "gameName": f"Player{participant_id}",
                "tagLine": "TAG",
            },
        })
    return {
        "gameId": game_id,
        "platformId": platform_id,
        "queueId": queue_id,
        "mapId": 11,
        "gameMode": "CLASSIC",
        "gameDuration": duration,
        "gameCreation": 1721000000000 + game_id,
        "gameCreationDate": "2026-07-14T00:00:00Z",
        "gameVersion": "16.13.1",
        "participants": participants,
        "participantIdentities": identities,
    }


def _timeline(duration=1800):
    participant_frames = {
        str(participant_id): {"participantId": participant_id}
        for participant_id in range(1, 11)
    }
    return {
        "frames": [
            {
                "timestamp": timestamp,
                "participantFrames": participant_frames,
                "events": [],
            }
            for timestamp in range(0, duration * 1000 + 1, 60000)
        ] + ([{
            "timestamp": duration * 1000,
            "participantFrames": participant_frames,
            "events": [],
        }] if duration % 60 else []),
    }


def test_normalize_match_builds_report_and_roles():
    report = normalize_match(_game(), "puuid-1", CHAMPIONS)

    assert report["match"]["local_champion_name"] == "Sion"
    assert report["match"]["local_role"] == "top"
    assert len(report["participants"]) == 10
    assert len(report["scores"]) == 10
    assert {score["match_rank"] for score in report["scores"]} == set(range(1, 11))


@pytest.mark.parametrize("queue_id", [400, 420, 430, 440, 480, 490])
def test_standard_summoners_rift_queues_are_scored(queue_id):
    report = normalize_match(_game(queue_id=queue_id), "puuid-1", CHAMPIONS)

    assert report["match"]["queue_id"] == queue_id


def test_captured_positions_fill_missing_timeline_role():
    game = _game()
    game["participants"][0]["timeline"] = {"lane": "NONE", "role": "SUPPORT"}
    game["participants"][2]["timeline"] = {"lane": "NONE", "role": "DUO"}
    report = normalize_match(
        game, "puuid-1", CHAMPIONS, {"puuid-1": "mid"},
    )
    assert report["match"]["local_role"] == "mid"


def test_valid_played_role_beats_champ_select_assignment():
    game = _game()
    report = normalize_match(
        game, "puuid-1", CHAMPIONS, {"puuid-1": "mid"},
    )
    assert report["match"]["local_role"] == "top"


def test_unsupported_queue_and_remake_are_rejected():
    with pytest.raises(UnsupportedMatch):
        normalize_match(_game(queue_id=450), "puuid-1", CHAMPIONS)
    with pytest.raises(UnsupportedMatch):
        normalize_match(_game(duration=200), "puuid-1", CHAMPIONS)


def test_service_sync_is_idempotent(tmp_path):
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_history_summaries.return_value = [
        {"gameId": 123, "queueId": 420, "mapId": 11},
    ]
    lcu.get_match_details.return_value = _game()
    lcu.get_match_timeline.return_value = _timeline()
    service = MatchHistoryService(lcu, HistoryStore(tmp_path / "history.db"))

    assert service.sync_recent() == 1
    assert service.sync_recent() == 0
    assert len(service.list_history()) == 1
    timeline = service.store.get_timeline_payload(123, "lcu_timeline")
    assert timeline["completeness"] == 1.0
    assert timeline["payload"]["provenance"]["source"] == "lcu_timeline"
    features = service.store.get_feature_set(
        123, evidence_source="lcu_timeline",
    )
    assert features["features"]["evidence_source"] == "lcu_timeline"
    assert len(service.store.list_score_runs(123)) == 1


def test_ingest_routes_v2_through_best_tier_with_a_registered_artifact(tmp_path):
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_details.return_value = _game()
    lcu.get_match_timeline.return_value = _timeline()
    store = HistoryStore(tmp_path / "history.db")
    artifact = _score_v2_artifact()
    service = MatchHistoryService(
        lcu, store,
        score_v2_artifacts={"aggregate": artifact},
        allow_development_score_v2=True,
    )

    service.ingest_game(123, attempt_match_v5=False)

    runs = store.list_score_runs(123)
    assert len(runs) == 2
    active = next(run for run in runs if run["is_active"])
    assert active["model_version"] == 2
    assert active["evidence_source"] == "aggregate"
    assert active["artifact_model_version"] == artifact.model_version
    assert active["model_family"] == artifact.model_family
    report = store.get_report(123)
    assert all(row["model_version"] == 2 for row in report["participants"])
    assert all(row["participant_confidence"] is not None for row in report["participants"])


def test_refresh_reextracts_same_tier_content_without_artifact(tmp_path, monkeypatch):
    lcu = MagicMock()
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(normalize_match(_game(), "puuid-1", CHAMPIONS))
    store.save_timeline_payload(
        123, "lcu_timeline",
        {"provenance": {"source": "lcu_timeline"}, "timeline": _timeline()},
        completeness=1.0,
    )
    service = MatchHistoryService(lcu, store)
    service.refresh_score_v2(123)
    original = match_history.extract_game_features
    extractor = MagicMock(side_effect=original)
    monkeypatch.setattr(match_history, "extract_game_features", extractor)

    assert service.refresh_score_v2(123) is None

    extractor.assert_called_once_with(
        store, 123, FEATURE_VERSION, evidence_source="lcu_timeline",
    )
    assert len(store.list_feature_sets(123)) == 1
    assert len(store.list_score_runs(123)) == 1


def test_stronger_feature_failure_does_not_block_healthy_routed_tier(
        tmp_path, monkeypatch):
    lcu = MagicMock()
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(normalize_match(_game(), "puuid-1", CHAMPIONS))
    store.save_timeline_payload(
        123, "lcu_timeline",
        {"provenance": {"source": "lcu_timeline"}, "timeline": _timeline()},
        completeness=1.0,
    )
    logs = []
    service = MatchHistoryService(
        lcu, store,
        on_log=lambda message, tag: logs.append((message, tag)),
        score_v2_artifacts={"aggregate": _score_v2_artifact("aggregate")},
        allow_development_score_v2=True,
    )
    original = match_history.extract_game_features

    def extract(store_arg, game_id, feature_version, evidence_source=None):
        if evidence_source == "lcu_timeline":
            raise ValueError("simulated malformed local timeline")
        return original(
            store_arg, game_id, feature_version,
            evidence_source=evidence_source,
        )

    monkeypatch.setattr(match_history, "extract_game_features", extract)

    run_id = service.refresh_score_v2(123)

    assert run_id is not None
    active = next(
        run for run in store.list_score_runs(123) if run["is_active"]
    )
    assert active["id"] == run_id
    assert active["evidence_source"] == "aggregate"
    assert logs == [(
        "History could not persist stronger lcu_timeline Score v2 evidence "
        "for game 123; continuing with registered aggregate evidence: "
        "simulated malformed local timeline",
        "warn",
    )]


def test_unroutable_artifact_does_not_hide_evidence_retention_failure(
        tmp_path, monkeypatch):
    lcu = MagicMock()
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(normalize_match(_game(), "puuid-1", CHAMPIONS))
    store.save_timeline_payload(
        123, "lcu_timeline",
        {"provenance": {"source": "lcu_timeline"}, "timeline": _timeline()},
        completeness=1.0,
    )
    logs = []
    service = MatchHistoryService(
        lcu, store,
        on_log=lambda message, tag: logs.append((message, tag)),
        score_v2_artifacts={"match_v5": _score_v2_artifact("match_v5")},
        allow_development_score_v2=True,
    )
    monkeypatch.setattr(
        match_history, "extract_game_features",
        MagicMock(side_effect=ValueError("simulated malformed local timeline")),
    )

    assert service.refresh_score_v2(123) is None

    assert len(store.list_score_runs(123)) == 1
    assert logs == [(
        "History could not persist lcu_timeline Score v2 evidence for game "
        "123: simulated malformed local timeline",
        "warn",
    )]


def test_stronger_timeline_evidence_appends_and_activates_an_upgrade(tmp_path):
    lcu = MagicMock()
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(normalize_match(_game(), "puuid-1", CHAMPIONS))
    aggregate_artifact = _score_v2_artifact("aggregate")
    lcu_artifact = _score_v2_artifact("lcu_timeline")
    service = MatchHistoryService(
        lcu, store,
        score_v2_artifacts={
            "aggregate": aggregate_artifact,
            "lcu_timeline": lcu_artifact,
        },
        allow_development_score_v2=True,
    )

    aggregate_run = service.refresh_score_v2(123)
    store.save_timeline_payload(
        123, "lcu_timeline",
        {"provenance": {"source": "lcu_timeline"}, "timeline": _timeline()},
        completeness=1.0,
    )
    lcu_run = service.refresh_score_v2(123)

    assert aggregate_run != lcu_run
    runs = store.list_score_runs(123)
    assert len(runs) == 3
    assert next(run for run in runs if run["id"] == aggregate_run)["is_active"] is False
    active = next(run for run in runs if run["id"] == lcu_run)
    assert active["is_active"] is True
    assert active["evidence_source"] == "lcu_timeline"
    assert store.get_report(123)["participants"][0]["model_artifact_hash"] == (
        lcu_artifact.content_hash
    )


def test_sync_recent_summarizes_timeline_failures(tmp_path):
    games = {game_id: _game(game_id) for game_id in (101, 102)}
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_history_summaries.return_value = [
        {"gameId": game_id, "queueId": 420, "mapId": 11}
        for game_id in games
    ]
    lcu.get_match_details.side_effect = lambda game_id: games[game_id]
    lcu.get_match_timeline.side_effect = LCUConnectionError("unavailable")
    logs = []
    service = MatchHistoryService(
        lcu,
        HistoryStore(tmp_path / "history.db"),
        on_log=lambda message, tag: logs.append((message, tag)),
    )

    assert service.sync_recent() == 2
    timeline_logs = [
        message for message, tag in logs
        if tag == "warn" and "timeline" in message
    ]
    assert timeline_logs == [
        "History could not capture 2 local timelines; "
        "aggregate evidence remains available."
    ]


def test_sync_recent_backfills_stored_games_missing_from_lcu_summaries(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    store.save_report(normalize_match(_game(), "puuid-1", CHAMPIONS))
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_match_history_summaries.return_value = []
    lcu.get_match_details.return_value = _game()
    lcu.get_match_timeline.return_value = _timeline()
    service = MatchHistoryService(lcu, store)

    assert service.sync_recent() == 0
    assert store.has_timeline_payload(123, "lcu_timeline")


def test_sync_recent_isolates_timeline_persistence_failure(tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "history.db")
    for game_id in (122, 123):
        report = normalize_match(_game(game_id), "puuid-1", CHAMPIONS)
        store.save_report(report)
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_match_history_summaries.return_value = []
    lcu.get_match_details.side_effect = lambda game_id: _game(game_id)
    lcu.get_match_timeline.return_value = _timeline()
    logs = []
    service = MatchHistoryService(
        lcu, store, on_log=lambda message, tag: logs.append((message, tag)),
    )
    original_save = store.save_timeline_payload

    def save_with_one_failure(game_id, *args, **kwargs):
        if game_id == 123:
            raise sqlite3.OperationalError("database is busy")
        return original_save(game_id, *args, **kwargs)

    monkeypatch.setattr(store, "save_timeline_payload", save_with_one_failure)

    assert service.sync_recent() == 0
    assert not store.has_timeline_payload(123, "lcu_timeline")
    assert store.has_timeline_payload(122, "lcu_timeline")
    assert store.get_meta("last_sync")
    assert any(
        tag == "warn" and "1 local timeline" in message
        for message, tag in logs
    )


def test_sync_recent_isolates_missing_timeline_scan_failure(
        tmp_path, monkeypatch):
    store = HistoryStore(tmp_path / "history.db")
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_history_summaries.return_value = [
        {"gameId": 123, "queueId": 420, "mapId": 11},
    ]
    lcu.get_match_details.return_value = _game()
    lcu.get_match_timeline.return_value = _timeline()
    logs = []
    notified = []
    service = MatchHistoryService(
        lcu,
        store,
        on_log=lambda message, tag: logs.append((message, tag)),
        on_updated=lambda: notified.append(True),
    )
    monkeypatch.setattr(
        store,
        "game_ids_missing_timeline",
        lambda *args, **kwargs: (
            (_ for _ in ()).throw(sqlite3.OperationalError("database busy"))
        ),
    )

    assert service.sync_recent() == 1
    assert store.has_game(123)
    assert store.get_meta("last_sync")
    assert notified == [True]
    assert (
        "History could not inspect missing local timelines; "
        "timeline backfill will retry later.",
        "warn",
    ) in logs


def test_postgame_uses_captured_game_and_notifies(tmp_path):
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_gameflow_session.return_value = {
        "gameData": {
            "gameId": 123,
            "teamOne": [
                {"puuid": "puuid-1", "selectedPosition": "MIDDLE"},
            ],
            "teamTwo": [],
        },
    }
    lcu.get_match_details.return_value = _game()
    lcu.get_match_timeline.return_value = _timeline()
    notified = []
    service = MatchHistoryService(
        lcu, HistoryStore(tmp_path / "history.db"),
        on_postgame=notified.append,
    )
    service.capture_active_game()

    assert service.ingest_after_game(retries=1, delay=0) == 123
    assert notified == [123]
    assert service.report(123)["match"]["local_role"] == "top"


def test_mock_lcu_supplies_complete_scored_match():
    state = DraftState()
    report = normalize_match(
        state.match_dict(), "mock-puuid-0000", state.champion_map,
    )
    assert len(report["participants"]) == 10
    assert len(report["scores"]) == 10


def test_sanitized_real_lcu_payload_normalizes_end_to_end():
    game = json.loads(
        (FIXTURES / "real_lcu_match_sanitized.json").read_text(encoding="utf-8")
    )
    champions = {
        23: "Tryndamere", 59: "Jarvan IV", 3: "Galio", 203: "Kindred",
        223: "Tahm Kench", 80: "Pantheon", 19: "Warwick", 805: "Mel",
        202: "Jhin", 111: "Nautilus",
    }
    report = normalize_match(game, "fixture-puuid-1", champions)

    assert report["match"]["local_role"] == "top"
    assert len(report["participants"]) == 10
    for team_id in (100, 200):
        assert {
            p["role"] for p in report["participants"] if p["team_id"] == team_id
        } == set(TEAM_ROLES)
    assert {score["match_rank"] for score in report["scores"]} == set(range(1, 11))


def test_ambiguous_bottom_duo_role_is_not_assumed_to_be_adc():
    assert _timeline_role({
        "timeline": {"lane": "BOTTOM", "role": "DUO"},
    }) == ""


def test_duplicate_timeline_roles_resolve_to_one_role_each():
    players = [
        {"participant_id": 1, "puuid": "a", "champion_name": "Volibear",
         "team_id": 100, "role": "top"},
        {"participant_id": 2, "puuid": "b", "champion_name": "Darius",
         "team_id": 100, "role": "top"},
        {"participant_id": 3, "puuid": "c", "champion_name": "Graves",
         "team_id": 100, "role": "jungle"},
        {"participant_id": 4, "puuid": "d", "champion_name": "Zed",
         "team_id": 100, "role": "mid"},
        {"participant_id": 5, "puuid": "e", "champion_name": "Thresh",
         "team_id": 100, "role": "support"},
    ]
    _resolve_roles(players, {"b": "bot"})
    assert {player["role"] for player in players} == set(TEAM_ROLES)
    assert next(player for player in players if player["puuid"] == "b")["role"] == "bot"


def test_sync_recent_repairs_gap_after_transient_failure(tmp_path):
    games = {
        game_id: _game(game_id=game_id)
        for game_id in (101, 102, 103)
    }
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_history_summaries.return_value = [
        {"gameId": game_id, "queueId": 420, "mapId": 11}
        for game_id in (103, 102, 101)
    ]
    failed = {"once": True}

    def details(game_id):
        if game_id == 102 and failed["once"]:
            failed["once"] = False
            raise LCUConnectionError("transient")
        return games[game_id]

    lcu.get_match_details.side_effect = details
    lcu.get_match_timeline.side_effect = lambda game_id: _timeline(
        games[game_id]["gameDuration"]
    )
    service = MatchHistoryService(lcu, HistoryStore(tmp_path / "history.db"))

    assert service.sync_recent() == 2
    assert not service.store.has_game(102)
    assert service.sync_recent() == 1
    assert service.store.has_game(102)


def test_failed_capture_cannot_reopen_previous_game(tmp_path):
    games = {101: _game(101), 102: _game(102)}
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_details.side_effect = lambda game_id: games[game_id]
    lcu.get_match_timeline.side_effect = lambda game_id: _timeline(
        games[game_id]["gameDuration"]
    )
    opened = []
    service = MatchHistoryService(
        lcu, HistoryStore(tmp_path / "history.db"), on_postgame=opened.append,
    )
    service._active_game_id = 101
    assert service.ingest_after_game(retries=1, delay=0) == 101

    lcu.get_gameflow_session.return_value = None
    service.capture_active_game()
    lcu.get_end_of_game_stats.return_value = None
    lcu.get_match_history_summaries.return_value = [
        {"gameId": 102, "queueId": 420, "mapId": 11},
    ]
    assert service.ingest_after_game(retries=1, delay=0) == 102

    assert opened == [101, 102]
    assert service.store.has_game(102)
    assert service._active_game_id is None


def test_ingest_game_serializes_duplicate_concurrent_imports(tmp_path):
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    calls = {"details": 0}

    def details(game_id):
        calls["details"] += 1
        time.sleep(0.03)
        return _game(game_id)

    lcu.get_match_details.side_effect = details
    lcu.get_match_timeline.return_value = _timeline()
    service = MatchHistoryService(lcu, HistoryStore(tmp_path / "history.db"))
    results = []

    threads = [
        threading.Thread(target=lambda: results.append(service.ingest_game(123)))
        for _ in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls["details"] == 1
    assert len(results) == 2
    assert all(result["match"]["game_id"] == 123 for result in results)


def test_old_ingestion_cannot_clear_new_game_capture(tmp_path):
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    started = threading.Event()
    release = threading.Event()

    def details(game_id):
        started.set()
        release.wait(timeout=2)
        return _game(game_id)

    lcu.get_match_details.side_effect = details
    lcu.get_match_timeline.return_value = _timeline()
    service = MatchHistoryService(lcu, HistoryStore(tmp_path / "history.db"))
    service._active_game_id = 101
    worker = threading.Thread(
        target=lambda: service.ingest_after_game(retries=1, delay=0),
    )
    worker.start()
    assert started.wait(timeout=1)

    lcu.get_gameflow_session.return_value = {
        "gameData": {"gameId": 102, "teamOne": [], "teamTwo": []},
    }
    service.capture_active_game()
    release.set()
    worker.join(timeout=2)

    assert service._active_game_id == 102


# ---------------------------------------------------------------------------
# Private Match-V5 timeline integration
#
# Match-V5 is an opt-in upgrade path (RUNESYNC_ENABLE_PRIVATE_RIOT_MATCH_V5)
# layered strictly after LCU-derived history. These tests inject a throwaway
# RiotSecretStore (FakeSecretBackend, never the real DPAPI backend) and a
# fake Riot client via MatchHistoryService's riot_status_tracker /
# riot_client_factory constructor seams, so no real network or on-disk key
# is ever touched.
# ---------------------------------------------------------------------------


class FakeSecretBackend:
    """In-memory stand-in for the real DPAPI backend used only in tests."""

    def protect(self, plaintext: bytes) -> bytes:
        return b"enc:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"enc:"):
            raise SecretStoreCorruptError("corrupt")
        return ciphertext[4:][::-1]


def _riot_secret_store(tmp_path, key=None):
    store = RiotSecretStore(tmp_path / "riot.bin", backend=FakeSecretBackend())
    if key:
        store.set_key(key)
    return store


class FakeRiotClient:
    """Stand-in for RiotApiClient: raises/returns canned match/timeline data."""

    def __init__(self, match_payload=None, timeline_payload=None,
                 match_error=None, timeline_error=None):
        self._match_payload = match_payload
        self._timeline_payload = timeline_payload
        self._match_error = match_error
        self._timeline_error = timeline_error
        self.calls = []

    def get_match(self, match_id):
        self.calls.append(("match", match_id))
        if self._match_error is not None:
            raise self._match_error
        return self._match_payload

    def get_timeline(self, match_id):
        self.calls.append(("timeline", match_id))
        if self._timeline_error is not None:
            raise self._timeline_error
        return self._timeline_payload


def _match_v5_frames(duration):
    participant_frames = {
        str(pid): {"participantId": pid} for pid in range(1, 11)
    }
    timestamps = list(range(0, duration * 1000, 60000)) + [duration * 1000]
    return [
        {"timestamp": ts, "participantFrames": participant_frames, "events": []}
        for ts in timestamps
    ]


def _match_v5_payloads(game_id=123, platform_id="NA1", duration=1800, puuids=None):
    puuids = puuids or [f"puuid-{i}" for i in range(1, 11)]
    match_id = f"{platform_id}_{game_id}"
    participants = [
        {"participantId": i, "puuid": puuid} for i, puuid in enumerate(puuids, start=1)
    ]
    match_payload = {
        "metadata": {"matchId": match_id, "participants": puuids},
        "info": {
            "gameId": game_id, "gameDuration": duration,
            "participants": participants,
        },
    }
    timeline_payload = {
        "metadata": {"matchId": match_id, "participants": puuids},
        "info": {"frames": _match_v5_frames(duration)},
    }
    return match_payload, timeline_payload


def _service_with_match_v5(tmp_path, lcu, riot_client, key="dev-key", store=None):
    secret_store = _riot_secret_store(tmp_path, key=key)
    tracker = RiotProviderStatusTracker(store=secret_store)
    service = MatchHistoryService(
        lcu, store or HistoryStore(tmp_path / "history.db"),
        riot_status_tracker=tracker,
        riot_client_factory=lambda key_supplier: riot_client,
        match_v5_scheduler=lambda task: task(),
    )
    return service, tracker, secret_store


def _lcu_for_game(game_id=123, duration=1800, platform_id="NA1"):
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_details.return_value = _game(
        game_id, duration=duration, platform_id=platform_id,
    )
    lcu.get_match_timeline.return_value = _timeline(duration)
    return lcu


def _attempt_count(store: HistoryStore, game_id: int, source: str) -> int:
    with sqlite3.connect(store.path) as conn:
        row = conn.execute(
            "SELECT attempt_count FROM timeline_fetch_attempts "
            "WHERE game_id = ? AND source = ?",
            (game_id, source),
        ).fetchone()
    return row[0] if row else 0


def test_gate_disabled_skips_match_v5_entirely(tmp_path):
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_game(123)
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    # The gate must be checked before any I/O: no client call, no backoff row.
    assert riot_client.calls == []
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 0


def test_missing_key_skips_silently_without_backoff_row(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, secret_store = _service_with_match_v5(
        tmp_path, lcu, riot_client, key=None,
    )

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    assert riot_client.calls == []
    # A missing key is a global config issue, not a per-match fault: no
    # backoff row should be created for it.
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 0
    assert tracker.status() is ProviderStatus.MISSING


def test_corrupt_key_skips_silently_without_backoff_row(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    path = tmp_path / "riot.bin"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"not-encrypted-with-our-backend")
    secret_store = RiotSecretStore(path, backend=FakeSecretBackend())
    tracker = RiotProviderStatusTracker(store=secret_store)
    service = MatchHistoryService(
        lcu, HistoryStore(tmp_path / "history.db"),
        riot_status_tracker=tracker,
        riot_client_factory=lambda key_supplier: riot_client,
    )

    report = service.ingest_game(123)

    assert report is not None
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    assert riot_client.calls == []
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 0
    assert tracker.status() is ProviderStatus.CORRUPT


def test_auth_rejected_records_backoff_without_breaking_lcu_history(
        tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    riot_client = FakeRiotClient(
        match_error=RiotApiAuthError("super-secret-token"),
    )
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_game(123)
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1
    assert tracker.status() is ProviderStatus.AUTH_REJECTED


def test_routing_builds_platform_prefixed_match_id(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game(platform_id="EUW1")
    match_payload, timeline_payload = _match_v5_payloads(platform_id="EUW1")
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    report = service.ingest_game(123)

    assert report is not None
    assert riot_client.calls == [
        ("match", "EUW1_123"), ("timeline", "EUW1_123"),
    ]
    stored = service.store.get_timeline_payload(123, MATCH_V5_SOURCE)
    assert stored["payload"]["provenance"]["platform_id"] == "EUW1"
    assert stored["payload"]["provenance"]["regional_route"] == "EUROPE"
    assert tracker.status() is ProviderStatus.AVAILABLE


def test_missing_platform_id_fails_explicitly_without_guessing(
        tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    game = _game(123)
    del game["platformId"]
    for identity in game["participantIdentities"]:
        identity["player"].pop("currentPlatformId", None)
        identity["player"].pop("platformId", None)
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_details.return_value = game
    lcu.get_match_timeline.return_value = _timeline()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    # No routable platform ID: the provider must never be reached.
    assert riot_client.calls == []
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1


def test_caching_does_not_refetch_a_stored_match_v5_payload(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    service.ingest_game(123)
    assert len(riot_client.calls) == 2
    assert service.store.has_timeline_payload(123, MATCH_V5_SOURCE)

    # Re-ingesting an already-known game must not re-invoke the client.
    service.ingest_game(123)

    assert len(riot_client.calls) == 2


def test_match_v5_upgrade_is_scheduled_after_lcu_ingestion_returns(
        tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    secret_store = _riot_secret_store(tmp_path, key="dev-key")
    tracker = RiotProviderStatusTracker(store=secret_store)
    scheduled = []
    service = MatchHistoryService(
        lcu,
        HistoryStore(tmp_path / "history.db"),
        riot_status_tracker=tracker,
        riot_client_factory=lambda key_supplier: riot_client,
        match_v5_scheduler=scheduled.append,
    )

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_game(123)
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert riot_client.calls == []
    assert len(scheduled) == 1
    assert service._ingest_lock.acquire(blocking=False)
    service._ingest_lock.release()

    scheduled[0]()

    assert riot_client.calls == [
        ("match", "NA1_123"), ("timeline", "NA1_123"),
    ]
    assert service.store.has_timeline_payload(123, MATCH_V5_SOURCE)


def test_backoff_blocks_an_immediate_retry_after_a_failure(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    riot_client = FakeRiotClient(match_error=RiotApiRateLimitError("nope"))
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    service.ingest_game(123)
    assert len(riot_client.calls) == 1
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1

    # An immediate retry must be skipped by the per-match backoff window,
    # not just by tracker-level rate-limit status.
    assert service._save_match_v5_timeline(123) is False
    assert len(riot_client.calls) == 1
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1


def test_malformed_payload_is_rejected_and_lcu_history_survives(
        tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    match_payload["info"]["frames"] = []
    timeline_payload["info"]["frames"] = []
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1


def test_cross_source_participant_mismatch_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    # An individually well-formed Match-V5 payload (10 participants, valid
    # frames, matching internal metadata) but with puuids that don't match
    # what was locally stored for this game -- e.g. a same-numeric-ID
    # collision on the wrong region. This must be rejected even though it
    # would pass RiotMatchV5Provider's own internal validation.
    other_puuids = [f"other-puuid-{i}" for i in range(1, 11)]
    match_payload, timeline_payload = _match_v5_payloads(puuids=other_puuids)
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    report = service.ingest_game(123)

    assert report is not None
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1


def test_cross_source_identity_db_error_does_not_break_lcu_history(
        tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, _, _ = _service_with_match_v5(tmp_path, lcu, riot_client)
    logs = []
    service.on_log = lambda message, tag: logs.append((message, tag))
    monkeypatch.setattr(
        service.store,
        "participant_puuids",
        lambda game_id: (_ for _ in ()).throw(
            sqlite3.OperationalError("database is locked")
        ),
    )

    report = service.ingest_game(123)

    assert report is not None
    assert service.store.has_game(123)
    assert service.store.has_timeline_payload(123, "lcu_timeline")
    assert not service.store.has_timeline_payload(123, MATCH_V5_SOURCE)
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1
    assert any(
        tag == "warn" and "participant identities" in message
        for message, tag in logs
    )


def test_match_v5_persistence_error_records_backoff_after_successful_fetch(
        tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, _, _ = _service_with_match_v5(tmp_path, lcu, riot_client)
    service.ingest_game(123, attempt_match_v5=False)
    original_save = service.store.save_timeline_payload

    def fail_match_v5(game_id, source, *args, **kwargs):
        if source == MATCH_V5_SOURCE:
            raise sqlite3.OperationalError("database is locked")
        return original_save(game_id, source, *args, **kwargs)

    monkeypatch.setattr(
        service.store, "save_timeline_payload", fail_match_v5,
    )

    assert service._save_match_v5_timeline(123) is False
    assert _attempt_count(service.store, 123, MATCH_V5_SOURCE) == 1
    assert not service.store.timeline_fetch_due(123, MATCH_V5_SOURCE)


def test_immutable_storage_is_content_hash_deduplicated(tmp_path, monkeypatch):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = _lcu_for_game()
    match_payload, timeline_payload = _match_v5_payloads()
    riot_client = FakeRiotClient(match_payload, timeline_payload)
    service, tracker, _ = _service_with_match_v5(tmp_path, lcu, riot_client)

    service.ingest_game(123)
    stored = service.store.get_timeline_payload(123, MATCH_V5_SOURCE)
    assert stored["payload"]["provenance"]["source"] == MATCH_V5_SOURCE
    assert stored["completeness"] == 1.0

    with sqlite3.connect(service.store.path) as conn:
        rows_before = conn.execute(
            "SELECT COUNT(*) FROM timeline_payloads"
        ).fetchone()[0]

    # Re-saving the identical payload directly must hash-dedupe rather than
    # insert a second row.
    service.store.save_timeline_payload(
        123, MATCH_V5_SOURCE, stored["payload"],
        schema_version="match-v5-v1", completeness=1.0,
    )

    with sqlite3.connect(service.store.path) as conn:
        rows_after = conn.execute(
            "SELECT COUNT(*) FROM timeline_payloads"
        ).fetchone()[0]

    assert rows_after == rows_before


@pytest.mark.parametrize(
    ("global_error", "expected_status"),
    [
        (RiotApiRateLimitError("nope"), ProviderStatus.RATE_LIMITED),
        (RiotApiAuthError("nope"), ProviderStatus.AUTH_REJECTED),
        (
            RiotApiTransientError("nope"),
            ProviderStatus.UPSTREAM_UNAVAILABLE,
        ),
    ],
)
def test_backfill_global_error_stops_pass_without_starving_other_games(
        tmp_path, monkeypatch, global_error, expected_status):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_details.side_effect = lambda game_id: _game(game_id)
    store = HistoryStore(tmp_path / "history.db")
    for game_id in (201, 202):
        game = _game(game_id)
        local_puuid = "puuid-1"
        report = normalize_match(game, local_puuid, CHAMPIONS, None)
        store.save_report(report)
        store.save_timeline_payload(
            game_id, "lcu_timeline",
            {"provenance": {"source": "lcu_timeline"}, "timeline": _timeline()},
            schema_version="lcu-v1", completeness=1.0,
        )
    riot_client = FakeRiotClient(match_error=global_error)
    service, tracker, _ = _service_with_match_v5(
        tmp_path, lcu, riot_client, store=store,
    )

    failures, scan_failed = service._backfill_match_v5_timelines(limit=10)

    assert scan_failed is False
    assert failures == 1
    assert tracker.status() is expected_status
    # Only the first game in the pass should have been attempted; the
    # second must be left untouched (still due) for a future pass rather
    # than starved or hammered in the same rate-limited pass.
    attempted_game_ids = {
        int(call[1].split("_")[1]) for call in riot_client.calls
        if call[0] == "match"
    }
    assert len(attempted_game_ids) == 1
    untouched_game_id = ({201, 202} - attempted_game_ids).pop()
    assert _attempt_count(store, untouched_game_id, MATCH_V5_SOURCE) == 0
    assert store.timeline_fetch_due(untouched_game_id, MATCH_V5_SOURCE) is True


@pytest.mark.parametrize(
    "global_error",
    [
        RiotApiAuthError("nope"),
        RiotApiRateLimitError("nope"),
        RiotApiTransientError("nope"),
    ],
)
def test_sync_global_error_opens_one_pass_circuit_without_blocking_lcu_imports(
        tmp_path, monkeypatch, global_error):
    monkeypatch.setenv(PRIVATE_MATCH_V5_ENV, "1")
    game_ids = (101, 102, 103)
    games = {game_id: _game(game_id) for game_id in game_ids}
    lcu = MagicMock()
    lcu.get_current_summoner_puuid.return_value = "puuid-1"
    lcu.get_champion_name_map.return_value = CHAMPIONS
    lcu.get_match_history_summaries.return_value = [
        {"gameId": game_id, "queueId": 420, "mapId": 11}
        for game_id in game_ids
    ]
    lcu.get_match_details.side_effect = lambda game_id: games[game_id]
    lcu.get_match_timeline.return_value = _timeline()
    riot_client = FakeRiotClient(match_error=global_error)
    service, tracker, _ = _service_with_match_v5(
        tmp_path, lcu, riot_client,
    )

    imported = service.sync_recent()

    assert imported == 3
    assert len([call for call in riot_client.calls if call[0] == "match"]) == 1
    assert all(service.store.has_game(game_id) for game_id in game_ids)
    assert all(
        service.store.has_timeline_payload(game_id, "lcu_timeline")
        for game_id in game_ids
    )
    assert not any(
        service.store.has_timeline_payload(game_id, MATCH_V5_SOURCE)
        for game_id in game_ids
    )
