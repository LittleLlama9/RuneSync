import json
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from history_store import HistoryStore
from lcu import LCUConnectionError
from match_history import (
    TEAM_ROLES, MatchHistoryService, UnsupportedMatch, _resolve_roles,
    _timeline_role, normalize_match,
)
from mock_lcu import DraftState


CHAMPIONS = {
    14: "Sion", 86: "Garen", 154: "Zac", 360: "Samira", 902: "Milio",
    157: "Yasuo", 48: "Trundle", 800: "Mel", 161: "Vel'Koz", 145: "Kai'Sa",
}
FIXTURES = Path(__file__).parent / "fixtures"


def _game(game_id=123, queue_id=420, duration=1800):
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
