"""Tests for the AngryBacteria Match-V5 -> RuneSync corpus adapter.

These lock in the privacy, queue-pooling, role-validation and skip-accounting
guarantees the ingest pipeline depends on. They use only synthetic Match-V5
shapes; no real dump or Riot identity is touched.
"""

import pytest

from corpus.angrybacteria import (
    POOLED_QUEUE_IDS,
    QUEUE_DOMAIN,
    SkipMatch,
    assert_no_identity_leak,
    build_report,
    build_timeline_payload,
    player_group_key,
    _deep_unwrap,
    _num,
)

SALT = b"\x00" * 32
_POSITIONS = ("TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY")


def _participant(participant_id, team_id, position, **overrides):
    base = {
        "participantId": participant_id,
        "teamId": team_id,
        "teamPosition": position,
        "puuid": f"riot-puuid-of-player-{participant_id}",
        "summonerName": f"RealName{participant_id}",
        "riotIdGameName": f"Real{participant_id}",
        "riotIdTagline": "EUW",
        "championId": 100 + participant_id,
        "championName": f"Champion{participant_id}",
        "win": team_id == 100,
        "kills": participant_id,
        "deaths": 11 - participant_id,
        "assists": participant_id + 1,
        "goldEarned": 10000 + participant_id,
        "totalMinionsKilled": 120,
        "neutralMinionsKilled": 20,
        "champLevel": 15,
        "totalDamageDealtToChampions": 15000,
        "damageDealtToObjectives": 5000,
        "damageDealtToTurrets": 1000,
        "totalDamageTaken": 20000,
        "damageSelfMitigated": 8000,
        "totalHeal": 3000,
        "visionScore": 25,
        "wardsPlaced": 10,
        "wardsKilled": 3,
        "item0": 3000,
        "item1": 0,
        "item2": 3157,
        "item3": 0,
        "item4": 0,
        "item5": 0,
        "item6": 3340,
    }
    base.update(overrides)
    return base


def _info(**overrides):
    participants = []
    for team_id in (100, 200):
        for slot, position in enumerate(_POSITIONS):
            pid = (0 if team_id == 100 else 5) + slot + 1
            participants.append(_participant(pid, team_id, position))
    info = {
        "queueId": 420,
        "mapId": 11,
        "gameId": 5602827182,
        "gameDuration": 1800,
        "gameCreation": {"$numberLong": "1724067103756"},
        "gameMode": "CLASSIC",
        "gameVersion": "14.10.581.1234",
        "participants": participants,
    }
    info.update(overrides)
    return info


# --- number unwrapping -----------------------------------------------------

def test_num_unwraps_extended_json_and_plain():
    assert _num({"$numberLong": "1724067103756"}) == 1724067103756
    assert _num({"$numberInt": "420"}) == 420
    assert _num(11) == 11
    assert _num(True) == 1
    assert _num(None, default=-1) == -1
    assert _num({"weird": 1}, default=7) == 7


# --- happy path ------------------------------------------------------------

def test_build_report_maps_roles_and_pools_queue():
    report = build_report(_info(), SALT)

    assert report["match"]["game_id"] == 5602827182
    assert report["match"]["queue_id"] in POOLED_QUEUE_IDS
    assert QUEUE_DOMAIN[report["match"]["queue_id"]] == "ranked_solo"
    assert len(report["participants"]) == 10

    roles = {p["participant_id"]: p["role"] for p in report["participants"]}
    assert roles[1] == "top" and roles[2] == "jungle" and roles[3] == "mid"
    assert roles[4] == "bot" and roles[5] == "support"
    # both teams carry the same canonical five roles
    assert roles[6] == "top" and roles[10] == "support"

    # cs = lane minions + neutral monsters
    assert all(p["cs"] == 140 for p in report["participants"])
    # v1 scoring ran for every participant
    assert len(report["scores"]) == 10


def test_build_report_never_persists_real_identity():
    report = build_report(_info(), SALT)
    for player in report["participants"]:
        assert len(player["puuid"]) == 64
        assert all(c in "0123456789abcdef" for c in player["puuid"])
        assert player["summoner_name"].startswith("Player ")
    # the loud guard agrees
    assert_no_identity_leak(report)


# --- queue / map / structure filtering ------------------------------------

@pytest.mark.parametrize("queue_id", [450, 1700, 900, 0])
def test_build_report_rejects_unpooled_queue(queue_id):
    with pytest.raises(SkipMatch) as exc:
        build_report(_info(queueId=queue_id), SALT)
    assert exc.value.reason.startswith("queue_not_pooled")


@pytest.mark.parametrize("queue_id,domain", [
    (420, "ranked_solo"), (400, "normal_draft"), (440, "ranked_flex"),
])
def test_build_report_accepts_all_pooled_queues(queue_id, domain):
    report = build_report(_info(queueId=queue_id), SALT)
    assert QUEUE_DOMAIN[report["match"]["queue_id"]] == domain


def test_build_report_rejects_non_rift_map():
    with pytest.raises(SkipMatch) as exc:
        build_report(_info(mapId=12), SALT)
    assert exc.value.reason.startswith("map_not_rift")


def test_build_report_rejects_short_game():
    with pytest.raises(SkipMatch) as exc:
        build_report(_info(gameDuration=299), SALT)
    assert exc.value.reason.startswith("too_short")


def test_build_report_rejects_early_surrender():
    info = _info()
    info["participants"][0]["gameEndedInEarlySurrender"] = True
    with pytest.raises(SkipMatch) as exc:
        build_report(info, SALT)
    assert exc.value.reason == "early_surrender"


def test_build_report_rejects_wrong_player_count():
    info = _info()
    info["participants"] = info["participants"][:9]
    with pytest.raises(SkipMatch) as exc:
        build_report(info, SALT)
    assert exc.value.reason.startswith("participant_count")


def test_build_report_rejects_non_canonical_position():
    info = _info()
    info["participants"][3]["teamPosition"] = ""
    with pytest.raises(SkipMatch) as exc:
        build_report(info, SALT)
    assert exc.value.reason.startswith("non_canonical_position")


def test_build_report_rejects_incomplete_team_roles():
    info = _info()
    # give team 100 two junglers and no top -> incomplete canonical set
    info["participants"][0]["teamPosition"] = "JUNGLE"
    with pytest.raises(SkipMatch) as exc:
        build_report(info, SALT)
    assert exc.value.reason.startswith("incomplete_roles_team")


def test_build_report_rejects_missing_puuid():
    info = _info()
    info["participants"][2]["puuid"] = ""
    with pytest.raises(SkipMatch) as exc:
        build_report(info, SALT)
    assert exc.value.reason == "participant_missing_puuid"


# --- identity hashing ------------------------------------------------------

def test_player_group_key_is_stable_and_salt_dependent():
    a = player_group_key("puuid-x", SALT)
    b = player_group_key("puuid-x", SALT)
    assert a == b and len(a) == 64
    # a different player differs
    assert player_group_key("puuid-y", SALT) != a
    # a different salt differs (non-reversible without the salt)
    assert player_group_key("puuid-x", b"\x01" * 32) != a


def test_player_group_key_rejects_empty():
    with pytest.raises(SkipMatch):
        player_group_key("", SALT)


def test_assert_no_identity_leak_catches_raw_puuid():
    report = {"participants": [
        {"puuid": "riot-raw-puuid-not-hex", "summoner_name": "Player 1"},
    ]}
    with pytest.raises(ValueError):
        assert_no_identity_leak(report)


def test_assert_no_identity_leak_catches_real_name():
    report = {"participants": [
        {"puuid": "a" * 64, "summoner_name": "SomeRealName"},
    ]}
    with pytest.raises(ValueError):
        assert_no_identity_leak(report)


# --- timeline payload ------------------------------------------------------

def test_build_timeline_payload_unwraps_frame_timestamps():
    timeline_info = {"frames": [
        {"timestamp": {"$numberLong": "60000"}, "participantFrames": {"1": {}}},
        {"timestamp": 120000, "events": []},
        "not-a-frame",
    ]}
    payload = build_timeline_payload(timeline_info)
    frames = payload["timeline"]["frames"]
    assert len(frames) == 2
    assert frames[0]["timestamp"] == 60000
    assert frames[1]["timestamp"] == 120000


def test_build_timeline_payload_deep_unwraps_nested_numbers():
    # A dump that wraps the nested numbers score_features depends on: event
    # timestamps and participant-frame numerics must survive as plain numbers,
    # not extended-JSON dicts (which canonical_timeline_events silently drops).
    timeline_info = {"frames": [{
        "timestamp": {"$numberLong": "60000"},
        "participantFrames": {
            "1": {
                "participantId": {"$numberInt": "1"},
                "totalGold": {"$numberLong": "5000"},
                "position": {"x": {"$numberInt": "1200"},
                             "y": {"$numberDouble": "3400.5"}},
            },
        },
        "events": [
            {"type": "CHAMPION_KILL",
             "timestamp": {"$numberLong": "58000"},
             "killerId": {"$numberInt": "1"},
             "victimId": {"$numberInt": "6"},
             "realTimestamp": {"$numberLong": "1724067103756"}},
        ],
    }]}
    frame = build_timeline_payload(timeline_info)["timeline"]["frames"][0]
    assert frame["timestamp"] == 60000
    pf = frame["participantFrames"]["1"]
    assert pf["participantId"] == 1
    assert pf["totalGold"] == 5000
    assert pf["position"] == {"x": 1200, "y": 3400.5}
    assert isinstance(pf["position"]["y"], float)
    event = frame["events"][0]
    assert event["timestamp"] == 58000 and isinstance(event["timestamp"], int)
    assert event["killerId"] == 1 and event["victimId"] == 6
    assert event["realTimestamp"] == 1724067103756


def test_deep_unwrap_passes_raw_and_malformed_through():
    # Raw ints (how the AngryBacteria dump actually stores feature-relevant
    # numbers) are a no-op; a non-number single-key dict is left intact.
    assert _deep_unwrap({"a": 1, "b": [2, {"$numberInt": "3"}]}) == {
        "a": 1, "b": [2, 3]}
    assert _deep_unwrap({"$ref": "keep"}) == {"$ref": "keep"}
    assert _deep_unwrap({"$numberInt": "x"}) == {"$numberInt": "x"}
