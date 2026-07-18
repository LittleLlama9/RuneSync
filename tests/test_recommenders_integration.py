"""Integration tests: recommenders run against the SHIPPED champion_attrs.json.

These verify the curated catalog is present, complete, well-formed, and that the
draft/item recommenders produce sensible output end-to-end with the real profile
loader (champion_data classes fall back to empty without network — fine).
"""
import json
import os

import champion_profile
import draft_recs
import item_recs
from champion_roles import ROLE_WEIGHTS

_ATTRS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                           "score_v2", "champion_attrs.json")

_VALID_DT = {"AD", "AP", "MIXED"}
_VALID_CC = {"none", "soft", "hard-single", "hard-aoe"}


def _load():
    with open(_ATTRS_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_catalog_present_and_well_formed():
    d = _load()
    assert len(d) >= 170
    for name, v in d.items():
        assert set(v) == {"damage_type", "cc", "engage"}, name
        assert v["damage_type"] in _VALID_DT, name
        assert v["cc"] in _VALID_CC, name
        assert isinstance(v["engage"], bool), name


def test_catalog_covers_role_database():
    """Every champion RuneSync knows a role for must have an attribute entry, so
    the recommenders never silently fall back to defaults for a live pick."""
    d = _load()
    missing = sorted(set(ROLE_WEIGHTS) - set(d))
    assert not missing, f"champions missing from champion_attrs.json: {missing}"


def test_catalog_includes_recent_champion_locke():
    # Locke ships in Data Dragon 16.14.1 but not the older ROLE_WEIGHTS table,
    # so guard it explicitly to prevent a silent default-profile regression.
    d = _load()
    assert "Locke" in d, "Locke missing from champion_attrs.json"


def test_profile_loader_returns_catalog_values():
    prof = champion_profile.profile("Malphite")
    assert prof["cc"] == "hard-aoe"
    assert prof["engage"] is True
    assert prof["known"] is True


def test_profile_unknown_champion_safe_default():
    prof = champion_profile.profile("Totally Not A Champion 9000")
    assert prof["damage_type"] in _VALID_DT
    assert prof["cc"] == "none"
    assert prof["engage"] is False
    assert prof["known"] is False


def test_draft_recs_end_to_end_real_catalog():
    # A fully-AD, no-engage ally comp should be flagged.
    rec = draft_recs.build_draft_recs(
        ["Darius", "Zed", "Jinx", "Graves"], ["Malphite", "Amumu", "Leona"])
    assert rec is not None
    texts = " ".join(o["text"] for o in rec["observations"])
    assert "fully AD" in texts
    # Enemy is engage/CC heavy -> should be noted.
    assert rec["enemy"]["hard_cc"] >= 2


def test_item_recs_end_to_end_real_catalog():
    def _p(name, team, champ):
        return {"summonerName": name, "championName": champ, "team": team,
                "position": "MIDDLE", "isBot": False, "items": []}
    data = {
        "activePlayer": {"summonerName": "Me"},
        "allPlayers": [
            _p("Me", "ORDER", "Garen"),
            _p("E0", "CHAOS", "Darius"),
            _p("E1", "CHAOS", "Zed"),
            _p("E2", "CHAOS", "Graves"),
        ],
        "gameData": {"gameTime": 600.0},
    }
    # Use the real profile loader; inject a no-network item source.
    rec = item_recs.build_item_recs(
        data, items_fn=lambda kind, mg=800: [{"id": 1, "name": "Plated Steelcaps",
                                              "image": "x.png", "gold": 1100, "value": 20}]
        if kind == "armor" else [])
    assert rec["primary"] == "armor"
    assert any(s["kind"] == "armor" for s in rec["suggestions"])
