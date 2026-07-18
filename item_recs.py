"""
item_recs.py — dynamic in-game defensive item recommender.

Reads the local Live Client Data snapshot (same payload live_hud uses) and, from
the local player's perspective, summarises the *enemy team's* damage profile and
threat concentration, then suggests concrete resist/tenacity buys. This is about
the user's OWN itemisation decision versus the enemy composition, never a rating
or judgment of any player.

Threat weighting: each enemy contributes to an AD and/or AP threat pool weighted
by their estimated invested gold (item value) so a fed carry counts more than a
0/8 one, matching how players actually decide what to build. MIXED champions add
to both pools. Pure logic; champion_profile / item_data accessors are injectable
so tests need no network.
"""
from typing import Optional

# Below this share the enemy team is considered lopsided toward one damage type,
# so a single resist is the clear priority. Between the two thresholds it's
# genuinely mixed and we suggest both.
_LOPSIDED = 0.68


def _active_team(all_game_data: dict):
    # Reuse live_hud's robust riotId/summonerName matching so the item recommender
    # and the HUD always agree on who the local player is.
    import live_hud
    me = live_hud._find_active_player(all_game_data)
    players = all_game_data.get("allPlayers") or []
    return me, players


def _item_ids(player: dict) -> list:
    return [e.get("itemID") for e in (player.get("items") or [])
            if isinstance(e, dict) and e.get("itemID") is not None]


def build_item_recs(all_game_data: dict, *, profile_fn=None, gold_fn=None,
                    items_fn=None) -> Optional[dict]:
    """Return a UI-ready defensive recommendation dict, or None if unusable.

    profile_fn(name)->{damage_type,cc,...}; gold_fn(item_ids)->int;
    items_fn(kind, min_gold)->[{id,name,image,gold,value}]. All default to the
    live champion_profile / item_data modules.
    """
    if not isinstance(all_game_data, dict):
        return None
    if profile_fn is None:
        import champion_profile
        profile_fn = champion_profile.profile
    if gold_fn is None:
        import item_data
        gold_fn = item_data.estimate_gold_from_items
    if items_fn is None:
        import item_data
        items_fn = item_data.defensive_items

    me, players = _active_team(all_game_data)
    if not me:
        return None
    my_team = str(me.get("team") or "").upper()
    if not my_team:
        return None

    game_data = all_game_data.get("gameData") or {}
    try:
        game_time = int(round(float(game_data.get("gameTime") or 0.0)))
    except (TypeError, ValueError):
        game_time = 0

    ad_weight = 0.0
    ap_weight = 0.0
    hard_cc = 0
    biggest = None
    enemies = 0
    known_enemies = 0
    for p in players:
        if not isinstance(p, dict):
            continue
        if str(p.get("team") or "").upper() == my_team:
            continue
        enemies += 1
        prof = profile_fn(p.get("championName") or "")
        # Skip champions we have no curated data for (e.g. a brand-new champ):
        # counting them as AD by default would skew the resist advice.
        if not prof.get("known", True):
            continue
        known_enemies += 1
        # Baseline weight so an itemless early enemy still counts; invested gold
        # scales the fed carry up.
        gold = int(gold_fn(_item_ids(p)) or 0)
        weight = 1000.0 + gold
        dt = prof.get("damage_type") or "AD"
        if dt == "AD":
            ad_weight += weight
        elif dt == "AP":
            ap_weight += weight
        else:  # MIXED
            ad_weight += weight * 0.5
            ap_weight += weight * 0.5
        if prof.get("cc") in ("hard-single", "hard-aoe"):
            hard_cc += 1
        if biggest is None or gold > biggest["est_gold"]:
            biggest = {"champion": p.get("championName") or "",
                       "damage_type": dt, "est_gold": gold}

    if not known_enemies:
        return None

    total = ad_weight + ap_weight
    ad_pct = (ad_weight / total) if total else 0.5
    ap_pct = (ap_weight / total) if total else 0.5

    if ad_pct >= _LOPSIDED:
        primary = "armor"
    elif ap_pct >= _LOPSIDED:
        primary = "mr"
    else:
        primary = "mixed"

    suggestions = []
    if primary in ("armor", "mixed"):
        items = items_fn("armor", 800)[:3]
        if items:
            suggestions.append({
                "kind": "armor",
                "reason": f"{round(ad_pct * 100)}% of enemy threat is physical (AD)",
                "items": items,
            })
    if primary in ("mr", "mixed"):
        items = items_fn("mr", 800)[:3]
        if items:
            suggestions.append({
                "kind": "mr",
                "reason": f"{round(ap_pct * 100)}% of enemy threat is magic (AP)",
                "items": items,
            })

    notes = []
    if hard_cc >= 3:
        notes.append(f"Enemy has {hard_cc} hard-CC threats — consider tenacity "
                     f"(Mercury's Treads) or a cleanse/QSS option.")
    if biggest and biggest["champion"] and biggest["est_gold"] >= 4000:
        dt_label = {"AD": "physical", "AP": "magic", "MIXED": "mixed"}.get(
            biggest["damage_type"], "")
        notes.append(f"{biggest['champion']} is the enemy's biggest item threat "
                     f"({dt_label}) — prioritise the matching resist.")

    return {
        "game_time": game_time,
        "primary": primary,
        "threats": {
            "ad_pct": round(ad_pct, 3),
            "ap_pct": round(ap_pct, 3),
        },
        "hard_cc": hard_cc,
        "biggest_threat": biggest,
        "suggestions": suggestions,
        "notes": notes,
    }
