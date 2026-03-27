"""
test_cache.py — Unit tests for the matchup_data caching layer.

All tests use tmp_path + monkeypatch so no real files on disk are touched.
"""
import json
import pytest
import matchup_data

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE = {
    "Darius": {
        "top": {
            "Garen": {
                "difficulty": "medium",
                "who_wins_early": "Darius",
                "trading_pattern": "Extended trades. Walk into him to deny Q silence range.",
                "ability_to_dodge": "Garen Q — silence prevents abilities.",
                "power_spikes": {"you": "Level 5, Stridebreaker", "enemy": "Level 6, Sunfire"},
                "early_game": "Play aggressive levels 1-2.",
                "mid_game": "Shove and look for side lane pressure.",
                "late_game": "Stick to teamfights for ult resets.",
                "win_condition": "Get an early kill and snowball before Sunfire.",
                "counter_items": ["Plated Steelcaps", "Bramble Vest"],
                "scaling": "Even — Darius slightly favored early",
                "jungle_gankable": True,
                "positioning": "Hug the side of the wave away from his Q.",
            }
        }
    }
}


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset in-memory cache before and after each test."""
    matchup_data._cache = None
    yield
    matchup_data._cache = None


# ---------------------------------------------------------------------------
# _load() tests
# ---------------------------------------------------------------------------

def test_load_returns_empty_dict_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(tmp_path / "nope.json"))
    assert matchup_data._load() == {}


def test_load_parses_valid_json(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))
    assert matchup_data._load() == SAMPLE


def test_load_returns_empty_on_malformed_json(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text("{not valid json{{{{", encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))
    assert matchup_data._load() == {}


def test_load_caches_result_in_memory(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    first = matchup_data._load()
    f.unlink()  # delete the file — second call must use the in-memory cache
    second = matchup_data._load()

    assert first is second  # identity check proves cache was returned


def test_load_returns_empty_on_empty_file(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))
    assert matchup_data._load() == {}


# ---------------------------------------------------------------------------
# refresh_cache() tests
# ---------------------------------------------------------------------------

def test_refresh_cache_discards_and_reloads(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    matchup_data._load()
    assert matchup_data._cache is not None

    matchup_data.refresh_cache()
    assert matchup_data._cache == SAMPLE


def test_refresh_cache_picks_up_updated_file(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))
    matchup_data._load()

    updated = {"Garen": {"top": {}}}
    f.write_text(json.dumps(updated), encoding="utf-8")
    matchup_data.refresh_cache()

    assert matchup_data._cache == updated


# ---------------------------------------------------------------------------
# get_matchup_tips() tests
# ---------------------------------------------------------------------------

def test_get_matchup_tips_returns_correct_entry(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    tips = matchup_data.get_matchup_tips("Darius", "Garen", "top")
    assert tips is not None
    assert tips["difficulty"] == "medium"
    assert tips["who_wins_early"] == "Darius"
    assert isinstance(tips["counter_items"], list)


def test_get_matchup_tips_case_insensitive(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    tips = matchup_data.get_matchup_tips("darius", "garen", "top")
    assert tips is not None


def test_get_matchup_tips_unknown_enemy_returns_none(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    assert matchup_data.get_matchup_tips("Darius", "Zed", "top") is None


def test_get_matchup_tips_unknown_champion_returns_none(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    assert matchup_data.get_matchup_tips("Teemo", "Garen", "top") is None


def test_get_matchup_tips_auto_role_fallback(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))

    # "auto" role should still find the matchup by checking all roles
    tips = matchup_data.get_matchup_tips("Darius", "Garen", "auto")
    assert tips is not None


def test_get_matchup_tips_returns_none_when_cache_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(tmp_path / "nope.json"))
    assert matchup_data.get_matchup_tips("Darius", "Garen", "top") is None


# ---------------------------------------------------------------------------
# is_cache_loaded() tests
# ---------------------------------------------------------------------------

def test_is_cache_loaded_true_when_data_exists(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text(json.dumps(SAMPLE), encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))
    assert matchup_data.is_cache_loaded() is True


def test_is_cache_loaded_false_when_file_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(tmp_path / "nope.json"))
    assert matchup_data.is_cache_loaded() is False


def test_is_cache_loaded_false_when_file_empty_object(monkeypatch, tmp_path):
    f = tmp_path / "matchups.json"
    f.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(matchup_data, "_CACHE_PATH", str(f))
    assert matchup_data.is_cache_loaded() is False
