"""Tests for item_recs — dynamic in-game defensive item recommender.

Uses injected profile/gold/item functions so no network or Data Dragon is hit.
"""
import item_recs


# Toy champion damage/CC profiles.
_PROFILES = {
    "Darius": {"damage_type": "AD", "cc": "hard-single", "engage": False},
    "Zed": {"damage_type": "AD", "cc": "none", "engage": False},
    "Jinx": {"damage_type": "AD", "cc": "soft", "engage": False},
    "Leona": {"damage_type": "AP", "cc": "hard-aoe", "engage": True},
    "Lux": {"damage_type": "AP", "cc": "hard-single", "engage": False},
    "Syndra": {"damage_type": "AP", "cc": "hard-single", "engage": False},
    "Kai'Sa": {"damage_type": "MIXED", "cc": "none", "engage": False},
    "Me": {"damage_type": "AD", "cc": "none", "engage": False},
}

_PRICES = {3153: 3200, 3078: 3300, 6672: 3400, 3006: 1100}
_ARMOR = [{"id": 3075, "name": "Thornmail", "image": "a.png", "gold": 2700, "value": 70},
          {"id": 3110, "name": "Frozen Heart", "image": "b.png", "gold": 2500, "value": 70}]
_MR = [{"id": 3065, "name": "Spirit Visage", "image": "c.png", "gold": 2900, "value": 55},
       {"id": 3156, "name": "Maw", "image": "d.png", "gold": 3100, "value": 40}]


def _profile(name):
    return dict(_PROFILES.get(name, {"damage_type": "AD", "cc": "none", "engage": False}))


def _gold(item_ids):
    return sum(_PRICES.get(i, 0) for i in (item_ids or []))


def _items(kind, min_gold=800):
    return list(_ARMOR if kind == "armor" else _MR if kind == "mr" else [])


def _p(name, team, champ, items=None):
    return {"summonerName": name, "championName": champ, "team": team,
            "position": "MIDDLE", "isBot": False,
            "items": [{"itemID": i} for i in (items or [])]}


def _game(enemy_champs, enemy_items=None, game_time=900.0):
    enemy_items = enemy_items or {}
    players = [_p("Me", "ORDER", "Me", items=[6672])]
    for i, c in enumerate(enemy_champs):
        players.append(_p(f"E{i}", "CHAOS", c, items=enemy_items.get(c)))
    return {"activePlayer": {"summonerName": "Me"}, "allPlayers": players,
            "gameData": {"gameTime": game_time}}


def _call(data):
    return item_recs.build_item_recs(data, profile_fn=_profile, gold_fn=_gold,
                                     items_fn=_items)


def test_none_on_garbage():
    assert _call(None) is None
    assert _call({}) is None


def test_ad_heavy_enemy_recommends_armor():
    rec = _call(_game(["Darius", "Zed", "Jinx"]))
    assert rec["primary"] == "armor"
    kinds = [s["kind"] for s in rec["suggestions"]]
    assert kinds == ["armor"]
    assert rec["suggestions"][0]["items"][0]["name"] == "Thornmail"


def test_ap_heavy_enemy_recommends_mr():
    rec = _call(_game(["Leona", "Lux", "Syndra"]))
    assert rec["primary"] == "mr"
    assert [s["kind"] for s in rec["suggestions"]] == ["mr"]


def test_balanced_enemy_recommends_both():
    rec = _call(_game(["Darius", "Zed", "Leona", "Lux"]))
    assert rec["primary"] == "mixed"
    assert set(s["kind"] for s in rec["suggestions"]) == {"armor", "mr"}


def test_biggest_threat_is_richest_enemy():
    rec = _call(_game(["Darius", "Syndra"],
                      enemy_items={"Syndra": [3153, 3078]}))  # 6500 gold
    assert rec["biggest_threat"]["champion"] == "Syndra"
    assert rec["biggest_threat"]["est_gold"] == 6500


def test_hard_cc_note_emitted():
    rec = _call(_game(["Leona", "Lux", "Syndra"]))
    assert rec["hard_cc"] == 3
    assert any("hard-CC" in n for n in rec["notes"])


def test_mixed_champion_splits_threat():
    rec = _call(_game(["Kai'Sa", "Kai'Sa", "Kai'Sa"]))
    # All-MIXED -> neither side lopsided -> mixed suggestions.
    assert rec["primary"] == "mixed"
    assert abs(rec["threats"]["ad_pct"] - 0.5) < 1e-6


def test_unknown_champ_skipped():
    # An enemy with no curated data (known=False) must not count as AD.
    profiles = dict(_PROFILES)
    profiles["Newbie"] = {"damage_type": "AD", "cc": "none",
                          "engage": False, "known": False}

    def prof(name):
        return dict(profiles.get(name, {"damage_type": "AD", "cc": "none",
                                        "engage": False, "known": False}))

    data = _game(["Leona", "Lux", "Newbie"])  # 2 AP + 1 unknown
    rec = item_recs.build_item_recs(data, profile_fn=prof, gold_fn=_gold,
                                    items_fn=_items)
    # Unknown skipped -> still reads as fully AP -> armor never suggested.
    assert rec["primary"] == "mr"
    assert [s["kind"] for s in rec["suggestions"]] == ["mr"]


def test_all_unknown_enemies_returns_none():
    def prof(name):
        return {"damage_type": "AD", "cc": "none", "engage": False,
                "known": False}

    assert item_recs.build_item_recs(_game(["A", "B", "C"]), profile_fn=prof,
                                     gold_fn=_gold, items_fn=_items) is None
