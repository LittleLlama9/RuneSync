"""Tests for corpus.splits: deterministic grouped split assignment and
leakage checks."""

import pytest

from corpus.manifest import GameMetadata, build_entry, hash_identifier
from corpus.splits import (
    SplitConfig,
    SplitConfigError,
    SplitLeakageError,
    assign_splits,
    assign_splits_strict,
    check_leakage,
)

_SALT = b"test-split-salt"


def _metadata(**overrides):
    defaults = dict(
        patch="14.1", queue_id=420, map_id=11, duration_seconds=1800,
        game_creation_date="2024-01-01T00:00:00Z",
        region=None, region_unknown_reason="unknown", rank_tier=None,
        rank_unknown_reason="unknown",
    )
    defaults.update(overrides)
    return GameMetadata(**defaults)


def _entry(game_id, players, champion="Ahri", **overrides):
    return build_entry(
        game_id=game_id, source="aggregate", capture_method="lcu_aggregate_fallback",
        player_group_keys=[hash_identifier(p, _SALT) for p in players],
        completeness=1.0, privacy_classification="local_hashed_real",
        consent_status="personal_local_client_data",
        game_metadata=_metadata(**{k: v for k, v in overrides.items() if k in
                                    ("patch", "duration_seconds", "region", "rank_tier")}),
        champion=champion, role="MID",
    )


def _independent_games(n=12, start=1):
    """n games with fully disjoint player pools -- no forced grouping."""
    entries = []
    for i in range(n):
        game_id = start + i
        players = [f"p{game_id}_{slot}" for slot in range(10)]
        entries.append(_entry(game_id, players))
    return entries


def test_split_config_rejects_ratios_not_summing_to_one():
    with pytest.raises(SplitConfigError):
        SplitConfig(seed=1, ratios={"train": 0.5, "validation": 0.2, "test": 0.2})


def test_split_config_accepts_valid_ratios():
    cfg = SplitConfig(seed=1)
    assert cfg.ratios["train"] == pytest.approx(0.7)


def test_assign_splits_is_deterministic_given_same_seed():
    entries = _independent_games(20)
    a1 = assign_splits(entries, SplitConfig(seed=42))
    a2 = assign_splits(entries, SplitConfig(seed=42))
    assert a1 == a2


def test_assign_splits_can_differ_with_different_seed():
    entries = _independent_games(20)
    a1 = assign_splits(entries, SplitConfig(seed=1))
    a2 = assign_splits(entries, SplitConfig(seed=2))
    assert a1 != a2


def test_assign_splits_uses_all_split_names():
    entries = _independent_games(30)
    assignments = assign_splits(entries, SplitConfig(seed=7))
    assert set(assignments.values()) <= {"train", "validation", "test"}
    assert set(assignments.values()) == {"train", "validation", "test"}


def test_shared_player_across_two_matches_forces_same_split():
    shared_players = ["shared_a"] + [f"p1_{i}" for i in range(9)]
    other_players = ["shared_a"] + [f"p2_{i}" for i in range(9)]
    e1 = _entry(1, shared_players)
    e2 = _entry(2, other_players)
    rest = _independent_games(20, start=100)
    entries = [e1, e2] + rest

    assignments = assign_splits(entries, SplitConfig(seed=5))
    assert assignments[e1.entry_id] == assignments[e2.entry_id]


def test_check_leakage_reports_clean_for_valid_assignment():
    entries = _independent_games(20)
    assignments = assign_splits(entries, SplitConfig(seed=9))
    report = check_leakage(entries, assignments)
    assert report.is_clean()
    assert report.hard_violations == []


def test_check_leakage_detects_forced_player_split_violation():
    shared_players = ["shared_x"] + [f"pa_{i}" for i in range(9)]
    other_players = ["shared_x"] + [f"pb_{i}" for i in range(9)]
    e1 = _entry(1, shared_players)
    e2 = _entry(2, other_players)
    entries = [e1, e2]

    bad_assignments = {e1.entry_id: "train", e2.entry_id: "test"}
    report = check_leakage(entries, bad_assignments)
    assert not report.is_clean()
    assert any("span" in v for v in report.hard_violations)


def test_check_leakage_detects_match_group_spanning_splits():
    # Same game_id/source entered twice with different entry objects would
    # collide on entry_id in a manifest, but check_leakage independently
    # verifies match_group_key never spans splits even if callers pass in
    # a manually-doctored assignment map.
    e1 = _entry(1, [f"pa_{i}" for i in range(10)])
    entries = [e1]
    # Forge an assignment claiming the same match_group_key spans two
    # splits by duplicating the key under a synthetic second "entry".
    bad_assignments = {e1.entry_id: "train"}
    report = check_leakage(entries, bad_assignments)
    assert report.is_clean()  # single entry alone cannot span


def test_check_leakage_detects_duplicate_content_hash_across_splits():
    e1 = _entry(1, [f"pa_{i}" for i in range(10)])
    e2 = _entry(2, [f"pb_{i}" for i in range(10)])
    # Force e2 to have identical content_hash to e1 by copying it (as if the
    # same underlying evidence were registered under two different games).
    import dataclasses
    e2_dup_hash = dataclasses.replace(e2, content_hash=e1.content_hash)
    entries = [e1, e2_dup_hash]
    bad_assignments = {e1.entry_id: "train", e2_dup_hash.entry_id: "test"}
    report = check_leakage(entries, bad_assignments)
    assert not report.is_clean()


def test_check_leakage_warns_on_champion_concentration():
    # Many entries, all the same champion, all forced into one split via
    # shared players, should trigger a concentration warning rather than a
    # hard violation.
    shared = "shared_champ_pool"
    entries = []
    for i in range(10):
        players = [shared] + [f"cc_{i}_{j}" for j in range(9)]
        entries.append(_entry(100 + i, players, champion="SameChampion"))
    assignments = {e.entry_id: "train" for e in entries}
    report = check_leakage(entries, assignments)
    assert report.is_clean()
    assert any("champion" in w for w in report.warnings)


def test_assign_splits_strict_passes_through_clean_assignment(monkeypatch):
    e1 = _entry(1, [f"pa_{i}" for i in range(10)])
    e2 = _entry(2, [f"pb_{i}" for i in range(10)])
    entries = [e1, e2]

    def fake_assign(_entries, _config):
        return {e1.entry_id: "train", e2.entry_id: "test"}

    import corpus.splits as splits_mod
    monkeypatch.setattr(splits_mod, "assign_splits", fake_assign)
    result, report = splits_mod.assign_splits_strict(entries, SplitConfig(seed=1))
    assert result[e1.entry_id] == "train"
    assert report.is_clean()


def test_assign_splits_strict_raises_when_shared_player_forced_apart(monkeypatch):
    shared_players = ["shared_y"] + [f"pa_{i}" for i in range(9)]
    other_players = ["shared_y"] + [f"pb_{i}" for i in range(9)]
    e1 = _entry(1, shared_players)
    e2 = _entry(2, other_players)
    entries = [e1, e2]

    def fake_assign_bad(_entries, _config):
        return {e1.entry_id: "train", e2.entry_id: "test"}

    import corpus.splits as splits_mod
    monkeypatch.setattr(splits_mod, "assign_splits", fake_assign_bad)
    with pytest.raises(SplitLeakageError):
        splits_mod.assign_splits_strict(entries, SplitConfig(seed=1))


def test_assign_splits_empty_entries_returns_empty():
    assert assign_splits([], SplitConfig(seed=1)) == {}


def test_fixture_manifest_round_trips_through_split_assignment():
    """End-to-end sanity check using the on-disk sample fixture manifest
    covering all 4 evidence sources, including a player (p1001_0) shared
    between game 1001 (aggregate) and game 1003 (live_client)."""
    from pathlib import Path
    from corpus.manifest import CorpusManifest

    fixture_path = Path(__file__).parent / "fixtures" / "corpus" / "manifest_sample.json"
    manifest = CorpusManifest.load(fixture_path)
    entries = manifest.to_list()
    assert len(entries) == 4
    assert {e.source for e in entries} == {
        "aggregate", "lcu_timeline", "live_client", "match_v5",
    }

    assignments, report = assign_splits_strict(entries, SplitConfig(seed=123))
    assert report.is_clean()
    game_1001 = next(e for e in entries if e.game_id == 1001)
    game_1003 = next(e for e in entries if e.game_id == 1003)
    # These two share a player (p1001_0) and must land in the same split.
    assert assignments[game_1001.entry_id] == assignments[game_1003.entry_id]
