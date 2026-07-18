"""Tests for the botlane duo ("best pair") recommender.

Covers the four surfaces of the feature:
  * duo_bands       — the shared read/build winrate band + synergy tiers.
  * ugg_api         — get_best_partners(): reads the bundle `duos` section,
                      clamps to the sane band, sorts, and degrades to [] when
                      the bundle predates duo data (ships inert).
  * monitor         — locked-partner detection: fires only when your botlane
                      lane partner has LOCKED and you have not locked yet.
  * build_data_bundle — the UNVERIFIED u.gg `champion_duos` parser and its
                      band/games curation (shape is documented-guess; these
                      lock the normalization we WILL apply once confirmed).
"""
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import duo_bands
from duo_bands import is_duo_wr, duo_tier
import ugg_api
from monitor import ChampSelectMonitor

# The bundle builder is a local, gitignored dev tool — absent on CI. Its duo
# parser tests only run where the file exists; the client/monitor/bands tests
# (the shipped surfaces) always run.
_BUILDER_PATH = (Path(__file__).resolve().parent.parent
                 / "scripts" / "build_data_bundle.py")


# ── duo_bands ────────────────────────────────────────────────────────────────
class TestDuoBands:
    def test_read_floor_is_lenient_build_floor_is_stricter(self):
        assert duo_bands.DUO_WR_READ_MIN < duo_bands.DUO_WR_BUILD_MIN
        assert duo_bands.DUO_WR_BUILD_MIN > 50.0  # must beat a coin flip

    def test_max_clamps_small_sample_flukes(self):
        assert duo_bands.DUO_WR_MAX == 60.0

    def test_is_duo_wr_band(self):
        assert is_duo_wr(44.0)          # at read floor
        assert is_duo_wr(53.2)
        assert is_duo_wr(60.0)          # at max
        assert not is_duo_wr(43.9)
        assert not is_duo_wr(60.1)
        assert not is_duo_wr(72.0)      # fluke

    def test_is_duo_wr_rejects_non_numeric_and_bool(self):
        assert not is_duo_wr(None)
        assert not is_duo_wr("54")
        assert not is_duo_wr([])
        assert not is_duo_wr(True)      # bool subclasses int; 1.0 is out of band

    def test_tiers_are_ordered(self):
        assert duo_tier(56.0)[0] == "S"
        assert duo_tier(53.5)[0] == "A"
        assert duo_tier(51.2)[0] == "B"
        assert duo_tier(45.0)[0] == "C"

    def test_tier_returns_label(self):
        letter, label = duo_tier(56.0)
        assert letter == "S" and isinstance(label, str) and label


# ── ugg_api.get_best_partners ────────────────────────────────────────────────
def _install_bundle(monkeypatch, bundle):
    monkeypatch.setattr(ugg_api, "_bundle", bundle, raising=False)
    ugg_api._bundle_ready_event.set()


class TestGetBestPartners:
    def _bundle(self):
        return {"duos": {"leona": {"support": [
            {"champion": "Kai'Sa", "role": "bot", "win_rate": 54.3, "games": 1200},
            {"champion": "Jhin", "role": "bot", "win_rate": 53.1, "games": 800},
            {"champion": "Ezreal", "role": "bot", "win_rate": 49.0, "games": 900},
            {"champion": "Fluke", "role": "bot", "win_rate": 72.0, "games": 12},
        ]}}}

    def test_returns_sorted_clamped_partners(self, monkeypatch):
        _install_bundle(monkeypatch, self._bundle())
        recs = ugg_api.UGGClient().get_best_partners("Leona", "support")
        names = [r["champion"] for r in recs]
        assert names == ["Kai'Sa", "Jhin", "Ezreal"]   # Fluke (72%) clamped out
        assert recs[0]["win_rate"] >= recs[1]["win_rate"] >= recs[2]["win_rate"]
        assert recs[0]["tier"] == "A" and recs[0]["tier_label"]

    def test_top_n_limit(self, monkeypatch):
        _install_bundle(monkeypatch, self._bundle())
        recs = ugg_api.UGGClient().get_best_partners("Leona", "support", top_n=1)
        assert len(recs) == 1 and recs[0]["champion"] == "Kai'Sa"

    def test_adc_alias_normalizes_to_bot(self, monkeypatch):
        _install_bundle(monkeypatch, {"duos": {"jinx": {"bot": [
            {"champion": "Lulu", "role": "support", "win_rate": 53.0, "games": 500},
        ]}}})
        recs = ugg_api.UGGClient().get_best_partners("Jinx", "adc")
        assert [r["champion"] for r in recs] == ["Lulu"]

    def test_missing_champ_returns_empty(self, monkeypatch):
        _install_bundle(monkeypatch, self._bundle())
        assert ugg_api.UGGClient().get_best_partners("Zyra", "support") == []

    def test_non_botlane_role_returns_empty(self, monkeypatch):
        _install_bundle(monkeypatch, self._bundle())
        assert ugg_api.UGGClient().get_best_partners("Leona", "top") == []

    def test_ships_inert_when_no_duos_section(self, monkeypatch):
        # An older bundle with no `duos` key must degrade silently to [].
        _install_bundle(monkeypatch, {"matchups": {}, "builds": {}})
        assert ugg_api.UGGClient().get_best_partners("Leona", "support") == []

    def test_ships_inert_when_no_bundle(self, monkeypatch):
        monkeypatch.setattr(ugg_api, "_bundle", None, raising=False)
        ugg_api._bundle_ready_event.set()
        assert ugg_api.UGGClient().get_best_partners("Leona", "support") == []

    def test_malformed_entries_are_skipped(self, monkeypatch):
        _install_bundle(monkeypatch, {"duos": {"leona": {"support": [
            {"champion": "Kai'Sa", "win_rate": 54.3, "games": 1200},
            {"win_rate": 53.0},                 # no champion
            {"champion": "Bad", "win_rate": None},
            "not-a-dict",
        ]}}})
        recs = ugg_api.UGGClient().get_best_partners("Leona", "support")
        assert [r["champion"] for r in recs] == ["Kai'Sa"]


# ── monitor locked-partner detection ─────────────────────────────────────────
def _monitor(my_role="bot"):
    mon = ChampSelectMonitor(
        lcu=MagicMock(), ugg=MagicMock(), overrides=MagicMock(),
        on_log=lambda *a, **k: None,
    )
    mon._my_role = my_role
    mon._champ_name_map = {111: "Jinx", 222: "Leona", 333: "Thresh", 444: "Garen"}
    return mon


def _session(*, my_cell, my_pos, my_completed, partner_cell, partner_pos,
             partner_champ, partner_completed):
    """Build a minimal champ-select session with me + a botlane partner."""
    return {
        "localPlayerCellId": my_cell,
        "myTeam": [
            {"cellId": my_cell, "assignedPosition": my_pos},
            {"cellId": partner_cell, "assignedPosition": partner_pos},
        ],
        "actions": [[
            {"actorCellId": my_cell, "type": "pick", "championId": 111,
             "completed": my_completed},
            {"actorCellId": partner_cell, "type": "pick",
             "championId": partner_champ, "completed": partner_completed},
        ]],
    }


class TestLockedPartnerDetection:
    def test_detects_locked_support_partner_when_i_am_adc(self):
        mon = _monitor("bot")
        s = _session(my_cell=1, my_pos="bottom", my_completed=False,
                     partner_cell=2, partner_pos="utility", partner_champ=222,
                     partner_completed=True)
        assert mon._get_locked_botlane_partner(s) == ("Leona", "support")

    def test_detects_locked_adc_partner_when_i_am_support(self):
        mon = _monitor("support")
        s = _session(my_cell=2, my_pos="utility", my_completed=False,
                     partner_cell=1, partner_pos="bottom", partner_champ=111,
                     partner_completed=True)
        assert mon._get_locked_botlane_partner(s) == ("Jinx", "bot")

    def test_no_partner_when_partner_only_hovering(self):
        mon = _monitor("bot")
        s = _session(my_cell=1, my_pos="bottom", my_completed=False,
                     partner_cell=2, partner_pos="utility", partner_champ=222,
                     partner_completed=False)   # hovered, not locked
        assert mon._get_locked_botlane_partner(s) == (None, None)

    def test_no_partner_for_non_botlane_role(self):
        mon = _monitor("top")
        s = _session(my_cell=1, my_pos="top", my_completed=False,
                     partner_cell=2, partner_pos="utility", partner_champ=222,
                     partner_completed=True)
        assert mon._get_locked_botlane_partner(s) == (None, None)

    def test_my_pick_completed_flag(self):
        mon = _monitor("bot")
        locked = _session(my_cell=1, my_pos="bottom", my_completed=True,
                          partner_cell=2, partner_pos="utility", partner_champ=222,
                          partner_completed=True)
        unlocked = _session(my_cell=1, my_pos="bottom", my_completed=False,
                            partner_cell=2, partner_pos="utility", partner_champ=222,
                            partner_completed=True)
        assert mon._my_pick_completed(locked) is True
        assert mon._my_pick_completed(unlocked) is False

    def test_run_duo_lookup_fires_callback_with_recs(self):
        mon = _monitor("bot")
        fired = {}
        mon._on_duo_recommendations = lambda p, pr, mr, recs: fired.update(
            partner=p, partner_role=pr, my_role=mr, recs=recs)
        mon.ugg.get_best_partners.return_value = [
            {"champion": "Kai'Sa", "win_rate": 54.3, "games": 1200,
             "tier": "A", "tier_label": "Strong pairing"}]
        mon._run_duo_lookup("Leona", "support", "bot")
        assert fired["partner"] == "Leona" and fired["my_role"] == "bot"
        assert fired["recs"][0]["champion"] == "Kai'Sa"

    def test_run_duo_lookup_stays_inert_on_empty(self):
        mon = _monitor("bot")
        fired = {}
        mon._on_duo_recommendations = lambda p, pr, mr, recs: fired.update(recs=recs)
        mon.ugg.get_best_partners.return_value = []
        mon._run_duo_lookup("Leona", "support", "bot")
        # Callback still fires (so the UI panel clears) but with no recs.
        assert fired == {"recs": []}


# ── build_data_bundle duo parser (UNVERIFIED shape) ──────────────────────────
def _load_builder():
    spec = importlib.util.spec_from_file_location("bdb_duo", str(_BUILDER_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not _BUILDER_PATH.exists(),
                    reason="gitignored dev builder not present (e.g. CI)")
class TestBuilderDuoParser:
    ID_TO_NAME = {"412": "Thresh", "497": "Rakan", "555": "Pyke", "99": "Lux"}

    def _payload_4field(self):
        # region 12 -> rank 10 -> role_id "3" (bot) -> [[ entries ]]
        # entry: [partner_id, partner_role_id(2=support), wins, matches]
        return {"12": {"10": {"3": [[
            ["412", 2, 650, 1200],   # Thresh 54.2%
            ["497", 2, 300, 900],    # Rakan 33% -> below build floor, dropped
            ["555", 2, 520, 1000],   # Pyke 52.0%
            ["99", 2, 80, 120],      # Lux 66.7% -> above max, dropped
            ["412", 2, 40, 50],      # Thresh again but <100 games -> dropped
        ]]}}}

    def test_parser_is_marked_unverified_by_default(self):
        # The shipped-safe default: duo data must not reach a bundle until the
        # champion_duos shape is confirmed and this flag is deliberately flipped.
        mod = _load_builder()
        assert mod._DUO_PARSER_VERIFIED is False

    def test_parses_and_curates_botlane_pairs(self, monkeypatch):
        mod = _load_builder()
        monkeypatch.setattr(mod, "_ugg_fetch", lambda url: self._payload_4field())
        out = mod.fetch_duos_ugg("16.14.1", "1.5.0", "222", "Jinx",
                                 ["bot"], self.ID_TO_NAME)
        names = [e["champion"] for e in out["bot"]]
        assert names == ["Thresh", "Pyke"]           # sorted desc, flukes dropped
        assert out["bot"][0]["role"] == "support"
        assert out["bot"][0]["games"] == 1200

    def test_three_field_shape_infers_complementary_role(self, monkeypatch):
        mod = _load_builder()
        payload = {"12": {"10": {"3": [[["412", 650, 1200], ["555", 520, 1000]]]}}}
        monkeypatch.setattr(mod, "_ugg_fetch", lambda url: payload)
        out = mod.fetch_duos_ugg("16.14.1", "1.5.0", "222", "Jinx",
                                 ["bot"], self.ID_TO_NAME)
        assert out["bot"][0]["role"] == "support"

    def test_non_botlane_role_yields_nothing(self, monkeypatch):
        mod = _load_builder()
        monkeypatch.setattr(mod, "_ugg_fetch", lambda url: self._payload_4field())
        assert mod.fetch_duos_ugg("16.14.1", "1.5.0", "222", "Jinx",
                                  ["top"], self.ID_TO_NAME) == {}

    def test_no_upstream_data_yields_nothing(self, monkeypatch):
        mod = _load_builder()
        monkeypatch.setattr(mod, "_ugg_fetch", lambda url: None)
        assert mod.fetch_duos_ugg("16.14.1", "1.5.0", "222", "Jinx",
                                  ["bot"], self.ID_TO_NAME) == {}

    def test_bundle_schema_matches_client_read_path(self, monkeypatch):
        # The builder output for an anchor must be directly readable by the
        # client's get_best_partners without any reshaping.
        mod = _load_builder()
        monkeypatch.setattr(mod, "_ugg_fetch", lambda url: self._payload_4field())
        out = mod.fetch_duos_ugg("16.14.1", "1.5.0", "222", "Jinx",
                                 ["bot"], self.ID_TO_NAME)
        bundle = {"duos": {"jinx": out}}
        monkeypatch.setattr(ugg_api, "_bundle", bundle, raising=False)
        ugg_api._bundle_ready_event.set()
        recs = ugg_api.UGGClient().get_best_partners("Jinx", "bot")
        assert [r["champion"] for r in recs] == ["Thresh", "Pyke"]
