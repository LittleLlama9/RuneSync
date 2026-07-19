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
    # Leona/Malphite/Amumu are all engage + hard-aoe -> the consolidated
    # "wombo" warn fires (and suppresses the separate CC/engage info lines).
    rec = _call([], ["Leona", "Malphite", "Amumu"])
    assert rec["enemy"]["hard_cc"] == 3
    assert rec["enemy"]["engage"] == 3
    assert any("strong engage and CC" in o["text"] for o in rec["observations"])
    # No stacked "hard-CC champions" / "engage tools" lines when wombo fires.
    assert not any("engage tools" in o["text"] for o in rec["observations"])


def test_enemy_hard_cc_without_engage_reads_cc_line():
    # 3 hard-CC champs but not enough engage -> the CC read fires (not wombo),
    # and the tenacity caveat about knockups is present.
    rec = _call([], ["Lux", "Syndra", "Darius"])
    assert rec["enemy"]["hard_cc"] == 3
    assert rec["enemy"]["engage"] == 0
    cc = [o for o in rec["observations"] if "hard-CC champions" in o["text"]]
    assert cc and "not knockups" in cc[0]["text"]


def test_enemy_no_engage_reassurance():
    rec = _call([], ["Lux", "Syndra", "Zed"])
    assert rec["enemy"]["engage"] == 0
    assert any("no reliable hard engage" in o["text"] and o["level"] == "info"
               for o in rec["observations"])


def test_soft_cc_only_refines_no_hard_cc_line():
    # Ally has zero hard CC but multiple soft-CC picks -> the softer wording.
    def prof(name):
        table = {
            "Jinx": {"damage_type": "AD", "cc": "soft", "engage": False},
            "Ashe": {"damage_type": "AD", "cc": "soft", "engage": False},
            "Zed": {"damage_type": "AD", "cc": "none", "engage": False},
        }
        return dict(table.get(name, {"damage_type": "AD", "cc": "none",
                                     "engage": False}))

    rec = draft_recs.build_draft_recs(["Jinx", "Ashe", "Zed"], [], profile_fn=prof)
    assert rec["ally"]["hard_cc"] == 0 and rec["ally"]["soft_cc"] == 2
    texts = " ".join(o["text"] for o in rec["observations"])
    assert "all soft" in texts
    assert "no hard CC" not in texts.lower()


def test_every_observation_has_short_form():
    # The overlay renders `short`; every emitted observation must supply one and
    # it must stay compact enough for the narrow panel.
    combos = [
        (["Darius", "Zed", "Jinx"], ["Leona", "Malphite", "Amumu"]),
        (["Leona", "Malphite", "Vi", "Jinx"], ["Lux", "Syndra", "Darius"]),
        (["Lux", "Syndra"], ["Darius", "Zed", "Graves"]),
    ]
    for ally, enemy in combos:
        rec = _call(ally, enemy)
        for o in rec["observations"]:
            assert o.get("short"), f"missing short: {o}"
            assert len(o["short"]) <= 30, f"short too long: {o['short']!r}"


# ── champion suggestions (suggest_picks + picks wiring) ───────────────────────
_SUGGEST_ATTRS = {
    "Alpha":   {"damage_type": "AD", "cc": "none", "engage": True, "known": True},
    "Bravo":   {"damage_type": "AD", "cc": "none", "engage": True, "known": True},
    "Charlie": {"damage_type": "AP", "cc": "hard-aoe", "engage": False, "known": True},
    "Delta":   {"damage_type": "AD", "cc": "none", "engage": True, "known": False},
}
_SUGGEST_WEIGHTS = {
    "Alpha":   {"top": 80.0},
    "Bravo":   {"top": 40.0, "jungle": 30.0},
    "Charlie": {"top": 55.0},
    "Delta":   {"top": 95.0},
}


def _sp(trait, role, taken=(), limit=3):
    return draft_recs.suggest_picks(
        trait, role, taken,
        attrs_fn=lambda n: dict(_SUGGEST_ATTRS[n]),
        roles_fn=lambda n: _SUGGEST_WEIGHTS.get(n, {}),
        pool=list(_SUGGEST_ATTRS), limit=limit)


def test_suggest_picks_ranks_by_role_rate_and_filters_trait():
    # Only engage champs playable top, ranked by top play-rate. Charlie has no
    # engage; Delta is not in the curated catalog (known=False) -> both dropped.
    assert _sp("engage", "top") == ["Alpha", "Bravo"]


def test_suggest_picks_excludes_taken_and_below_threshold():
    # Alpha is taken; Bravo plays top at only 40% here but is above 12% -> kept.
    assert _sp("engage", "top", taken={"Alpha"}) == ["Bravo"]
    # Charlie only fits the hard_cc trait.
    assert _sp("hard_cc", "top") == ["Charlie"]


def test_suggest_picks_unknown_role_returns_empty():
    assert _sp("engage", "auto") == []
    assert _sp("engage", "") == []


def test_ally_gaps_include_champion_picks():
    def fake_suggest(trait, limit=3):
        return {"engage": ["Leona", "Nautilus"], "ap": ["Sylas"]}.get(trait, [])

    rec = draft_recs.build_draft_recs(["Darius", "Zed", "Jinx"], [],
                                      profile_fn=_profile, suggest_fn=fake_suggest)
    eng = next(o for o in rec["observations"] if o["short"] == "No hard engage")
    assert eng["picks"] == ["Leona", "Nautilus"]
    ad = next(o for o in rec["observations"] if o["short"] == "All AD, add magic dmg")
    assert ad["picks"] == ["Sylas"]


def test_enemy_reads_have_no_picks():
    # Enemy-side observations are not pickable gaps -> never get a picks list,
    # even if the suggester would return champions.
    rec = draft_recs.build_draft_recs([], ["Darius", "Zed", "Graves"],
                                      profile_fn=_profile,
                                      suggest_fn=lambda trait, limit=3: ["Nope"])
    assert rec["observations"]
    assert all("picks" not in o for o in rec["observations"])


def test_empty_picks_omits_key():
    rec = draft_recs.build_draft_recs(["Darius", "Zed", "Jinx"], [],
                                      profile_fn=_profile,
                                      suggest_fn=lambda trait, limit=3: [])
    eng = next(o for o in rec["observations"] if o["short"] == "No hard engage")
    assert "picks" not in eng


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
