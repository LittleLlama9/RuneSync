"""Tests for draft_recs — champ-select composition recommender.

Uses an injected profile function so no network or Data Dragon is hit.
"""
import draft_recs


_PROFILES = {
    "Darius": {"damage_type": "AD", "cc": "hard-single", "engage": False},
    "Zed": {"damage_type": "AD", "cc": "none", "engage": False},
    "Jinx": {"damage_type": "AD", "cc": "soft", "engage": False},
    "Graves": {"damage_type": "AD", "cc": "none", "engage": False},
    "Leona": {"damage_type": "AP", "cc": "hard-aoe", "engage": True},
    "Malphite": {"damage_type": "AP", "cc": "hard-aoe", "engage": True},
    "Amumu": {"damage_type": "AP", "cc": "hard-aoe", "engage": True},
    "Lux": {"damage_type": "AP", "cc": "hard-single", "engage": False},
    "Syndra": {"damage_type": "AP", "cc": "hard-single", "engage": False},
    "Vi": {"damage_type": "AD", "cc": "hard-single", "engage": True},
}


def _profile(name):
    return dict(_PROFILES.get(name, {"damage_type": "AD", "cc": "none", "engage": False}))


def _call(ally, enemy):
    return draft_recs.build_draft_recs(ally, enemy, profile_fn=_profile)


def test_none_when_empty():
    assert _call([], []) is None


def test_all_ad_team_flagged():
    rec = _call(["Darius", "Zed", "Jinx"], [])
    texts = " ".join(o["text"] for o in rec["observations"])
    assert "fully AD" in texts
    assert rec["ally"]["ad"] == 3
    assert rec["ally"]["ap"] == 0


def test_no_engage_flagged():
    rec = _call(["Darius", "Zed", "Jinx"], [])
    assert any("no reliable hard engage" in o["text"] for o in rec["observations"])


def test_enemy_ad_heavy_read():
    rec = _call([], ["Darius", "Zed", "Graves"])
    assert any("AD-heavy" in o["text"] for o in rec["observations"])
    assert rec["enemy"]["ad"] == 3


def test_enemy_heavy_cc_read():
    rec = _call([], ["Leona", "Malphite", "Amumu"])
    assert rec["enemy"]["hard_cc"] == 3
    assert any("hard-CC" in o["text"] for o in rec["observations"])


def test_strong_engage_comp_praised():
    rec = _call(["Leona", "Malphite", "Vi", "Jinx"], [])
    assert rec["ally"]["engage"] == 3
    assert any(o["level"] == "good" for o in rec["observations"])


def test_below_threshold_no_ally_warnings():
    # Only two ally picks -> not enough to draw comp conclusions.
    rec = _call(["Darius", "Zed"], [])
    assert not any(o["level"] == "warn" for o in rec["observations"])


def test_mixed_pick_not_called_fully_ad():
    # 2 AD + 1 MIXED must not be labelled "fully AD" (mixed deals both).
    profiles = dict(_PROFILES)
    profiles["Kayn"] = {"damage_type": "MIXED", "cc": "soft", "engage": False}

    def prof(name):
        return dict(profiles.get(name, {"damage_type": "AD", "cc": "none",
                                        "engage": False}))

    rec = draft_recs.build_draft_recs(["Darius", "Zed", "Kayn"], [],
                                      profile_fn=prof)
    texts = " ".join(o["text"] for o in rec["observations"])
    assert "fully AD" not in texts
    assert rec["ally"]["mixed"] == 1


def test_unknown_champ_not_counted_as_ad():
    # A champ with known=False is skipped from the damage tally.
    def prof(name):
        if name == "Ghost":
            return {"damage_type": "AD", "cc": "none", "engage": False,
                    "known": False}
        return dict(_PROFILES.get(name, {"damage_type": "AD", "cc": "none",
                                         "engage": False}))

    rec = draft_recs.build_draft_recs(["Leona", "Lux", "Ghost"], [],
                                      profile_fn=prof)
    # Only the two known AP champs counted; unknown tracked separately.
    assert rec["ally"]["count"] == 2
    assert rec["ally"]["ap"] == 2
    assert rec["ally"]["unknown"] == 1
