"""
draft_recs.py — champ-select draft/composition recommender.

Given the ally and enemy champion picks in champ select, summarises each team's
damage balance, engage, and crowd-control profile and surfaces neutral drafting
observations (e.g. "your comp has no reliable engage", "enemy is AD-heavy").
This analyses CHAMPION game data only — it never rates or judges any player.

Pure logic; the champion_profile accessor is injectable so tests need no network.
"""
from typing import Optional


def _summarise(names, profile_fn) -> dict:
    ad = ap = mixed = engage = hard_cc = soft_cc = 0
    known = unknown = 0
    for name in names:
        if not name:
            continue
        prof = profile_fn(name)
        # Skip champions with no curated data (e.g. a brand-new champ): counting
        # them as AD by default would distort the damage-balance read.
        if not prof.get("known", True):
            unknown += 1
            continue
        known += 1
        dt = prof.get("damage_type") or "AD"
        if dt == "AD":
            ad += 1
        elif dt == "AP":
            ap += 1
        else:
            mixed += 1
        if prof.get("engage"):
            engage += 1
        cc = prof.get("cc") or "none"
        if cc in ("hard-single", "hard-aoe"):
            hard_cc += 1
        elif cc == "soft":
            soft_cc += 1
    return {"ad": ad, "ap": ap, "mixed": mixed, "engage": engage,
            "hard_cc": hard_cc, "soft_cc": soft_cc, "count": known,
            "unknown": unknown}


def build_draft_recs(ally_champs, enemy_champs, *, profile_fn=None) -> Optional[dict]:
    """Return a UI-ready draft analysis dict, or None if there's nothing to analyse.

    ally_champs / enemy_champs are lists of champion display names (locked or
    hovered). profile_fn(name)->{damage_type,cc,engage,...} defaults to the live
    champion_profile module.
    """
    ally = [c for c in (ally_champs or []) if c]
    enemy = [c for c in (enemy_champs or []) if c]
    if not ally and not enemy:
        return None
    if profile_fn is None:
        import champion_profile
        profile_fn = champion_profile.profile

    a = _summarise(ally, profile_fn)
    e = _summarise(enemy, profile_fn)

    obs = []

    # ── Ally-side drafting gaps (actionable while you can still pick) ──────────
    if a["count"] >= 3:
        if a["engage"] == 0:
            obs.append({"level": "warn", "short": "No hard engage",
                        "text": "Your team has no reliable hard engage — consider "
                                "a pick with lockdown to start fights."})
        if a["hard_cc"] == 0:
            if a["soft_cc"] >= 2:
                # Soft CC (slows/short roots) peels but can't lock a target down;
                # distinguish it from a genuinely CC-less comp.
                obs.append({"level": "warn", "short": "Soft CC only, no lockdown",
                            "text": "Your CC is all soft (slows) — enough to peel, "
                                    "but no reliable lockdown for priority targets."})
            else:
                obs.append({"level": "warn", "short": "No hard CC",
                            "text": "Your team has no hard CC — you may struggle to "
                                    "lock down priority targets."})
        # All-one-damage-type comps let the enemy stack a single resist. Only
        # call it "fully" AD/AP when there are no mixed-damage picks either.
        if a["ap"] == 0 and a["mixed"] == 0 and a["ad"] >= 3:
            obs.append({"level": "warn", "short": "All AD, add magic dmg",
                        "text": "Your team is fully AD — enemies can stack armor. "
                                "A magic-damage pick would diversify."})
        elif a["ad"] == 0 and a["mixed"] == 0 and a["ap"] >= 3:
            obs.append({"level": "warn", "short": "All AP, add AD threat",
                        "text": "Your team is fully AP — enemies can stack magic "
                                "resist. An AD pick would diversify."})

    # ── Enemy-side reads (inform your pick / your early itemisation) ───────────
    if e["count"] >= 3:
        if e["ap"] == 0 and e["mixed"] == 0 and e["ad"] >= 3:
            obs.append({"level": "info", "short": "Enemy AD-heavy: buy armor",
                        "text": "Enemy comp is AD-heavy — armor will be efficient "
                                "and MR picks give up less."})
        elif e["ad"] == 0 and e["mixed"] == 0 and e["ap"] >= 3:
            obs.append({"level": "info", "short": "Enemy AP-heavy: buy MR",
                        "text": "Enemy comp is AP-heavy — magic resist will be "
                                "efficient this game."})

        # Wombo: heavy engage AND heavy hard CC. One consolidated warn instead of
        # two stacked info lines (the CC and engage reads below).
        if e["engage"] >= 2 and e["hard_cc"] >= 3:
            obs.append({"level": "warn", "short": "Enemy wombo, disengage+peel",
                        "text": "Enemy has strong engage and CC — expect all-ins; "
                                "prioritise disengage, flank vision, and peel."})
        else:
            if e["hard_cc"] >= 3:
                # Much of League's hard CC is knockup/displacement, which
                # tenacity does NOT reduce — lead with positioning/peel.
                obs.append({"level": "info",
                            "short": f"{e['hard_cc']} hard CC: peel/tenacity",
                            "text": f"Enemy has {e['hard_cc']} hard-CC champions — "
                                    "positioning and peel matter most; tenacity "
                                    "helps vs stuns/roots (not knockups)."})
            if e["engage"] >= 2:
                obs.append({"level": "info",
                            "short": f"{e['engage']} engage tools, disengage",
                            "text": f"Enemy has {e['engage']} engage tools — "
                                    "disengage and vision around fights matter."})

        if e["engage"] == 0:
            obs.append({"level": "info", "short": "Enemy no engage, group free",
                        "text": "Enemy has no reliable hard engage — you can group, "
                                "take vision, and pick your fights; sidelaning is "
                                "lower-risk."})

    if a["count"] >= 3 and a["engage"] >= 2 and a["hard_cc"] >= 2:
        obs.append({"level": "good", "short": "Strong engage+CC, force it",
                    "text": "Your comp has strong engage and CC — look for "
                            "opportunities to force fights."})

    return {"ally": a, "enemy": e, "observations": obs}
