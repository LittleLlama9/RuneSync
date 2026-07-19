"""
draft_recs.py — champ-select draft/composition recommender.

Given the ally and enemy champion picks in champ select, summarises each team's
damage balance, engage, and crowd-control profile and surfaces neutral drafting
observations (e.g. "your comp has no reliable engage", "enemy is AD-heavy").
Fixable ally-side gaps also name real, role-appropriate champions the user could
still pick to fill them (a `picks` list), filtered to their assigned role and
excluding champs already picked or banned. This analyses CHAMPION game data only
— it never rates or judges any player.

Pure logic; the champion_profile accessor is injectable so tests need no network.
"""
from typing import Optional


# ── champion suggestion engine ────────────────────────────────────────────────
# When the draft has a fixable gap (no engage, no hard CC, one-dimensional
# damage), we don't just say "you lack X" — we name real champions the user
# could still pick that fill it. Suggestions are filtered to the user's assigned
# role (so they're actually pickable) and exclude champs already picked/banned.
_ROLE_MIN_PCT = 12.0    # a champ "plays" a role at >= this % of its games
_PLAYABLE_ROLES = ("top", "jungle", "mid", "bot", "support")

_TRAIT_TESTS = {
    "engage":  lambda a: bool(a.get("engage")),
    "hard_cc": lambda a: a.get("cc") in ("hard-single", "hard-aoe"),
    "ap":      lambda a: a.get("damage_type") in ("AP", "MIXED"),
    "ad":      lambda a: a.get("damage_type") in ("AD", "MIXED"),
}


def suggest_picks(trait, role, taken, *, attrs_fn, roles_fn, pool,
                  limit: int = 3, min_pct: float = _ROLE_MIN_PCT):
    """Return up to `limit` champion names that supply `trait` and are played in
    `role`, ranked by how often they're played there (a comfort/reliability
    proxy). Excludes anything in `taken` (picked or banned) and champs missing
    from the curated catalog. Returns [] when the role is unknown/invalid so the
    caller degrades to advice-only.

    Injectable data sources keep it unit-testable:
      * attrs_fn(name)  -> {damage_type, cc, engage, known}
      * roles_fn(name)  -> {role: play_pct, ...}
      * pool            -> iterable of candidate champion names
    """
    role = (role or "").lower()
    if role not in _PLAYABLE_ROLES:
        return []
    test = _TRAIT_TESTS.get(trait)
    if test is None:
        return []
    taken = set(taken or ())
    scored = []
    for champ in pool:
        if champ in taken:
            continue
        w = (roles_fn(champ) or {}).get(role, 0.0)
        if w < min_pct:
            continue
        a = attrs_fn(champ)
        if not a.get("known", True):
            continue
        if test(a):
            scored.append((w, champ))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [c for _, c in scored[:limit]]


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


def build_draft_recs(ally_champs, enemy_champs, *, profile_fn=None,
                     my_role: str = "", taken=None, suggest_fn=None,
                     roles_fn=None) -> Optional[dict]:
    """Return a UI-ready draft analysis dict, or None if there's nothing to analyse.

    ally_champs / enemy_champs are lists of champion display names (locked or
    hovered). profile_fn(name)->{damage_type,cc,engage,...} defaults to the live
    champion_profile module.

    `my_role` (top/jungle/mid/bot/support) and `taken` (champs already picked or
    banned) drive concrete champion suggestions for the fixable ally-side gaps:
    each such observation gets a `picks` list of real, role-appropriate champs
    the user could still pick. When the role is unknown or no candidate fits,
    the observation stays advice-only. `suggest_fn`/`roles_fn` are injectable for
    tests.
    """
    ally = [c for c in (ally_champs or []) if c]
    enemy = [c for c in (enemy_champs or []) if c]
    if not ally and not enemy:
        return None
    if profile_fn is None:
        import champion_profile
        profile_fn = champion_profile.profile

    # Default suggestion engine: curated attrs + role pick-rates over the whole
    # catalog, minus everything already off the board. Built lazily so tests can
    # inject `suggest_fn` and skip the real data entirely.
    if suggest_fn is None:
        try:
            import champion_profile
            import champion_roles
            _pool = tuple(champion_profile.known_champions())
            _attrs_fn = champion_profile.attrs_for
            _roles_fn = roles_fn or champion_roles.get_role_weights
            _taken_all = set(ally) | set(enemy) | set(taken or ())

            def suggest_fn(trait, limit=3):
                return suggest_picks(trait, my_role, _taken_all,
                                     attrs_fn=_attrs_fn, roles_fn=_roles_fn,
                                     pool=_pool, limit=limit)
        except Exception:
            def suggest_fn(trait, limit=3):
                return []

    def _obs(level, short, text, trait=None):
        o = {"level": level, "short": short, "text": text}
        if trait:
            picks = suggest_fn(trait) or []
            if picks:
                o["picks"] = picks
        return o

    a = _summarise(ally, profile_fn)
    e = _summarise(enemy, profile_fn)

    obs = []

    # ── Ally-side drafting gaps (actionable while you can still pick) ──────────
    if a["count"] >= 3:
        if a["engage"] == 0:
            obs.append(_obs("warn", "No hard engage",
                            "Your team has no reliable hard engage — consider a "
                            "pick with lockdown to start fights.", "engage"))
        if a["hard_cc"] == 0:
            if a["soft_cc"] >= 2:
                # Soft CC (slows/short roots) peels but can't lock a target down;
                # distinguish it from a genuinely CC-less comp.
                obs.append(_obs("warn", "Soft CC only, no lockdown",
                                "Your CC is all soft (slows) — enough to peel, but "
                                "no reliable lockdown for priority targets.",
                                "hard_cc"))
            else:
                obs.append(_obs("warn", "No hard CC",
                                "Your team has no hard CC — you may struggle to "
                                "lock down priority targets.", "hard_cc"))
        # All-one-damage-type comps let the enemy stack a single resist. Only
        # call it "fully" AD/AP when there are no mixed-damage picks either.
        if a["ap"] == 0 and a["mixed"] == 0 and a["ad"] >= 3:
            obs.append(_obs("warn", "All AD, add magic dmg",
                            "Your team is fully AD — enemies can stack armor. A "
                            "magic-damage pick would diversify.", "ap"))
        elif a["ad"] == 0 and a["mixed"] == 0 and a["ap"] >= 3:
            obs.append(_obs("warn", "All AP, add AD threat",
                            "Your team is fully AP — enemies can stack magic "
                            "resist. An AD pick would diversify.", "ad"))

    # ── Enemy-side reads (inform your pick / your early itemisation) ───────────
    if e["count"] >= 3:
        if e["ap"] == 0 and e["mixed"] == 0 and e["ad"] >= 3:
            obs.append(_obs("info", "Enemy AD-heavy: buy armor",
                            "Enemy comp is AD-heavy — armor will be efficient and "
                            "MR picks give up less."))
        elif e["ad"] == 0 and e["mixed"] == 0 and e["ap"] >= 3:
            obs.append(_obs("info", "Enemy AP-heavy: buy MR",
                            "Enemy comp is AP-heavy — magic resist will be "
                            "efficient this game."))

        # Wombo: heavy engage AND heavy hard CC. One consolidated warn instead of
        # two stacked info lines (the CC and engage reads below).
        if e["engage"] >= 2 and e["hard_cc"] >= 3:
            obs.append(_obs("warn", "Enemy wombo, disengage+peel",
                            "Enemy has strong engage and CC — expect all-ins; "
                            "prioritise disengage, flank vision, and peel."))
        else:
            if e["hard_cc"] >= 3:
                # Much of League's hard CC is knockup/displacement, which
                # tenacity does NOT reduce — lead with positioning/peel.
                obs.append(_obs("info", f"{e['hard_cc']} hard CC: peel/tenacity",
                                f"Enemy has {e['hard_cc']} hard-CC champions — "
                                "positioning and peel matter most; tenacity helps "
                                "vs stuns/roots (not knockups)."))
            if e["engage"] >= 2:
                obs.append(_obs("info", f"{e['engage']} engage tools, disengage",
                                f"Enemy has {e['engage']} engage tools — disengage "
                                "and vision around fights matter."))

        if e["engage"] == 0:
            obs.append(_obs("info", "Enemy no engage, group free",
                            "Enemy has no reliable hard engage — you can group, "
                            "take vision, and pick your fights; sidelaning is "
                            "lower-risk."))

    if a["count"] >= 3 and a["engage"] >= 2 and a["hard_cc"] >= 2:
        obs.append(_obs("good", "Strong engage+CC, force it",
                        "Your comp has strong engage and CC — look for "
                        "opportunities to force fights."))

    return {"ally": a, "enemy": e, "observations": obs}
