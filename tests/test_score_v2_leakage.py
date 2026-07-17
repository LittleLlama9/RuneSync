"""Tests for score_v2/leakage.py -- the recursive outcome-leakage guard.

Sections:
  1. Word-boundary matching catches real outcome keys (win/local_win/
     nexus/game_end/result/outcome variants) without the raw-substring
     false positive on legitimate `score_features.py` field names like
     `lead_windows`/`converted_lead_windows` (which contain "win" only as
     a substring of "windows").
  2. Nested/recursive detection through dicts and lists.
  3. `validate_feature_payload` is the required entry point and raises
     `OutcomeLeakageError`.
"""

import pytest

from score_v2.leakage import (
    OutcomeLeakageError,
    assert_no_outcome_leakage,
    scan_for_outcome_leakage,
    validate_feature_payload,
)


# ── 1. word-boundary matching ───────────────────────────────────────────────

@pytest.mark.parametrize("key", [
    "win", "Win", "local_win", "localWin", "teamWin", "didWin", "team_won",
    "loss", "lose", "loses", "result", "matchResult", "game_result",
    "nexus", "nexusHealth", "nexus_kills", "victory", "defeat", "defeated",
    "surrender", "surrendered", "remake", "outcome", "matchOutcome",
    "game_end", "gameEnd", "game_end_reason", "endGame",
])
def test_flags_real_outcome_shaped_keys(key):
    problems = scan_for_outcome_leakage({key: True})
    assert problems, f"expected {key!r} to be flagged"


@pytest.mark.parametrize("key", [
    # The regression this word-boundary rewrite exists for: "windows" as a
    # raw substring contains "win", but is a legitimate resource-conversion
    # timing concept in score_features.py, not an outcome field.
    "lead_windows", "converted_lead_windows", "window_seconds",
    # Other legitimate score_features.py-shaped keys that must never trip.
    "kills", "deaths", "assists", "gold_earned", "cs", "vision_score",
    "kill_events", "death_events", "structure_secures", "turret_kills",
    "game_creation_date", "game_mode", "gold", "role", "champion",
    "duration_seconds", "conversion_rate",
])
def test_does_not_flag_legitimate_keys(key):
    problems = scan_for_outcome_leakage({key: 1})
    assert not problems, f"did not expect {key!r} to be flagged: {problems}"


# ── 2. recursion through dicts/lists ────────────────────────────────────────

def test_recurses_into_nested_dicts():
    payload = {"fight_influence": {"nested": {"local_win": True}}}
    problems = scan_for_outcome_leakage(payload)
    assert any("local_win" in problem for problem in problems)


def test_recurses_into_lists():
    payload = {"events": [{"kind": "kill"}, {"result": "victory"}]}
    problems = scan_for_outcome_leakage(payload)
    assert problems


def test_clean_nested_payload_has_no_problems():
    payload = {
        "raw": {"kills": 5, "deaths": 2, "assists": 4, "gold_earned": 9000},
        "objective_participation": {
            "grub_secures": 1, "turret_kills": 2, "turret_plates": 3,
        },
        "resource_conversion": {
            "available": True, "lead_windows": 3, "converted_lead_windows": 2,
            "conversion_rate": 0.67,
        },
    }
    assert scan_for_outcome_leakage(payload) == []


# ── 3. entry points raise ───────────────────────────────────────────────────

def test_assert_no_outcome_leakage_raises_with_context():
    with pytest.raises(OutcomeLeakageError, match="my context"):
        assert_no_outcome_leakage({"win": True}, context="my context")


def test_assert_no_outcome_leakage_passes_clean_payload():
    assert_no_outcome_leakage({"kills": 5}) is None


def test_validate_feature_payload_raises_on_leakage():
    with pytest.raises(OutcomeLeakageError):
        validate_feature_payload({"raw": {"kills": 5}, "local_win": True})


def test_validate_feature_payload_passes_clean_block():
    assert validate_feature_payload({"raw": {"kills": 5}}) is None
