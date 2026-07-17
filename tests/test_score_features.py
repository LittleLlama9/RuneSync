"""Tests for score_features.py -- the source-aware DAEMON Score v2

feature/evidence extraction layer.

Sections:
  1. Capability detection (all four tiers, missing != zero/full).
  2. Canonical event/frame normalization (LCU/Match-V5 + Live Client).
  3. K'Sante regression (game 5602827182): first blood, turret/grub secures,
     Seraphine's raw vision/deaths not becoming influence, Vel'Koz's raw
     turret damage not becoming objective credit, sustained lane-lead
     conversion.
  4. Sion abstention regression (game 5601631110, 8:30 / 510s): aggregate-
     only evidence, `abstain=True`, no fabricated event-anchored families.
  5. Invariants/counterfactuals: unconverted ward, raw-stat bump, productive
     secure, extra untraded death, role symmetry, participant-order
     invariance, bounded outputs.
  6. No win/result leakage; determinism/content-hash stability.
  7. End-to-end `extract_game_features` against a real (tmp_path)
     HistoryStore.
"""

import copy
import json
from pathlib import Path

import pytest

import score_features as sf
from history_store import HistoryStore

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name):
    with open(FIXTURES / name, encoding="utf-8") as handle:
        return json.load(handle)


KSANTE_GAME_ID = 5602827182
KSANTE_DURATION = 1861
SION_GAME_ID = 5601631110
SION_DURATION = 510


def _ksante_participants():
    return copy.deepcopy(_load("aggregate_participants_5602827182.json"))["participants"]


def _ksante_timeline():
    fixture = _load("lcu_timeline_5602827182.json")
    return {"frames": fixture["frames"]}


def _sion_participants():
    return copy.deepcopy(_load("aggregate_participants_5601631110.json"))["participants"]


def _iter_features(features):
    return features["participants"].items()


# ── 1. capability detection ─────────────────────────────────────────────────

class _FakeStore:
    """Minimal duck-typed double for detect_capabilities -- no sqlite needed."""

    def __init__(self, has_match_v5=False, has_lcu=False,
                 live_sessions=None, known_game=True):
        self._has_match_v5 = has_match_v5
        self._has_lcu = has_lcu
        self._live_sessions = live_sessions or []
        self._known_game = known_game

    def has_timeline_payload(self, game_id, source):
        if source == sf.MATCH_V5:
            return self._has_match_v5
        if source == sf.LCU_TIMELINE:
            return self._has_lcu
        return False

    def list_live_capture_sessions(self):
        return self._live_sessions

    def has_game(self, game_id):
        return self._known_game


def test_detect_capabilities_all_tiers_absent_except_aggregate():
    store = _FakeStore()
    caps = sf.detect_capabilities(store, 1)

    assert caps.as_dict() == {
        "match_v5": False, "lcu_timeline": False,
        "live_client": False, "aggregate": True,
    }
    assert caps.best_source() == sf.AGGREGATE
    assert caps.has_event_timeline() is False


def test_detect_capabilities_prefers_match_v5_over_lcu_timeline():
    store = _FakeStore(has_match_v5=True, has_lcu=True)
    caps = sf.detect_capabilities(store, 1)

    assert caps.match_v5 is True and caps.lcu_timeline is True
    assert caps.best_source() == sf.MATCH_V5


def test_detect_capabilities_lcu_timeline_without_match_v5():
    store = _FakeStore(has_lcu=True)
    caps = sf.detect_capabilities(store, 1)

    assert caps.best_source() == sf.LCU_TIMELINE
    assert caps.has_event_timeline() is True


def test_detect_capabilities_live_client_uses_real_terminal_statuses():
    store_incomplete = _FakeStore(live_sessions=[
        {"game_id": 1, "status": "active", "completeness": 0.0},
    ])
    store_other_game = _FakeStore(live_sessions=[
        {"game_id": 999, "status": "completed", "completeness": 1.0},
    ])
    store_ok = _FakeStore(live_sessions=[
        {"game_id": 1, "status": "partial_endpoint_lost", "completeness": 0.4},
    ])
    store_completed = _FakeStore(live_sessions=[
        {"game_id": 1, "status": "completed", "completeness": 1.0},
    ])

    assert sf.detect_capabilities(store_incomplete, 1).live_client is False
    assert sf.detect_capabilities(store_other_game, 1).live_client is False
    assert sf.detect_capabilities(store_ok, 1).live_client is True
    assert sf.detect_capabilities(store_completed, 1).live_client is True


def test_best_source_prefers_more_complete_evidence_then_richer_tier():
    partial_match = sf.EvidenceCapabilities(
        match_v5=True, lcu_timeline=True,
        match_v5_completeness=0.5, lcu_timeline_completeness=1.0,
    )
    equal = sf.EvidenceCapabilities(
        match_v5=True, lcu_timeline=True,
        match_v5_completeness=1.0, lcu_timeline_completeness=1.0,
    )
    partial_live = sf.EvidenceCapabilities(
        live_client=True, live_client_completeness=0.2,
    )

    assert partial_match.best_source() == sf.LCU_TIMELINE
    assert equal.best_source() == sf.MATCH_V5
    assert partial_live.best_source() == sf.LIVE_CLIENT


def test_missing_evidence_is_never_silently_zero_or_full():
    # No timeline tiers -> compute_feature_set must mark families
    # unavailable, not zero, and must not claim match_v5/lcu_timeline exist.
    caps = sf.EvidenceCapabilities(aggregate=True)
    participants = _sion_participants()

    features, _ = sf.compute_feature_set(
        participants, SION_DURATION, caps, sf.AGGREGATE,
    )

    assert features["capabilities"] == {
        "match_v5": False, "lcu_timeline": False,
        "live_client": False, "aggregate": True,
    }
    for pid, pf in _iter_features(features):
        assert pf["fight_influence"] is None
        assert pf["objective_participation"] is None
        assert pf["vision_influence"] == {
            "available": False,
            "reason": "no event-level timeline for this evidence tier",
        }
        assert pf["resource_conversion"]["available"] is False


# ── 2. canonical event/frame normalization ──────────────────────────────────

def test_canonical_timeline_events_normalizes_and_sorts_all_kinds():
    frames = [
        {
            "timestamp": 5000,
            "participantFrames": {},
            "events": [
                {
                    "type": "ELITE_MONSTER_KILL", "timestamp": 300000,
                    "killerId": 2, "killerTeamId": 100,
                    "assistingParticipantIds": [1], "monsterType": "HORDE",
                },
                {
                    "type": "CHAMPION_KILL", "timestamp": 60000,
                    "killerId": 1, "victimId": 6, "assistingParticipantIds": [2],
                },
            ],
        },
        {
            "timestamp": 10000,
            "participantFrames": {},
            "events": [
                {
                    "type": "BUILDING_KILL", "timestamp": 500000,
                    "killerId": 1, "assistingParticipantIds": [],
                    "teamId": 200, "buildingType": "TOWER_BUILDING",
                    "laneType": "MID_LANE",
                },
                {"type": "SKILL_LEVEL_UP", "timestamp": 1000, "skillSlot": 1},
            ],
        },
    ]

    events = sf.canonical_timeline_events(frames, sf.LCU_TIMELINE)

    # unmodeled event kinds (skill level ups, item purchases, ...) are dropped
    assert len(events) == 3
    # sorted ascending by timestamp regardless of frame order
    assert [e["t_ms"] for e in events] == [60000.0, 300000.0, 500000.0]
    kill, monster, building = events
    assert kill == {
        "kind": "champion_kill", "killer": 1, "victim": 6, "assists": [2],
        "position": None, "t_ms": 60000.0, "phase": "early", "source": "lcu_timeline",
    }
    assert monster["kind"] == "elite_monster_kill"
    assert monster["monster_type"] == "HORDE"
    assert building["kind"] == "building_kill"
    assert building["building_type"] == "TOWER_BUILDING"


def test_canonical_timeline_events_drops_malformed_entries():
    frames = [
        {"timestamp": 0, "events": [{"type": "CHAMPION_KILL"}]},  # no timestamp
        {"timestamp": 0, "events": ["not-a-dict"]},
        "not-a-frame",
    ]
    assert sf.canonical_timeline_events(frames, sf.LCU_TIMELINE) == []


def test_canonical_timeline_frames_extracts_per_participant_series():
    frames = [
        {
            "timestamp": 60000,
            "participantFrames": {
                "1": {"totalGold": 500, "xp": 200, "level": 2, "minionsKilled": 4,
                      "position": {"x": 100, "y": 200}},
            },
        },
        {
            "timestamp": 0,
            "participantFrames": {
                "1": {"totalGold": 0, "xp": 0, "level": 1, "minionsKilled": 0},
            },
        },
    ]

    frames_by_pid = sf.canonical_timeline_frames(frames)

    assert list(frames_by_pid.keys()) == [1]
    series = frames_by_pid[1]
    # sorted ascending by time even though input frames were out of order
    assert [s["t_ms"] for s in series] == [0.0, 60000.0]
    assert series[1]["gold"] == 500
    assert series[1]["position"] == {"x": 100, "y": 200}
    assert series[0]["position"] is None


def test_canonical_live_client_events_maps_named_events_and_drops_unresolved():
    name_to_pid = {"Player One": 1, "Player Two": 6}
    events = [
        {
            "event_id": 1, "event_time": 120.0, "event_type": "ChampionKill",
            "payload": {
                "EventName": "ChampionKill", "KillerName": "Player One",
                "VictimName": "Player Two", "Assisters": ["Unknown Player"],
            },
        },
        {
            "event_id": 2, "event_time": 300.0, "event_type": "TurretKilled",
            "payload": {"EventName": "TurretKilled", "KillerName": "Player One"},
        },
        {
            "event_id": 3, "event_time": 10.0, "event_type": "DragonKill",
            "payload": {
                "EventName": "DragonKill", "KillerName": "Player One",
                "DragonType": "Fire",
            },
        },
    ]

    canonical = sf.canonical_live_client_events(events, name_to_pid)

    assert [e["t_ms"] for e in canonical] == [10000.0, 120000.0, 300000.0]
    dragon, kill, turret = canonical
    assert dragon["kind"] == "elite_monster_kill" and dragon["monster_type"] == "DRAGON"
    assert kill["kind"] == "champion_kill"
    assert kill["killer"] == 1 and kill["victim"] == 6
    # "Unknown Player" (unresolved assister) must never silently attach
    assert kill["assists"] == []
    assert turret["kind"] == "building_kill" and turret["killer"] == 1


def test_map_live_client_names_to_participants_requires_unique_champion_match():
    participants = [
        {"participant_id": 1, "champion_name": "Ahri"},
        {"participant_id": 2, "champion_name": "Ahri"},  # ambiguous duplicate
        {"participant_id": 3, "champion_name": "Garen"},
    ]
    all_game_players = [
        {"championName": "Ahri", "summonerName": "P1"},
        {"championName": "Garen", "riotIdGameName": "P3", "summonerName": ""},
        {"championName": "Zed", "summonerName": "NoMatch"},
    ]

    mapping = sf.map_live_client_names_to_participants(all_game_players, participants)

    # Ahri is ambiguous (2 participants) so it must not be mapped at all
    assert "P1" not in mapping
    assert mapping["P3"] == 3
    assert "NoMatch" not in mapping


def test_live_client_snapshots_exclude_active_player_only_fields():
    participants = _synthetic_participants()
    snapshots = [{
        "snapshot_time": 120.0,
        "payload": {
            "game_time": 120.0,
            "active_player": {
                "active_player_only": True,
                "currentGold": 9999,
                "championStats": {"attackDamage": 999},
            },
            "players": [{
                "summonerName": "Player One",
                "championName": "Champ1",
                "level": 4,
                "isDead": False,
                "respawnTimer": 0,
                "items": [{"itemID": 1001}],
                "scores": {
                    "kills": 1, "deaths": 0, "assists": 2,
                    "creepScore": 20, "wardScore": 3,
                },
            }],
        },
    }]
    name_map = sf.map_live_client_names_to_participants(
        snapshots[0]["payload"]["players"], participants,
    )

    series = sf.canonical_live_client_snapshots(snapshots, name_map)

    assert series[1][0]["level"] == 4
    assert series[1][0]["cs"] == 20
    assert "currentGold" not in json.dumps(series)
    assert "championStats" not in json.dumps(series)


# ── 3. K'Sante regression (game 5602827182) ─────────────────────────────────

@pytest.fixture(scope="module")
def ksante_features():
    caps = sf.EvidenceCapabilities(lcu_timeline=True)
    features, evidence = sf.compute_feature_set(
        _ksante_participants(), KSANTE_DURATION, caps, sf.LCU_TIMELINE,
        timeline=_ksante_timeline(),
    )
    return features, evidence


def test_ksante_first_blood_and_objective_secures(ksante_features):
    features, _ = ksante_features
    ksante = features["participants"]["8"]

    assert ksante["fight_influence"]["first_blood"] is True
    obj = ksante["objective_participation"]
    assert obj["turret_kills"] == 2
    assert obj["grub_secures"] == 2
    assert ksante["structure_pressure"]["structure_secures"] == 2


def test_ksante_sustained_lane_lead_is_converted(ksante_features):
    features, _ = ksante_features
    conversion = features["participants"]["8"]["resource_conversion"]

    assert conversion["available"] is True
    assert conversion["lane_opponent"] == 3  # Yone, the real mid lane opponent
    assert conversion["lead_windows"] > 0
    assert conversion["converted_lead_windows"] > 0


def test_seraphine_deaths_and_raw_vision_do_not_become_influence(ksante_features):
    features, _ = ksante_features
    seraphine = features["participants"]["10"]

    assert seraphine["fight_influence"]["death_events"] == 15
    assert seraphine["death_tempo"]["death_count"] == 15
    # she has zero direct map-event involvement in the fixture
    obj = seraphine["objective_participation"]
    assert obj["turret_kills"] == 0 and obj["grub_secures"] == 0
    assert obj["turret_assists"] == 0 and obj["grub_assists"] == 0
    # raw vision_score is real (79) but must not surface as influence:
    # the lcu_timeline tier carries no ward events at all.
    assert seraphine["raw"]["vision_score"] == 79
    assert seraphine["vision_influence"] == {
        "available": False,
        "reason": (
            "This evidence source does not carry ward/item events "
            "(verified for the LCU historical timeline); raw vision "
            "stats remain in `raw` but must not be treated as influence."
        ),
    }


def test_velkoz_raw_turret_damage_is_not_objective_credit(ksante_features):
    features, _ = ksante_features
    velkoz = features["participants"]["9"]

    assert velkoz["raw"]["damage_to_turrets"] == 5609
    obj = velkoz["objective_participation"]
    assert obj["turret_kills"] == 0
    assert obj["turret_assists"] == 0
    assert obj["grub_secures"] == 0 and obj["grub_assists"] == 0
    assert obj["epic_monster_secures"] == 0 and obj["epic_monster_assists"] == 0
    assert velkoz["structure_pressure"]["structure_secures"] == 0


def test_ksante_fixture_features_contain_no_outcome_fields(ksante_features):
    features, _ = ksante_features
    dumped = json.dumps(features)

    assert '"win"' not in dumped
    assert '"local_win"' not in dumped
    for _, pf in _iter_features(features):
        assert "win" not in pf["raw"]


# ── 4. Sion 8:30 abstention regression (game 5601631110) ───────────────────

def test_sion_short_game_abstains_and_produces_no_fabricated_event_families():
    caps = sf.EvidenceCapabilities()  # only aggregate is ever unconditionally true
    features, _ = sf.compute_feature_set(
        _sion_participants(), SION_DURATION, caps, sf.AGGREGATE,
    )

    assert features["duration_seconds"] == SION_DURATION
    assert features["abstain"] is True
    assert features["abstain_reason"] == "short_game"
    assert len(features["participants"]) == 10

    for pid, pf in _iter_features(features):
        assert pf["fight_influence"] is None
        assert pf["objective_participation"] is None
        assert pf["structure_pressure"] is None
        assert pf["enablement_suppression"] is None
        assert pf["death_tempo"] is None
        assert pf["phase_breakdown"] is None
        assert pf["resource_conversion"]["available"] is False
        assert pf["vision_influence"]["available"] is False
        assert pf["raw"]["kills"] is not None  # raw aggregate is still populated


def test_sion_local_participant_is_not_treated_as_confident_vector():
    caps = sf.EvidenceCapabilities()
    features, _ = sf.compute_feature_set(
        _sion_participants(), SION_DURATION, caps, sf.AGGREGATE,
    )
    sion = features["participants"]["8"]

    assert features["abstain"] is True
    assert sion["raw"]["kills"] == 1 and sion["raw"]["deaths"] == 1
    assert sion["fight_influence"] is None  # never fabricated from 1/1/0 raw stats


def test_longer_duration_with_same_evidence_does_not_abstain():
    caps = sf.EvidenceCapabilities()
    features, _ = sf.compute_feature_set(
        _sion_participants(), 1800.0, caps, sf.AGGREGATE,
    )
    assert features["abstain"] is False
    assert features["abstain_reason"] is None


# ── 5. invariants / counterfactuals ──────────────────────────────────────────

def _synthetic_participants():
    participants = []
    for pid in range(1, 11):
        team = 100 if pid <= 5 else 200
        role = ["top", "jungle", "mid", "bot", "support"][(pid - 1) % 5]
        participants.append({
            "participant_id": pid, "team_id": team, "role": role,
            "champion_id": pid, "champion_name": f"Champ{pid}",
            "win": team == 100,
            "kills": 3, "deaths": 3, "assists": 3, "gold_earned": 8000,
            "cs": 120, "vision_score": 20, "wards_placed": 8, "wards_killed": 2,
            "damage_to_champions": 12000, "damage_to_objectives": 3000,
            "damage_to_turrets": 1000,
        })
    return participants


def _synthetic_frame(t_ms, gold_by_pid):
    return {
        "timestamp": t_ms,
        "participantFrames": {
            str(pid): {
                "totalGold": gold, "xp": gold, "level": 6,
                "minionsKilled": 20, "position": {"x": pid * 10, "y": pid * 10},
            }
            for pid, gold in gold_by_pid.items()
        },
        "events": [],
    }


def _base_synthetic_timeline():
    """A small, controllable 10-participant timeline for invariant tests.

    Participant 1 (team 100) gets first blood on participant 6 (team 200) at
    t=60s with an assist from participant 2; participant 6 dies untraded.
    Participant 1 also secures a turret at t=120s (BUILDING_KILL) with no raw
    damage_to_turrets change required -- proving the credit comes only from
    the event, not the aggregate stat.
    """
    frames = [
        _synthetic_frame(0, {pid: 500 for pid in range(1, 11)}),
        _synthetic_frame(60_000, {pid: 500 + pid * 50 for pid in range(1, 11)}),
        _synthetic_frame(120_000, {pid: 1000 + pid * 50 for pid in range(1, 11)}),
    ]
    frames[1]["events"] = [
        {
            "type": "CHAMPION_KILL", "timestamp": 60_000,
            "killerId": 1, "victimId": 6, "assistingParticipantIds": [2],
            "position": {"x": 100, "y": 100},
        },
    ]
    frames[2]["events"] = [
        {
            "type": "BUILDING_KILL", "timestamp": 120_000,
            "killerId": 1, "assistingParticipantIds": [],
            "teamId": 200, "buildingType": "TOWER_BUILDING", "laneType": "TOP_LANE",
            "position": {"x": 100, "y": 100},
        },
    ]
    return {"frames": frames}


def _compute(participants=None, timeline=None, duration=1800.0):
    caps = sf.EvidenceCapabilities(lcu_timeline=True)
    participants = participants if participants is not None else _synthetic_participants()
    timeline = timeline if timeline is not None else _base_synthetic_timeline()
    features, _ = sf.compute_feature_set(
        participants, duration, caps, sf.LCU_TIMELINE, timeline=timeline,
    )
    return features


def test_unconverted_ward_does_not_improve_vision_actionable_rate():
    timeline = copy.deepcopy(_base_synthetic_timeline())
    baseline = _compute(timeline=timeline)

    # add a ward far from any followup event (300s, nowhere near the fight at
    # 60s/120s) -- it should count toward totals but never as "actionable"
    timeline["frames"].append({
        "timestamp": 300_000, "participantFrames": {}, "events": [
            {"type": "WARD_PLACED_EVENT", "timestamp": 300_000, "creatorId": 3},
        ],
    })
    with_unconverted_ward = _compute(timeline=timeline)

    base_vision = baseline["participants"]["3"]["vision_influence"]
    new_vision = with_unconverted_ward["participants"]["3"]["vision_influence"]
    assert base_vision["available"] is False  # base timeline has no ward events at all
    assert new_vision["available"] is True
    assert new_vision["actionable_wards"] == 0
    assert new_vision["wards_placed_events"] == 1
    assert new_vision["vision_actionable_rate"] == 0.0


def test_converted_ward_does_increase_actionable_rate():
    timeline = copy.deepcopy(_base_synthetic_timeline())
    # place a ward by participant 2 right before the t=120s turret secure,
    # within the 20s actionable window -> should count as actionable
    timeline["frames"][2]["events"].append(
        {
            "type": "WARD_PLACED_EVENT", "timestamp": 115_000,
            "creatorId": 2, "position": {"x": 120, "y": 120},
        },
    )
    features = _compute(timeline=timeline)
    vision = features["participants"]["2"]["vision_influence"]

    assert vision["available"] is True
    assert vision["actionable_wards"] == 1
    assert vision["vision_actionable_rate"] == 1.0


def test_unrelated_or_enemy_followup_does_not_convert_ward():
    team_of = {pid: 100 if pid <= 5 else 200 for pid in range(1, 11)}
    ward = {
        "kind": "ward_placed", "actor": 1, "t_ms": 1000.0,
        "phase": "early", "source": sf.MATCH_V5,
        "position": {"x": 100, "y": 100},
    }
    enemy_near = {
        "kind": "champion_kill", "killer": 6, "victim": 2, "assists": [],
        "t_ms": 2000.0, "phase": "early", "source": sf.MATCH_V5,
        "position": {"x": 120, "y": 120},
    }
    ally_far = {
        "kind": "champion_kill", "killer": 2, "victim": 6, "assists": [],
        "t_ms": 3000.0, "phase": "early", "source": sf.MATCH_V5,
        "position": {"x": 12000, "y": 12000},
    }
    ally_near = {
        "kind": "champion_kill", "killer": 2, "victim": 6, "assists": [],
        "t_ms": 4000.0, "phase": "early", "source": sf.MATCH_V5,
        "position": {"x": 140, "y": 140},
    }

    unrelated = sf._vision_influence(
        1, team_of, [ward, enemy_near, ally_far], True, {},
    )
    related = sf._vision_influence(
        1, team_of, [ward, enemy_near, ally_far, ally_near], True, {},
    )

    assert unrelated["actionable_wards"] == 0
    assert related["actionable_wards"] == 1


def test_cross_map_kill_does_not_mark_death_traded():
    team_of = {pid: 100 if pid <= 5 else 200 for pid in range(1, 11)}
    death = {
        "kind": "champion_kill", "killer": 6, "victim": 1, "assists": [],
        "t_ms": 1000.0, "phase": "early", "source": sf.LCU_TIMELINE,
        "position": {"x": 1000, "y": 1000},
    }
    cross_map = {
        "kind": "champion_kill", "killer": 2, "victim": 7, "assists": [],
        "t_ms": 5000.0, "phase": "early", "source": sf.LCU_TIMELINE,
        "position": {"x": 13000, "y": 13000},
    }
    nearby = copy.deepcopy(cross_map)
    nearby["position"] = {"x": 1100, "y": 1100}

    unrelated = sf._fight_influence(1, team_of, [death, cross_map])
    related = sf._fight_influence(1, team_of, [death, nearby])

    assert unrelated["traded_deaths"] == 0
    assert unrelated["untraded_deaths"] == 1
    assert related["traded_deaths"] == 1


def test_phase_breakdown_has_exactly_one_real_first_blood():
    team_of = {pid: 100 if pid <= 5 else 200 for pid in range(1, 11)}
    events = [
        {
            "kind": "champion_kill", "killer": 1, "victim": 6, "assists": [],
            "t_ms": 300_000.0, "phase": "early", "source": sf.LCU_TIMELINE,
            "position": {"x": 100, "y": 100},
        },
        {
            "kind": "champion_kill", "killer": 4, "victim": 9, "assists": [],
            "t_ms": 1_080_000.0, "phase": "mid", "source": sf.LCU_TIMELINE,
            "position": {"x": 200, "y": 200},
        },
    ]

    first = sf._phase_breakdown(1, team_of, events)
    later = sf._phase_breakdown(4, team_of, events)

    assert first["early"]["fight"]["first_blood"] is True
    assert later["mid"]["fight"]["first_blood"] is False


def test_raw_stat_bump_never_changes_objective_or_structure_credit():
    baseline = _compute()
    bumped_participants = _synthetic_participants()
    for row in bumped_participants:
        row["damage_to_turrets"] = 99_999
        row["damage_to_objectives"] = 99_999
        row["vision_score"] = 500
    bumped = _compute(participants=bumped_participants)

    for pid in map(str, range(1, 11)):
        assert (bumped["participants"][pid]["objective_participation"]
                == baseline["participants"][pid]["objective_participation"])
        assert (bumped["participants"][pid]["structure_pressure"]["structure_secures"]
                == baseline["participants"][pid]["structure_pressure"]["structure_secures"])


def test_productive_secure_increases_objective_credit():
    baseline = _compute()
    timeline = copy.deepcopy(_base_synthetic_timeline())
    timeline["frames"][2]["events"].append({
        "type": "ELITE_MONSTER_KILL", "timestamp": 121_000,
        "killerId": 1, "killerTeamId": 100, "assistingParticipantIds": [2],
        "monsterType": "HORDE",
    })
    improved = _compute(timeline=timeline)

    base_obj = baseline["participants"]["1"]["objective_participation"]
    new_obj = improved["participants"]["1"]["objective_participation"]
    assert new_obj["grub_secures"] == base_obj["grub_secures"] + 1
    assert (improved["participants"]["2"]["objective_participation"]["grub_assists"]
            == baseline["participants"]["2"]["objective_participation"]["grub_assists"] + 1)


def test_resource_conversion_never_uses_event_at_frame_timestamp():
    participants = _synthetic_participants()
    frames = [
        _synthetic_frame(0, {pid: 500 for pid in range(1, 11)}),
        _synthetic_frame(
            60_000,
            {pid: (1200 if pid == 1 else 500) for pid in range(1, 11)},
        ),
    ]
    frames[1]["events"] = [{
        "type": "CHAMPION_KILL", "timestamp": 60_000,
        "killerId": 1, "victimId": 6, "assistingParticipantIds": [],
        "position": {"x": 100, "y": 100},
    }]

    features = _compute(participants=participants, timeline={"frames": frames})
    conversion = features["participants"]["1"]["resource_conversion"]

    assert conversion["lead_windows"] == 1
    assert conversion["converted_lead_windows"] == 0


def test_extra_untraded_death_worsens_and_never_improves_a_positive_metric():
    baseline = _compute()
    timeline = copy.deepcopy(_base_synthetic_timeline())
    # a second, untraded death for participant 6 far from any other event
    timeline["frames"].append({
        "timestamp": 400_000, "participantFrames": {}, "events": [
            {
                "type": "CHAMPION_KILL", "timestamp": 400_000,
                "killerId": 3, "victimId": 6, "assistingParticipantIds": [],
            },
        ],
    })
    worsened = _compute(timeline=timeline)

    base_fight = baseline["participants"]["6"]["fight_influence"]
    new_fight = worsened["participants"]["6"]["fight_influence"]
    assert new_fight["death_events"] == base_fight["death_events"] + 1
    assert new_fight["untraded_deaths"] == base_fight["untraded_deaths"] + 1
    assert new_fight["kill_events"] == base_fight["kill_events"]
    assert new_fight["assist_events"] == base_fight["assist_events"]
    # the enemy team's suppression numbers cannot improve from our own death
    base_supp = baseline["participants"]["1"]["enablement_suppression"]
    new_supp = worsened["participants"]["1"]["enablement_suppression"]
    assert new_supp["suppression_events"] == base_supp["suppression_events"]


def test_role_symmetry_relabeling_role_does_not_change_event_anchored_families():
    """Relabeling a participant's `role` (identical event pattern otherwise)
    must not change any event-anchored family (fight/objective/structure/
    enablement/death_tempo) -- those are keyed only by participant/team, not
    role. Only `baseline.role` (a shrinkage grouping key) may differ, and
    `resource_conversion` may legitimately differ since it deliberately
    looks up a same-role lane opponent.
    """
    original = _synthetic_participants()
    relabeled = copy.deepcopy(original)
    for row in relabeled:
        if row["participant_id"] == 1:
            row["role"] = "jungle" if row["role"] != "jungle" else "support"

    timeline = _base_synthetic_timeline()
    features_original = _compute(participants=original, timeline=copy.deepcopy(timeline))
    features_relabeled = _compute(participants=relabeled, timeline=copy.deepcopy(timeline))

    p1_original = features_original["participants"]["1"]
    p1_relabeled = features_relabeled["participants"]["1"]
    for family in (
        "fight_influence", "objective_participation", "structure_pressure",
        "enablement_suppression", "death_tempo",
    ):
        assert p1_original[family] == p1_relabeled[family]
    assert p1_original["baseline"]["role"] != p1_relabeled["baseline"]["role"]


def test_participant_order_invariance():
    participants = _synthetic_participants()
    shuffled = list(reversed(participants))

    forward = _compute(participants=participants)
    backward = _compute(participants=shuffled)

    assert forward["participants"] == backward["participants"]
    assert json.dumps(forward, sort_keys=True) == json.dumps(backward, sort_keys=True)


def test_bounded_outputs_rates_in_unit_interval_and_counts_nonnegative():
    for features in (
        _compute(),
        sf.compute_feature_set(
            _ksante_participants(), KSANTE_DURATION,
            sf.EvidenceCapabilities(lcu_timeline=True), sf.LCU_TIMELINE,
            timeline=_ksante_timeline(),
        )[0],
    ):
        for pid, pf in _iter_features(features):
            fight = pf.get("fight_influence")
            if fight and fight.get("event_kill_participation") is not None:
                assert 0.0 <= fight["event_kill_participation"] <= 1.0
            vision = pf.get("vision_influence")
            if vision and vision.get("available") and vision.get("vision_actionable_rate") is not None:
                assert 0.0 <= vision["vision_actionable_rate"] <= 1.0
            conversion = pf.get("resource_conversion")
            if conversion and conversion.get("available") and conversion.get("conversion_rate") is not None:
                assert 0.0 <= conversion["conversion_rate"] <= 1.0
            for family in ("fight_influence", "objective_participation", "death_tempo"):
                block = pf.get(family)
                if not block:
                    continue
                for key, value in block.items():
                    if isinstance(value, bool):
                        continue
                    if isinstance(value, int):
                        assert value >= 0, f"{family}.{key} was negative"


# ── 6. no win/result leakage; determinism ───────────────────────────────────

def test_toggling_win_flag_never_changes_output():
    participants_a = _synthetic_participants()
    participants_b = copy.deepcopy(participants_a)
    for row in participants_b:
        row["win"] = not row["win"]

    features_a = _compute(participants=participants_a)
    features_b = _compute(participants=participants_b)

    assert json.dumps(features_a, sort_keys=True) == json.dumps(features_b, sort_keys=True)


def test_no_result_or_win_keys_anywhere_in_output():
    for features in (
        _compute(),
        sf.compute_feature_set(
            _sion_participants(), SION_DURATION,
            sf.EvidenceCapabilities(), sf.AGGREGATE,
        )[0],
    ):
        dumped = json.dumps(features)
        assert '"win"' not in dumped
        assert '"local_win"' not in dumped
        assert '"result"' not in dumped


def test_compute_feature_set_is_deterministic_across_repeated_calls():
    first = _compute()
    second = _compute()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_evidence_contains_timestamped_signed_participant_events():
    features, evidence = sf.compute_feature_set(
        _synthetic_participants(), 1800.0,
        sf.EvidenceCapabilities(lcu_timeline=True), sf.LCU_TIMELINE,
        timeline=_base_synthetic_timeline(),
    )
    signed = [item for item in evidence if item["kind"] == "signed_event"]

    assert features["participants"]["1"]["fight_influence"]["kill_events"] == 1
    assert {
        (item["participant_id"], item["sign"], item["metric"], item["t_ms"])
        for item in signed
    } >= {
        (1, 1, "champion_kill", 60_000.0),
        (6, -1, "death", 60_000.0),
        (2, 1, "champion_kill_assist", 60_000.0),
        (1, 1, "structure_secure", 120_000.0),
    }


def test_live_snapshots_remain_useful_without_fabricating_event_influence():
    participants = _synthetic_participants()
    live_series = {
        1: [{
            "t_ms": 120_000.0, "level": 4, "is_dead": False,
            "respawn_timer": 0, "kills": 1, "deaths": 0, "assists": 2,
            "cs": 20, "ward_score": 3, "item_count": 1,
        }],
    }
    features, _ = sf.compute_feature_set(
        participants, 1800.0,
        sf.EvidenceCapabilities(
            live_client=True, live_client_completeness=0.8,
        ),
        sf.LIVE_CLIENT, live_snapshots=live_series,
    )

    player = features["participants"]["1"]
    assert player["fight_influence"] is None
    assert player["objective_participation"] is None
    assert player["live_state"]["available"] is True
    assert player["live_state"]["final_cs"] == 20
    assert features["chosen_source_completeness"] == 0.8


def test_unknown_evidence_source_is_rejected():
    with pytest.raises(ValueError):
        sf.compute_feature_set(
            _synthetic_participants(), 1800.0, sf.EvidenceCapabilities(), "made_up_source",
        )


def test_requires_exactly_ten_participants():
    with pytest.raises(ValueError):
        sf.compute_feature_set(
            _synthetic_participants()[:9], 1800.0, sf.EvidenceCapabilities(), sf.AGGREGATE,
        )


# ── 7. end-to-end HistoryStore integration ──────────────────────────────────

def _store_report(store, game_id, participants, duration, local_participant_id=1):
    scores = [
        {
            "participant_id": row["participant_id"], "model_version": 1,
            "total_score": 50.0, "match_rank": index + 1,
            "components": {}, "observations": [],
        }
        for index, row in enumerate(participants)
    ]
    match = {
        "game_id": game_id, "queue_id": 420, "map_id": 11, "game_mode": "CLASSIC",
        "game_creation": 1_000_000 + game_id, "game_creation_date": "2026-07-14T00:00:00Z",
        "duration": duration, "patch": "16.13.1",
        "local_participant_id": local_participant_id,
        "local_win": bool(participants[local_participant_id - 1]["win"]),
        "local_champion_id": participants[local_participant_id - 1]["champion_id"],
        "local_champion_name": participants[local_participant_id - 1]["champion_name"],
        "local_role": participants[local_participant_id - 1]["role"],
        "score_model_version": 1,
    }
    store.save_report({"match": match, "participants": participants, "scores": scores})


def test_extract_game_features_end_to_end_with_lcu_timeline(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    participants = _ksante_participants()
    _store_report(store, KSANTE_GAME_ID, participants, KSANTE_DURATION, local_participant_id=8)
    store.save_timeline_payload(
        KSANTE_GAME_ID, sf.LCU_TIMELINE,
        {"provenance": {"source": "lcu_historical"}, "timeline": _ksante_timeline()},
    )

    features = sf.extract_game_features(store, KSANTE_GAME_ID)

    assert features["evidence_source"] == sf.LCU_TIMELINE
    assert features["capabilities"]["lcu_timeline"] is True
    assert features["capabilities"]["match_v5"] is False
    ksante = features["participants"]["8"]
    assert ksante["fight_influence"]["first_blood"] is True
    assert ksante["objective_participation"]["turret_kills"] == 2

    with store._connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM feature_sets WHERE game_id = ?",
            (KSANTE_GAME_ID,),
        ).fetchone()
    assert rows["n"] == 1

    # calling again is idempotent (content-addressed): same features -> no
    # duplicate row, and the returned features are identical.
    features_again = sf.extract_game_features(store, KSANTE_GAME_ID)
    assert json.dumps(features, sort_keys=True) == json.dumps(features_again, sort_keys=True)
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT COUNT(*) AS n FROM feature_sets WHERE game_id = ?",
            (KSANTE_GAME_ID,),
        ).fetchone()
    assert rows["n"] == 1


def test_extract_game_features_can_explicitly_use_a_weaker_available_tier(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    participants = _ksante_participants()
    _store_report(store, KSANTE_GAME_ID, participants, KSANTE_DURATION, local_participant_id=8)
    store.save_timeline_payload(
        KSANTE_GAME_ID, sf.LCU_TIMELINE,
        {"provenance": {"source": "lcu_historical"}, "timeline": _ksante_timeline()},
    )

    features = sf.extract_game_features(
        store, KSANTE_GAME_ID, evidence_source=sf.AGGREGATE,
    )

    assert features["evidence_source"] == sf.AGGREGATE
    assert features["participants"]["8"]["fight_influence"] is None
    assert store.get_feature_set(
        KSANTE_GAME_ID, evidence_source=sf.AGGREGATE,
    ) is not None


def test_extract_game_features_falls_back_to_aggregate_when_no_timeline(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    participants = _sion_participants()
    _store_report(store, SION_GAME_ID, participants, SION_DURATION, local_participant_id=8)

    features = sf.extract_game_features(store, SION_GAME_ID)

    assert features["evidence_source"] == sf.AGGREGATE
    assert features["capabilities"] == {
        "match_v5": False, "lcu_timeline": False,
        "live_client": False, "aggregate": True,
    }
    assert features["abstain"] is True
    assert features["participants"]["8"]["fight_influence"] is None


def test_detect_capabilities_reads_real_completed_live_capture_session(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    participants = _sion_participants()
    game_id = 7000000001
    _store_report(store, game_id, participants, 1800.0)
    store.start_live_capture_session("session-1", game_id=game_id)
    store.update_live_capture_session("session-1", completeness=0.7)
    store.finalize_live_capture_session("session-1", status="completed")

    capabilities = sf.detect_capabilities(store, game_id)

    assert capabilities.live_client is True
    assert capabilities.live_client_completeness == 0.7
    assert capabilities.best_source() == sf.LIVE_CLIENT


def test_detect_capabilities_prefers_complete_lcu_over_partial_match_v5(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    participants = _sion_participants()
    game_id = 7000000002
    _store_report(store, game_id, participants, 1800.0)
    payload = {"provenance": {}, "timeline": {"frames": []}}
    store.save_timeline_payload(
        game_id, sf.MATCH_V5, payload, completeness=0.4,
    )
    store.save_timeline_payload(
        game_id, sf.LCU_TIMELINE, payload, completeness=1.0,
    )

    capabilities = sf.detect_capabilities(store, game_id)

    assert capabilities.match_v5 is True
    assert capabilities.lcu_timeline is True
    assert capabilities.best_source() == sf.LCU_TIMELINE


def test_extract_game_features_raises_for_unknown_game(tmp_path):
    store = HistoryStore(tmp_path / "history.db")
    with pytest.raises(ValueError):
        sf.extract_game_features(store, 424242)
