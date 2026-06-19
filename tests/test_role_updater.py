"""
test_role_updater.py — the role-weight scale contract.

Regression coverage for the bug where the data bundle shipped role weights as
fractions (0-1) while the runtime (champion_roles._is_plausible_dist) and the
hardcoded ROLE_WEIGHTS table are percent (0-100), so the bundle's weights were
silently rejected and the role cache fell back to a stale hardcoded table
forever. All network/disk I/O is mocked or redirected to tmp files.
"""
import json
import time

import pytest

import role_updater
import ugg_api
from champion_roles import _is_plausible_dist


# Representative fraction-scale weights as the bundle ships them (op.gg role_rate).
_FRACTION_WEIGHTS = {
    "Garen":   {"top": 0.915, "mid": 0.047},
    "Aatrox":  {"top": 0.847, "jungle": 0.13},
    "Jinx":    {"bot": 0.997},
    "Thresh":  {"support": 0.997},
}

# The same shape already on percent scale, as the legacy scraper returns it.
_PERCENT_WEIGHTS = {
    "Garen":   {"top": 91.5, "mid": 4.7},
    "Aatrox":  {"top": 84.7, "jungle": 13.0},
}


@pytest.fixture(autouse=True)
def restore_bundle():
    """Don't let a test's fake bundle leak into other modules' tests."""
    saved = ugg_api._bundle
    yield
    ugg_api._bundle = saved


# ---------------------------------------------------------------------------
# _normalize_to_percent
# ---------------------------------------------------------------------------

class TestNormalizeToPercent:
    def test_fractions_scaled_to_percent(self):
        out = role_updater._normalize_to_percent(_FRACTION_WEIGHTS)
        assert out["Garen"] == {"top": 91.5, "mid": 4.7}
        assert out["Jinx"] == {"bot": 99.7}

    def test_percent_passes_through_untouched(self):
        out = role_updater._normalize_to_percent(_PERCENT_WEIGHTS)
        assert out == _PERCENT_WEIGHTS

    def test_idempotent(self):
        once = role_updater._normalize_to_percent(_FRACTION_WEIGHTS)
        twice = role_updater._normalize_to_percent(once)
        assert once == twice

    def test_handles_empty_and_nondict_entries(self):
        out = role_updater._normalize_to_percent({"X": {}, "Y": None})
        assert out == {"X": {}, "Y": None}


# ---------------------------------------------------------------------------
# The core invariant: normalized bundle weights are accepted by the runtime.
# ---------------------------------------------------------------------------

class TestPlausibilityContract:
    def test_raw_fractions_are_rejected(self):
        # This is the bug: fraction-scale dists fail the runtime plausibility
        # check (total ~1.0 < 50), so they'd never reach inference.
        assert not _is_plausible_dist(_FRACTION_WEIGHTS["Garen"])

    def test_normalized_fractions_are_accepted(self):
        norm = role_updater._normalize_to_percent(_FRACTION_WEIGHTS)
        for champ, roles in norm.items():
            assert _is_plausible_dist(roles), f"{champ} rejected: {roles}"


# ---------------------------------------------------------------------------
# End-to-end: fetching from the bundle yields percent-scale, plausible weights.
# ---------------------------------------------------------------------------

class TestFetchFromBundle:
    def test_bundle_weights_are_normalized(self):
        # _fetch_from_bundle requires >10 champs; pad with synthetic fractions.
        padded = dict(_FRACTION_WEIGHTS)
        for i in range(12):
            padded[f"Filler{i}"] = {"mid": 0.9, "top": 0.1}
        ugg_api._bundle = {"role_weights": padded}

        out = role_updater._fetch_role_weights()

        assert out is not None
        assert out["Garen"] == {"top": 91.5, "mid": 4.7}
        assert _is_plausible_dist(out["Garen"])


# ---------------------------------------------------------------------------
# Cache round-trip: format-version invalidation + simulated patch rollover.
# ---------------------------------------------------------------------------

class TestCacheStaleness:
    @pytest.fixture(autouse=True)
    def tmp_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(role_updater, "CACHE_PATH", tmp_path / "role_cache.json")
        yield

    def test_fresh_cache_not_stale(self, monkeypatch):
        monkeypatch.setattr(role_updater, "get_latest_patch", lambda: "16.6")
        role_updater.save_cache(_PERCENT_WEIGHTS, "16.6")
        assert role_updater.cache_is_stale() is False

    def test_save_stamps_current_format(self):
        role_updater.save_cache(_PERCENT_WEIGHTS, "16.6")
        data = json.loads(role_updater.CACHE_PATH.read_text(encoding="utf-8"))
        assert data["format_version"] == role_updater.ROLE_CACHE_FORMAT

    def test_patch_rollover_marks_stale(self, monkeypatch):
        monkeypatch.setattr(role_updater, "get_latest_patch", lambda: "16.6")
        role_updater.save_cache(_PERCENT_WEIGHTS, "16.6")
        # New patch ships -> cache must be considered stale so it refreshes.
        monkeypatch.setattr(role_updater, "get_latest_patch", lambda: "16.7")
        assert role_updater.cache_is_stale() is True

    def test_prefix_fraction_cache_is_invalidated(self, monkeypatch):
        # A cache written by a pre-fix build: no format_version, fraction-scale,
        # stamped with the CURRENT patch. Without the format check this would
        # look fresh forever and never self-heal.
        monkeypatch.setattr(role_updater, "get_latest_patch", lambda: "16.6")
        role_updater.CACHE_PATH.write_text(json.dumps({
            "updated_at": time.time(),
            "patch": "16.6",
            "weights": _FRACTION_WEIGHTS,
        }), encoding="utf-8")
        assert role_updater.cache_is_stale() is True

    def test_missing_cache_is_stale(self):
        assert role_updater.cache_is_stale() is True
