import io
import urllib.error

import pytest

from lcu import LCUClient


def test_enemy_role_lookup_identifies_opposite_gameflow_team(monkeypatch):
    client = LCUClient()
    client._summoner_id = 123
    monkeypatch.setattr(client, "_get", lambda path: {
        "gameData": {
            "teamOne": [
                {"summonerId": 999, "championId": 360,
                 "selectedPosition": "MIDDLE"},
            ],
            "teamTwo": [
                {"summonerId": 123, "championId": 14,
                 "selectedPosition": "MIDDLE"},
            ],
        },
    })

    assert client.get_enemy_champion_id_for_role("mid") == 360


def test_enemy_role_lookup_returns_none_when_position_unavailable(monkeypatch):
    client = LCUClient()
    client._summoner_id = 123
    monkeypatch.setattr(client, "_get", lambda path: {
        "gameData": {
            "teamOne": [{"summonerId": 999, "championId": 360,
                         "selectedPosition": ""}],
            "teamTwo": [{"summonerId": 123, "championId": 14,
                         "selectedPosition": "MIDDLE"}],
        },
    })

    assert client.get_enemy_champion_id_for_role("mid") is None


def test_match_history_uses_puuid_and_caps_limit(monkeypatch):
    client = LCUClient()
    client._puuid = "player-puuid"
    seen = []

    def fake_get(path):
        seen.append(path)
        return {"games": {"games": [{"gameId": 123}]}}

    monkeypatch.setattr(client, "_get", fake_get)

    assert client.get_match_history_summaries(500) == [{"gameId": 123}]
    assert seen == [
        "/lol-match-history/v1/products/lol/player-puuid/matches"
        "?begIndex=0&endIndex=99"
    ]


def test_match_details_rejects_mismatched_payload(monkeypatch):
    client = LCUClient()
    monkeypatch.setattr(client, "_get", lambda path: {"gameId": 999})

    with pytest.raises(Exception, match="invalid payload"):
        client.get_match_details(123)


def test_match_timeline_uses_numeric_game_id(monkeypatch):
    client = LCUClient()
    seen = []

    def fake_get(path):
        seen.append(path)
        return {"frames": []}

    monkeypatch.setattr(client, "_get", fake_get)

    assert client.get_match_timeline(123) == {"frames": []}
    assert seen == ["/lol-match-history/v1/game-timelines/123"]


def test_match_timeline_rejects_invalid_payload(monkeypatch):
    client = LCUClient()
    monkeypatch.setattr(client, "_get", lambda path: {"notFrames": []})

    with pytest.raises(Exception, match="invalid payload"):
        client.get_match_timeline(123)


def test_end_of_game_404_is_not_ready(monkeypatch):
    client = LCUClient()

    def fake_get(path):
        raise urllib.error.HTTPError(
            path, 404, "not ready", hdrs=None, fp=io.BytesIO(b""),
        )

    monkeypatch.setattr(client, "_get", fake_get)

    assert client.get_end_of_game_stats() is None


def test_active_game_id_reads_gameflow(monkeypatch):
    client = LCUClient()
    monkeypatch.setattr(
        client, "get_gameflow_session",
        lambda: {"gameData": {"gameId": 456}},
    )

    assert client.get_active_game_id() == 456


def test_match_details_wraps_socket_timeout(monkeypatch):
    client = LCUClient()
    monkeypatch.setattr(
        client, "_get",
        lambda path: (_ for _ in ()).throw(TimeoutError("timed out")),
    )

    with pytest.raises(Exception, match="League client not reachable"):
        client.get_match_details(123)
