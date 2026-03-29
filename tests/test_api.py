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

    def test_passes_correct_params(self):
        captured = {}

        def fake_get(path, params, **kwargs):
            captured.update(params)
            return {}

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
