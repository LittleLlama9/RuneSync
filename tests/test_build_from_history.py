"""Tests for corpus.build_from_history: sanitized manifest entries built
from a hermetic (temp, non-production) HistoryStore.

These tests never touch the real %APPDATA%\\RuneSync\\history.db -- every
test builds its own throwaway HistoryStore in tmp_path.
"""

import pytest

from history_store import HistoryStore
from corpus.build_from_history import (
    HistoryEvidenceUnavailableError,
    available_sources_for_game,
    build_entry_from_history,
)
from corpus.manifest import validate_entry

_SALT = b"test-build-salt"


def _report(game_id=5602827182, local_participant_id=8):
    """Mirrors tests/test_history_store.py's `_report()` fixture shape, but
    with sanitized facts matching the real verified game 5602827182 (see
    tests/fixtures/aggregate_participants_5602827182.json)."""
    participants = [
        {"participant_id": 1, "puuid": "puuid-1", "summoner_name": "Player1",
         "champion_id": 86, "champion_name": "Garen", "team_id": 100, "role": "top",
         "win": True, "kills": 8, "deaths": 0, "assists": 2, "gold_earned": 15493,
         "cs": 264, "champion_level": 19, "damage_to_champions": 34321,
         "damage_to_objectives": 18930, "damage_to_turrets": 18930,
         "damage_taken": 25079, "damage_mitigated": 32856, "healing": 3934,
         "vision_score": 27, "wards_placed": 10, "wards_killed": 0, "items": []},
        {"participant_id": 8, "puuid": "puuid-8", "summoner_name": "Player8",
         "champion_id": 897, "champion_name": "K'Sante", "team_id": 200, "role": "mid",
         "win": False, "kills": 8, "deaths": 7, "assists": 4, "gold_earned": 12262,
         "cs": 184, "champion_level": 15, "damage_to_champions": 21543,
         "damage_to_objectives": 3153, "damage_to_turrets": 1897,
         "damage_taken": 36399, "damage_mitigated": 49298, "healing": 4918,
         "vision_score": 29, "wards_placed": 12, "wards_killed": 0, "items": []},
        {"participant_id": 10, "puuid": "puuid-10", "summoner_name": "Player10",
         "champion_id": 147, "champion_name": "Seraphine", "team_id": 200, "role": "support",
         "win": False, "kills": 3, "deaths": 15, "assists": 14, "gold_earned": 9358,
         "cs": 30, "champion_level": 13, "damage_to_champions": 11819,
         "damage_to_objectives": 3258, "damage_to_turrets": 2489,
         "damage_taken": 32982, "damage_mitigated": 15745, "healing": 5041,
         "vision_score": 79, "wards_placed": 30, "wards_killed": 8, "items": []},
    ]
    scores = [
        {"participant_id": p["participant_id"], "model_version": 1,
         "total_score": 50.0, "match_rank": 1, "components": {}, "observations": []}
        for p in participants
    ]
    return {
        "match": {
            "game_id": game_id, "queue_id": 420, "map_id": 11, "game_mode": "CLASSIC",
            "game_creation": 1000000 + game_id, "game_creation_date": "2026-07-14T00:00:00Z",
            "duration": 1861, "patch": "16.13.1", "local_participant_id": local_participant_id,
            "local_win": False, "local_champion_id": 897, "local_champion_name": "K'Sante",
            "local_role": "mid", "score_model_version": 1,
        },
        "participants": participants, "scores": scores,
    }


@pytest.fixture
def store(tmp_path):
    return HistoryStore(tmp_path / "history.db")


def test_available_sources_reports_only_what_is_actually_stored(store):
    store.save_report(_report(game_id=1))
    assert available_sources_for_game(store, 1) == ["aggregate"]

    store.save_timeline_payload(1, "lcu_timeline", {"frames": []}, completeness=0.8)
    assert set(available_sources_for_game(store, 1)) == {"aggregate", "lcu_timeline"}


def test_available_sources_empty_for_unknown_game(store):
    assert available_sources_for_game(store, 999) == []


def test_build_entry_from_history_aggregate_is_sanitized_and_valid(store):
    store.save_report(_report(game_id=1))
    entry = build_entry_from_history(store, 1, "aggregate", identity_salt=_SALT)
    assert validate_entry(entry) is None
    assert entry.leakage.champion == "K'Sante"
    # Every real puuid must be hashed away, never copied verbatim.
    for key in entry.leakage.player_group_keys:
        assert key.startswith("p_")
        assert "puuid-" not in key


def test_build_entry_from_history_honestly_reports_unknown_region_rank(store):
    store.save_report(_report(game_id=1))
    entry = build_entry_from_history(store, 1, "aggregate", identity_salt=_SALT)
    assert entry.game_metadata.region is None
    assert entry.game_metadata.region_unknown_reason
    assert entry.game_metadata.rank_tier is None
    assert entry.game_metadata.rank_unknown_reason


def test_build_entry_from_history_lcu_timeline_uses_real_content_hash(store):
    store.save_report(_report(game_id=1))
    store.save_timeline_payload(1, "lcu_timeline", {"frames": [1, 2, 3]}, completeness=0.9)
    entry = build_entry_from_history(store, 1, "lcu_timeline", identity_salt=_SALT)
    assert validate_entry(entry) is None
    assert entry.completeness == pytest.approx(0.9)


def test_build_entry_from_history_raises_when_match_v5_not_captured(store):
    store.save_report(_report(game_id=1))
    with pytest.raises(HistoryEvidenceUnavailableError):
        build_entry_from_history(store, 1, "match_v5", identity_salt=_SALT)


def test_build_entry_from_history_raises_for_unknown_game(store):
    with pytest.raises(HistoryEvidenceUnavailableError):
        build_entry_from_history(store, 999, "aggregate", identity_salt=_SALT)


def test_build_entry_from_history_raises_for_unknown_source(store):
    store.save_report(_report(game_id=1))
    with pytest.raises(HistoryEvidenceUnavailableError):
        build_entry_from_history(store, 1, "not_a_real_source", identity_salt=_SALT)


def test_build_entry_from_history_does_not_fabricate_live_client_evidence(store):
    store.save_report(_report(game_id=1))
    with pytest.raises(HistoryEvidenceUnavailableError):
        build_entry_from_history(store, 1, "live_client", identity_salt=_SALT)


def test_build_entry_from_history_is_deterministic(store):
    store.save_report(_report(game_id=1))
    e1 = build_entry_from_history(store, 1, "aggregate", identity_salt=_SALT)
    e2 = build_entry_from_history(store, 1, "aggregate", identity_salt=_SALT)
    assert e1.content_hash == e2.content_hash
    assert e1.entry_id == e2.entry_id
