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
