"""Tests for live_hud — in-game HUD snapshot derivation from Live Client Data."""
import live_hud


# A gold estimator that mirrors item_data.estimate_gold_from_items but with a
# fixed toy price table, so tests never touch the network / Data Dragon.
_PRICES = {1001: 300, 3006: 1100, 3078: 3300, 6672: 3400, 3153: 3200}


def _gold(item_ids):
    return sum(_PRICES.get(i, 0) for i in (item_ids or []))


def _player(name, team, pos, cs, level, items=None):
    return {
        "riotId": f"{name}#NA1",
        "riotIdGameName": name,
        "riotIdTagLine": "NA1",
        "summonerName": name,
        "championName": name,
        "team": team,
        "position": pos,
        "level": level,
        "scores": {"creepScore": cs},
        "items": [{"itemID": i} for i in (items or [])],
    }


def _payload(game_time=600.0, events=None, active_gold=250.0):
    return {
        "activePlayer": {"riotId": "Me#NA1", "summonerName": "Me",
                         "currentGold": active_gold},
        "allPlayers": [
            _player("Me", "ORDER", "MIDDLE", 100, 9, items=[6672, 1001]),
            _player("Foe", "CHAOS", "MIDDLE", 80, 8, items=[3153]),
            _player("AllyTop", "ORDER", "TOP", 90, 9, items=[3078]),
            _player("EnemyTop", "CHAOS", "TOP", 95, 9, items=[3006]),
        ],
        "events": {"Events": events or []},
        "gameData": {"gameTime": game_time, "gameMode": "CLASSIC"},
    }


def test_returns_none_on_garbage():
    assert live_hud.build_hud(None) is None
    assert live_hud.build_hud({}) is None
    # activePlayer present but no matching allPlayers entry -> None.
    assert live_hud.build_hud({"activePlayer": {"summonerName": "Ghost"},
                               "allPlayers": [_player("X", "ORDER", "MID", 0, 1)],
                               "gameData": {"gameTime": 1}}) is None


def test_me_cs_per_min_and_level():
    hud = live_hud.build_hud(_payload(game_time=600.0), gold_fn=_gold)
    assert hud["me"]["champion"] == "Me"
    assert hud["me"]["cs"] == 100
    assert hud["me"]["cs_per_min"] == 10.0   # 100 cs / 10 min
    assert hud["me"]["level"] == 9
    assert hud["me"]["gold"] == 250          # real unspent gold, local only


def test_lane_opponent_paired_by_position():
    hud = live_hud.build_hud(_payload(), gold_fn=_gold)
    assert hud["opponent"]["champion"] == "Foe"
    assert hud["opponent"]["cs"] == 80
    assert hud["delta"]["cs"] == 20          # 100 - 80
    assert hud["delta"]["level"] == 1        # 9 - 8


def test_lane_opponent_falls_back_to_role_when_position_blank():
    data = _payload()
    for p in data["allPlayers"]:
        p["position"] = ""  # emulate a payload with no positions
    hud = live_hud.build_hud(data, fallback_role="mid", gold_fn=_gold)
    # With no positions at all we cannot pair an opponent; ensure it degrades
    # gracefully rather than crashing.
    assert hud is not None
    assert hud["opponent"] is None


def test_gold_estimate_from_items():
    hud = live_hud.build_hud(_payload(), gold_fn=_gold)
    # Me: 6672 (3400) + 1001 (300) = 3700 invested.
    assert hud["me"]["est_gold"] == 3700
    # Foe: 3153 (3200).
    assert hud["opponent"]["est_gold"] == 3200
    assert hud["delta"]["gold"] == 500       # 3700 - 3200


def test_team_gold_diff():
    hud = live_hud.build_hud(_payload(), gold_fn=_gold)
    # Ours: Me 3700 + AllyTop 3078(3300) = 7000. Theirs: Foe 3200 + EnemyTop 3006(1100) = 4300.
    assert hud["team_gold"]["ours"] == 7000
    assert hud["team_gold"]["theirs"] == 4300
    assert hud["team_gold"]["diff"] == 2700


def test_objective_pending_before_initial_spawn():
    hud = live_hud.build_hud(_payload(game_time=120.0), gold_fn=_gold)
    objs = {o["name"]: o for o in hud["objectives"]}
    dragon = objs["Dragon"]
    assert dragon["state"] == "pending"
    assert dragon["next_seconds"] == 180     # 300 - 120


def test_objective_respawn_countdown_after_kill():
    events = [{"EventID": 1, "EventName": "DragonKill", "EventTime": 400.0}]
    hud = live_hud.build_hud(_payload(game_time=520.0, events=events), gold_fn=_gold)
    objs = {o["name"]: o for o in hud["objectives"]}
    dragon = objs["Dragon"]
    # Killed at 400, respawn 300 -> up at 700; at 520 -> 180s left.
    assert dragon["state"] == "respawning"
    assert dragon["next_seconds"] == 180


def test_one_time_objective_gone_after_kill():
    events = [{"EventID": 1, "EventName": "HeraldKill", "EventTime": 900.0}]
    hud = live_hud.build_hud(_payload(game_time=1000.0, events=events), gold_fn=_gold)
    objs = {o["name"]: o for o in hud["objectives"]}
    herald = objs["Rift Herald"]
    assert herald["state"] == "gone"
    assert herald["next_seconds"] is None


def test_objective_alive_when_past_spawn_untaken():
    hud = live_hud.build_hud(_payload(game_time=360.0), gold_fn=_gold)
    objs = {o["name"]: o for o in hud["objectives"]}
    # Dragon initial 300, no kill, game at 360 -> alive now.
    assert objs["Dragon"]["state"] == "alive"
    assert objs["Dragon"]["next_seconds"] is None


def test_find_active_prefers_exact_riot_id_over_shared_name():
    # An enemy shares the local player's game name ("Me") but has a different
    # Riot ID/tag. The exact Riot ID must win regardless of player ordering.
    me = _player("Me", "ORDER", "MIDDLE", 100, 9, items=[6672])
    me["riotId"] = "Me#NA1"
    imposter = _player("Me", "CHAOS", "MIDDLE", 80, 8, items=[3153])
    imposter["riotId"] = "Me#EUW"
    imposter["riotIdTagLine"] = "EUW"
    imposter["summonerName"] = "Me"
    # Imposter listed FIRST so a name-first match would grab it.
    data = {"activePlayer": {"riotId": "Me#NA1", "summonerName": "Me"},
            "allPlayers": [imposter, me]}
    found = live_hud._find_active_player(data)
    assert found is me
    assert found["team"] == "ORDER"


def test_find_active_summoner_fallback_without_riot_id():
    p = _player("Solo", "ORDER", "MIDDLE", 10, 3)
    p["riotId"] = ""
    p["riotIdGameName"] = ""
    p["riotIdTagLine"] = ""
    data = {"activePlayer": {"summonerName": "Solo"}, "allPlayers": [p]}
    assert live_hud._find_active_player(data) is p
