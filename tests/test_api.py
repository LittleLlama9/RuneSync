"""
test_api.py — Unit tests for ugg_api.UGGClient and the _get HTTP helper.

All network I/O is mocked — no real server is required.
"""
import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
import ugg_api
from ugg_api import UGGClient, _get


@pytest.fixture(autouse=True)
def clear_winrate_cache():
    """Reset the module-level winrate cache and patch state before every test."""
    ugg_api._WINRATE_CACHE.clear()
    ugg_api._patch_value = ""
    ugg_api._patch_fetched_at = 0.0
    yield
    ugg_api._WINRATE_CACHE.clear()
    ugg_api._patch_value = ""
    ugg_api._patch_fetched_at = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(payload) -> MagicMock:
    """Return a mock that looks like urllib.request.urlopen's response object."""
    mock = MagicMock()
    mock.read.return_value = json.dumps(payload).encode()
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _http_error(code: int, body: str = "error") -> urllib.error.HTTPError:
    fp = MagicMock()
    fp.read.return_value = body.encode()
    err = urllib.error.HTTPError(url="http://localhost", code=code, msg="err", hdrs=None, fp=fp)
    err.read = fp.read
    return err


# ---------------------------------------------------------------------------
# _get helper
# ---------------------------------------------------------------------------

class TestGetHelper:
    def test_returns_parsed_dict_on_success(self):
        with patch("ugg_api.urllib.request.urlopen", return_value=_mock_response({"patch": "14.5"})):
            result = _get("/patch", {})
        assert result == {"patch": "14.5"}

    def test_returns_parsed_list_on_success(self):
        payload = [{"champion": "Garen", "win_rate": 52.0}]
        with patch("ugg_api.urllib.request.urlopen", return_value=_mock_response(payload)):
            result = _get("/counters", {"champion": "Darius"})
        assert result == payload

    def test_returns_none_on_http_500(self):
        with patch("ugg_api.urllib.request.urlopen", side_effect=_http_error(500)):
            assert _get("/build", {}) is None

    def test_returns_none_on_http_404(self):
        with patch("ugg_api.urllib.request.urlopen", side_effect=_http_error(404)):
            assert _get("/build", {}) is None

    def test_returns_none_on_connection_refused(self):
        with patch("ugg_api.urllib.request.urlopen", side_effect=OSError("Connection refused")):
            assert _get("/build", {}) is None

    def test_returns_none_on_timeout(self):
        import socket
        with patch("ugg_api.urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            assert _get("/build", {}) is None

    def test_url_includes_query_params(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return _mock_response({})

        with patch("ugg_api.urllib.request.urlopen", side_effect=fake_urlopen):
            _get("/build", {"champion": "Darius", "role": "top"})

        assert "champion=Darius" in captured["url"]
        assert "role=top" in captured["url"]


# ---------------------------------------------------------------------------
# UGGClient.get_top_build
# ---------------------------------------------------------------------------

class TestGetTopBuild:
    def setup_method(self):
        self.client = UGGClient()

    def test_returns_build_dict_on_success(self):
        fake = {"runes": [8021], "items": [3153]}
        with patch("ugg_api._get", return_value=fake):
            result = self.client.get_top_build("Darius", "top")
        assert result == fake

    def test_raises_runtime_error_when_server_returns_none(self):
        with patch("ugg_api._get", return_value=None):
            with pytest.raises(RuntimeError, match="Failed to fetch build"):
                self.client.get_top_build("Darius", "top")

    def test_passes_correct_params(self):
        captured = {}

        def fake_get(path, params, **kwargs):
            captured.update(params)
            return {"runes": [], "items": []}

        with patch("ugg_api._get", side_effect=fake_get):
            self.client.get_top_build("Zed", "mid", rank="Diamond+", region="NA")

        assert captured["champion"] == "Zed"
        assert captured["role"] == "mid"
        assert captured["rank"] == "Diamond+"
        assert captured["region"] == "NA"


# ---------------------------------------------------------------------------
# UGGClient.get_counters
# ---------------------------------------------------------------------------

class TestGetCounters:
    def setup_method(self):
        self.client = UGGClient()

    def test_returns_list_on_success(self):
        fake = [{"champion": "Teemo", "win_rate": 55.0}]
        with patch("ugg_api._get", return_value=fake):
            result = self.client.get_counters("Darius", "top")
        assert result == fake

    def test_returns_empty_list_when_server_returns_none(self):
        with patch("ugg_api._get", return_value=None):
            assert self.client.get_counters("Darius", "top") == []

    def test_returns_empty_list_when_server_returns_non_list(self):
        with patch("ugg_api._get", return_value={"error": "bad response"}):
            assert self.client.get_counters("Darius", "top") == []

    def test_passes_top_n_param(self):
        captured = {}

        def fake_get(path, params, **kwargs):
            captured.update(params)
            return []

        with patch("ugg_api._get", side_effect=fake_get):
            self.client.get_counters("Garen", "top", top_n=10)

        assert captured["top_n"] == 10


class TestGetCountersBundleFallback:
    """When the bundle's curated counter list is empty, counters are derived
    from the matchup table so champs the builder skipped still resolve."""

    def setup_method(self):
        self.client = UGGClient()

    def teardown_method(self):
        ugg_api._bundle = None

    def test_derives_counters_from_matchups_when_curated_list_empty(self):
        # Cho'gath has no curated counters but a full matchup table. Opponents
        # with cho'gath WR < 50 should surface as counters (WR = 100 - that).
        ugg_api._bundle = {
            "counters": {"cho'gath": {}},
            "matchups": {"cho'gath": {"top": {
                "Sett": 43.75,      # -> counter 56.25
                "Dr. Mundo": 45.07,  # -> counter 54.93
                "Shen": 51.43,       # not a counter (cho'gath wins)
            }}},
        }
        result = self.client.get_counters("Cho'Gath", "top", top_n=5)
        names = [c["champion"] for c in result]
        assert names == ["Sett", "Dr. Mundo"]   # sorted by counter WR desc
        assert result[0]["win_rate"] == 56.25
        assert "Shen" not in names

    def test_merges_curated_with_derived(self):
        # Curated and matchup-derived counters are merged; both surface, sorted
        # by WR desc. Teemo (garen WR 40 -> counter 60) outranks curated Vayne 55.
        ugg_api._bundle = {
            "counters": {"garen": {"top": [{"champion": "Vayne", "win_rate": 55.0}]}},
            "matchups": {"garen": {"top": {"Teemo": 40.0}}},
        }
        result = self.client.get_counters("Garen", "top")
        assert [c["champion"] for c in result] == ["Teemo", "Vayne"]
        assert result[0]["win_rate"] == 60.0

    def test_curated_value_overrides_derived_for_shared_champ(self):
        # When a champ is in both lists, the curated (higher-sample) WR wins.
        ugg_api._bundle = {
            "counters": {"garen": {"top": [{"champion": "Teemo", "win_rate": 58.0}]}},
            "matchups": {"garen": {"top": {"Teemo": 40.0}}},  # derived would be 60.0
        }
        result = self.client.get_counters("Garen", "top")
        assert result == [{"champion": "Teemo", "win_rate": 58.0}]

    def test_drops_sub_50_curated_counters(self):
        # Regression: a curated list polluted with sub-50% "counters" (champs that
        # LOSE to the target — the Malphite bug) must drop them. Only the real
        # winning lane survives; nothing in the result may sit at or below 50.
        ugg_api._bundle = {
            "counters": {"malphite": {"top": [
                {"champion": "Garen", "win_rate": 53.61},   # real counter
                {"champion": "Yone", "win_rate": 49.52},    # LOSES -> drop
                {"champion": "Aatrox", "win_rate": 48.43},  # LOSES -> drop
                {"champion": "Darius", "win_rate": 44.81},  # LOSES -> drop
            ]}},
            "matchups": {"malphite": {}},
        }
        result = self.client.get_counters("Malphite", "top", top_n=5)
        names = [c["champion"] for c in result]
        assert names == ["Garen"]
        assert all(c["win_rate"] > 50.0 for c in result)

    def test_empty_when_no_losing_matchups(self):
        ugg_api._bundle = {
            "counters": {"sona": {}},
            "matchups": {"sona": {"support": {"Lux": 52.0, "Brand": 55.0}}},
        }
        assert self.client.get_counters("Sona", "support") == []


# ---------------------------------------------------------------------------
# Live off-role build fetch (niche lanes the bundle's games floor excluded)
# ---------------------------------------------------------------------------

class TestLiveOffRoleBuild:
    """When the bundle has no build for the requested lane, get_top_build fetches
    that lane live (op.gg) rather than importing the champ's main-role build
    (wrong runes + Smite dragged into a lane)."""

    def setup_method(self):
        self.client = UGGClient()
        ugg_api._live_build_cache.clear()

    def teardown_method(self):
        ugg_api._bundle = None
        ugg_api._live_build_cache.clear()

    def test_live_fetch_used_when_role_missing(self):
        ugg_api._bundle = {
            "patch": "16.13.1",
            "builds": {"sejuani": {"jungle": {"role": "jungle", "summoners": [4, 11]}}},
            "role_weights": {"Sejuani": {"jungle": 0.9}},
        }
        live = {"champion": "Sejuani", "role": "top", "summoners": [12, 14],
                "selected_perk_ids": [1, 2, 3, 4, 5, 6, 7, 8, 9],
                "primary_style_id": 8400, "sub_style_id": 8000,
                "items_core": ["2525"], "_source": "live:op.gg"}
        with patch("ugg_api._fetch_live_build", return_value=live) as m:
            b = self.client.get_top_build("Sejuani", role="top")
        m.assert_called_once_with("Sejuani", "top")
        assert b["role"] == "top"
        assert 11 not in b["summoners"]   # no Smite in a lane build

    def test_bundle_role_skips_live_fetch(self):
        ugg_api._bundle = {
            "patch": "16.13.1",
            "builds": {"sejuani": {"jungle": {"role": "jungle", "summoners": [4, 11]}}},
        }
        with patch("ugg_api._fetch_live_build") as m:
            b = self.client.get_top_build("Sejuani", role="jungle")
        m.assert_not_called()
        assert b["role"] == "jungle"

    def test_falls_back_to_offrole_when_live_unavailable(self):
        ugg_api._bundle = {
            "patch": "16.13.1",
            "builds": {"sejuani": {"jungle": {"role": "jungle", "summoners": [4, 11]}}},
            "role_weights": {"Sejuani": {"jungle": 0.9}},
        }
        with patch("ugg_api._fetch_live_build", return_value=None):
            b = self.client.get_top_build("Sejuani", role="top")
        assert b["role"] == "jungle"   # off-role bundle fallback preserved

    def test_extract_opgg_build_shape(self):
        d = {
            "rune_pages": [{"builds": [{
                "primary_page_id": 8400, "secondary_page_id": 8000,
                "primary_rune_ids": [8437, 8446, 8444, 8451],
                "secondary_rune_ids": [8226, 8237],
                "stat_mod_ids": [5005, 5001, 5001],
            }]}],
            "summoner_spells": [{"ids": [12, 14]}],
            "starter_items": [{"ids": [1054, 2003]}],
            "core_items": [{"ids": [2525, 3050, 2502]}],
            "last_items": [{"ids": [3075]}, {"ids": [3193]}],
        }
        b = ugg_api._extract_opgg_build(d, "Sejuani", "top")
        assert b["role"] == "top"
        assert b["summoners"] == [12, 14]
        assert len(b["selected_perk_ids"]) == 9
        assert b["items_core"] == ["2525", "3050", "2502"]
        assert b["primary_style_id"] == 8400
        assert b["_source"] == "live:op.gg"

    def test_extract_opgg_build_none_when_no_runes(self):
        assert ugg_api._extract_opgg_build({"rune_pages": []}, "Sejuani", "top") is None


# ---------------------------------------------------------------------------
# UGGClient.get_matchup_winrate
# ---------------------------------------------------------------------------

class TestGetMatchupWinrate:
    def setup_method(self):
        self.client = UGGClient()

    def test_returns_dict_on_success(self):
        fake = {"win_rate": 51.5, "games": 3000}
        with patch("ugg_api._get", return_value=fake):
            result = self.client.get_matchup_winrate("Darius", "Garen", "top")
        assert result == fake

    def test_returns_none_on_miss(self):
        with patch("ugg_api._get", return_value=None):
            assert self.client.get_matchup_winrate("Darius", "Zed", "top") is None

    def test_caches_successful_result(self):
        fake = {"win_rate": 51.5, "games": 3000}
        call_count = {"n": 0}

        def counting_get(path, params, **kwargs):
            call_count["n"] += 1
            return fake

        with patch("ugg_api._current_patch", return_value="16.6"):
            with patch("ugg_api._get", side_effect=counting_get):
                r1 = self.client.get_matchup_winrate("Darius", "Garen", "top")
                r2 = self.client.get_matchup_winrate("Darius", "Garen", "top")

        assert r1 == r2 == fake
        assert call_count["n"] == 1  # /matchup fetched only once; second call hit cache

    def test_cache_is_case_insensitive(self):
        fake = {"win_rate": 51.5, "games": 3000}
        call_count = {"n": 0}

        def counting_get(path, params, **kwargs):
            call_count["n"] += 1
            return fake

        with patch("ugg_api._current_patch", return_value="16.6"):
            with patch("ugg_api._get", side_effect=counting_get):
                self.client.get_matchup_winrate("Darius", "Garen", "top")
                self.client.get_matchup_winrate("darius", "garen", "top")

        assert call_count["n"] == 1

    def test_does_not_cache_failed_lookups(self):
        """Server-down results (None) must not be cached so the next session can retry."""
        call_count = {"n": 0}

        def counting_get(path, params, **kwargs):
            call_count["n"] += 1
            return None

        with patch("ugg_api._current_patch", return_value="16.6"):
            with patch("ugg_api._get", side_effect=counting_get):
                self.client.get_matchup_winrate("Darius", "Garen", "top")
                self.client.get_matchup_winrate("Darius", "Garen", "top")

        assert call_count["n"] == 2  # retried because first returned None

    def test_cache_evicted_on_patch_change(self):
        """Cache entries for old patch must not be served after a patch bump."""
        fake = {"win_rate": 51.5, "games": 3000}
        call_count = {"n": 0}

        def counting_get(path, params, **kwargs):
            call_count["n"] += 1
            return fake

        # Seed cache on patch 16.6
        with patch("ugg_api._current_patch", return_value="16.6"):
            with patch("ugg_api._get", side_effect=counting_get):
                self.client.get_matchup_winrate("Darius", "Garen", "top")

        assert call_count["n"] == 1

        # Same lookup on new patch — must re-fetch
        with patch("ugg_api._current_patch", return_value="16.7"):
            with patch("ugg_api._get", side_effect=counting_get):
                self.client.get_matchup_winrate("Darius", "Garen", "top")

        assert call_count["n"] == 2  # fetched again for new patch

    def test_passes_correct_params(self):
        captured = {}

        def fake_get(path, params, **kwargs):
            captured.update(params)
            return {}

        with patch("ugg_api._current_patch", return_value="16.6"):
            with patch("ugg_api._get", side_effect=fake_get):
                self.client.get_matchup_winrate("Darius", "Garen", "top")

        assert captured["my_champ"] == "Darius"
        assert captured["enemy_champ"] == "Garen"
        assert captured["role"] == "top"


# ---------------------------------------------------------------------------
# UGGClient.get_current_patch
# ---------------------------------------------------------------------------

class TestGetCurrentPatch:
    def setup_method(self):
        self.client = UGGClient()

    def test_returns_patch_string(self):
        with patch("ugg_api._get", return_value={"patch": "14.10"}):
            assert self.client.get_current_patch() == "14.10"

    def test_returns_latest_when_server_unavailable(self):
        with patch("ugg_api._get", return_value=None):
            assert self.client.get_current_patch() == "latest"

    def test_returns_latest_on_malformed_response(self):
        with patch("ugg_api._get", return_value={"bad_key": "nope"}):
            assert self.client.get_current_patch() == "latest"
