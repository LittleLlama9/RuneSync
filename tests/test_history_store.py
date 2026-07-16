from pathlib import Path

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
