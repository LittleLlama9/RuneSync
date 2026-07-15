import copy

import pytest

from performance_score import SCORING_MODEL_VERSION, score_match


def _players():
    roles = ["top", "jungle", "mid", "bot", "support"] * 2
    players = []
    for index in range(10):
        team = 100 if index < 5 else 200
        players.append({
            "participant_id": index + 1,
            "team_id": team,
            "role": roles[index],
            "win": team == 100,
            "kills": 5,
            "deaths": 5,
            "assists": 5,
            "gold_earned": 10000,
            "cs": 150,
            "damage_to_champions": 15000,
            "damage_to_objectives": 5000,
            "damage_to_turrets": 2000,
            "damage_taken": 15000,
            "damage_mitigated": 8000,
            "healing": 1000,
            "vision_score": 20,
            "wards_placed": 8,
            "wards_killed": 2,
        })
    return players


def test_scores_are_bounded_versioned_and_ranked():
    scores = score_match(_players(), 1800)

    assert len(scores) == 10
    assert {s["match_rank"] for s in scores} == set(range(1, 11))
    assert all(0 <= s["total_score"] <= 100 for s in scores)
    assert all(s["model_version"] == SCORING_MODEL_VERSION for s in scores)
    assert all(set(s["components"]) == {
        "combat", "economy", "objectives", "vision", "teamplay",
    } for s in scores)


def test_strong_loser_can_outrank_weak_winner():
    players = _players()
    strong_loser = players[5]
    strong_loser.update({
        "kills": 20, "deaths": 1, "assists": 15, "gold_earned": 18000,
        "cs": 280, "damage_to_champions": 60000,
        "damage_to_objectives": 25000, "damage_to_turrets": 12000,
        "vision_score": 45, "wards_placed": 16, "wards_killed": 6,
        "damage_mitigated": 20000, "healing": 5000,
    })
    weak_winner = players[0]
    weak_winner.update({
        "kills": 0, "deaths": 12, "assists": 1, "gold_earned": 6000,
        "cs": 60, "damage_to_champions": 3000,
        "damage_to_objectives": 100, "damage_to_turrets": 0,
        "vision_score": 4, "wards_placed": 2, "wards_killed": 0,
        "damage_mitigated": 1000, "healing": 0,
    })

    by_id = {s["participant_id"]: s for s in score_match(players, 1800)}
    assert by_id[6]["match_rank"] < by_id[1]["match_rank"]
    assert by_id[6]["total_score"] > by_id[1]["total_score"]


def test_support_is_not_penalized_for_low_cs_when_vision_is_strong():
    players = _players()
    support = players[4]
    support.update({
        "cs": 20, "vision_score": 100, "wards_placed": 40, "wards_killed": 12,
        "assists": 22, "deaths": 2, "damage_mitigated": 18000, "healing": 8000,
    })
    scores = {s["participant_id"]: s for s in score_match(players, 1800)}

    assert scores[5]["components"]["economy"] < 50
    assert scores[5]["components"]["vision"] == 100
    assert scores[5]["total_score"] > 50


def test_observations_report_match_leading_fact():
    players = _players()
    players[2]["damage_to_champions"] = 50000
    score = next(
        s for s in score_match(players, 1800) if s["participant_id"] == 3
    )
    assert "Highest champion damage in the match." in score["observations"]


def test_requires_exactly_ten_participants():
    with pytest.raises(ValueError, match="exactly 10"):
        score_match(copy.deepcopy(_players()[:9]), 1800)


def test_tied_components_do_not_create_contradictory_observations():
    scores = score_match(_players(), 1800)
    for score in scores:
        assert score["observations"][0] == "Score components were evenly balanced."
        assert not any("strongest component" in text for text in score["observations"])
        assert not any("ranked lowest" in text for text in score["observations"])
        assert "Highest champion damage in the match." not in score["observations"]
