"""live_hud — derive a compact in-game HUD snapshot from Live Client Data.

Pure, dependency-free functions that turn a raw `/liveclientdata/allgamedata`
payload (see live_client.py) into a small, UI-ready dict:

  - your live CS and CS/min, level, and current gold,
  - the same for your lane opponent (enemy on your position), and the deltas,
  - upcoming neutral-objective timers derived from the events log + game clock.

Everything here is read-only and local (the Live Client Data API on :2999 needs
no Riot API key). Nothing about another player is presented as a rating or a
judgement — the opponent's CS/level are shown only as a neutral lane benchmark
for the local player's own improvement.
"""
from __future__ import annotations

from typing import Optional

# App role token  ->  Live Client Data `position` value.
_ROLE_TO_POSITION = {
    "top": "TOP",
    "jungle": "JUNGLE",
    "jgl": "JUNGLE",
    "mid": "MIDDLE",
    "middle": "MIDDLE",
    "bot": "BOTTOM",
    "adc": "BOTTOM",
    "bottom": "BOTTOM",
    "support": "UTILITY",
    "sup": "UTILITY",
    "utility": "UTILITY",
}

# Live Client Data `position` value  ->  app role token (for skill-order lookup).
_POSITION_TO_ROLE = {
    "TOP": "top", "JUNGLE": "jungle", "MIDDLE": "mid",
    "BOTTOM": "bot", "UTILITY": "support",
}

# Ability slots in level-up order of interest.
_ABILITIES = ("Q", "W", "E", "R")

# Neutral-objective spawn/respawn model. Times are in SECONDS of game clock and
# reflect the live balance state current as of patch 16.x — update the numbers
# here (only here) when Riot changes objective timings.
#   initial : first spawn time from game start
#   respawn : seconds after a kill until it is up again (None = does not respawn
#             on the same simple cadence, so we only show the initial spawn)
#   event   : the Live Client Data EventName that marks a kill/clear
_OBJECTIVES = {
    "Dragon": {"initial": 5 * 60, "respawn": 5 * 60, "event": "DragonKill"},
    "Voidgrubs": {"initial": 5 * 60, "respawn": None, "event": "HordeKill"},
    "Rift Herald": {"initial": 14 * 60, "respawn": None, "event": "HeraldKill"},
    "Baron": {"initial": 25 * 60, "respawn": 6 * 60, "event": "BaronKill"},
}


def _active_identity(all_game_data: dict) -> tuple[str, str]:
    """Return (riotId, summonerName) for the local player, lowercased."""
    ap = all_game_data.get("activePlayer") or {}
    riot_id = str(ap.get("riotId") or "").strip().lower()
    summoner = str(ap.get("summonerName") or "").strip().lower()
    return riot_id, summoner


def _player_identity(player: dict) -> tuple[str, str]:
    riot_id = str(player.get("riotId") or "").strip().lower()
    if not riot_id:
        name = str(player.get("riotIdGameName") or "").strip()
        tag = str(player.get("riotIdTagLine") or "").strip()
        if name and tag:
            riot_id = f"{name}#{tag}".lower()
    summoner = str(player.get("summonerName") or "").strip().lower()
    return riot_id, summoner


def _find_active_player(all_game_data: dict) -> Optional[dict]:
    riot_id, summoner = _active_identity(all_game_data)
    players = all_game_data.get("allPlayers") or []
    # Exact Riot ID (name#tag) is globally unique — always prefer it, scanning
    # every player before falling back so a shared game name never mismatches.
    if riot_id:
        for player in players:
            if not isinstance(player, dict):
                continue
            p_riot, _ = _player_identity(player)
            if p_riot and p_riot == riot_id:
                return player
    # Fallback: summoner name (legacy payloads without a Riot ID).
    if summoner:
        for player in players:
            if not isinstance(player, dict):
                continue
            _, p_summoner = _player_identity(player)
            if p_summoner and p_summoner == summoner:
                return player
    return None


def _cs(player: dict) -> int:
    scores = player.get("scores") or {}
    try:
        return int(scores.get("creepScore") or 0)
    except (TypeError, ValueError):
        return 0


def _item_ids(player: dict) -> list:
    """Item IDs a player is currently carrying (from Live Client Data `items`)."""
    ids = []
    for entry in player.get("items") or []:
        if isinstance(entry, dict) and entry.get("itemID") is not None:
            ids.append(entry.get("itemID"))
    return ids


def _default_gold_fn(item_ids) -> int:
    # Lazy import keeps this module import-cycle-free and unit-testable with an
    # injected estimator (no network / catalog needed in tests).
    import item_data
    return item_data.estimate_gold_from_items(item_ids)


def _cs_per_min(cs: int, game_time: float) -> float:
    if game_time <= 0:
        return 0.0
    return round(cs / (game_time / 60.0), 1)


def _lane_opponent(all_game_data: dict, me: dict,
                   fallback_role: Optional[str]) -> Optional[dict]:
    """The enemy-team player sharing the local player's lane position."""
    my_team = str(me.get("team") or "").upper()
    my_pos = str(me.get("position") or "").upper()
    if not my_pos and fallback_role:
        my_pos = _ROLE_TO_POSITION.get(str(fallback_role).strip().lower(), "")
    if not my_pos:
        return None
    for player in all_game_data.get("allPlayers") or []:
        if not isinstance(player, dict):
            continue
        if str(player.get("team") or "").upper() == my_team:
            continue
        if str(player.get("position") or "").upper() == my_pos:
            return player
    return None


def _objective_timers(all_game_data: dict, game_time: float) -> list[dict]:
    """Next-up countdown for each tracked neutral objective.

    For a respawning objective we key off the most recent kill event; before the
    first spawn (or for one-time objectives not yet taken) we use the initial
    spawn time. `next_seconds` is None once an objective has been taken and does
    not respawn on a simple cadence.
    """
    events = ((all_game_data.get("events") or {}).get("Events")) or []
    last_kill: dict[str, float] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        name = str(event.get("EventName") or "")
        try:
            when = float(event.get("EventTime") or 0.0)
        except (TypeError, ValueError):
            continue
        for obj, spec in _OBJECTIVES.items():
            if name == spec["event"]:
                last_kill[obj] = max(when, last_kill.get(obj, 0.0))

    timers = []
    for obj, spec in _OBJECTIVES.items():
        killed_at = last_kill.get(obj)
        if killed_at is None:
            # Not taken yet: count down to the initial spawn.
            up_at = spec["initial"]
            state = "alive" if game_time >= up_at else "pending"
        elif spec["respawn"] is not None:
            up_at = killed_at + spec["respawn"]
            state = "alive" if game_time >= up_at else "respawning"
        else:
            # One-time objective already taken: nothing more to count down to.
            timers.append({"name": obj, "state": "gone", "next_seconds": None})
            continue
        next_seconds = None if game_time >= up_at else int(round(up_at - game_time))
        timers.append({"name": obj, "state": state, "next_seconds": next_seconds})
    return timers


def _team_gold(all_game_data: dict, me: dict, gold_fn) -> Optional[dict]:
    """Invested-gold totals for the local player's team vs the enemy team.

    Summed item value per side (the visible-inventory proxy), giving a macro
    gold lead/deficit without needing any player's unspent gold.
    """
    my_team = str(me.get("team") or "").upper()
    if not my_team:
        return None
    ours = 0
    theirs = 0
    for player in all_game_data.get("allPlayers") or []:
        if not isinstance(player, dict):
            continue
        value = int(gold_fn(_item_ids(player)) or 0)
        if str(player.get("team") or "").upper() == my_team:
            ours += value
        else:
            theirs += value
    return {"ours": ours, "theirs": theirs, "diff": ours - theirs}


def _position_to_role(position: str) -> str:
    return _POSITION_TO_ROLE.get(str(position or "").upper(), "")


def _ability_ranks(all_game_data: dict) -> dict:
    """Current Q/W/E/R ranks for the local player from Live Client Data.

    `activePlayer.abilities` is present only for the local player (no other
    player's ability levels are exposed), which keeps this strictly about the
    user's own skill leveling.
    """
    ap = all_game_data.get("activePlayer") or {}
    abilities = ap.get("abilities") or {}
    ranks = {}
    for slot in _ABILITIES:
        entry = abilities.get(slot) or {}
        try:
            ranks[slot] = int(entry.get("abilityLevel") or 0)
        except (TypeError, ValueError):
            ranks[slot] = 0
    return ranks


def _ult_cap(level: int) -> int:
    """Max ultimate rank legal at a given champion level (6/11/16 unlocks)."""
    if level >= 16:
        return 3
    if level >= 11:
        return 2
    if level >= 6:
        return 1
    return 0


def next_skill(ranks: dict, level: int,
               skill_seq=None, skill_max=None) -> Optional[str]:
    """Recommend the next ability to level ("Q"/"W"/"E"/"R"), or None if maxed.

    The popular *exact* level order (`skill_seq`, e.g. ["W","Q","E","Q",...]) is
    authoritative and is walked point-by-point: the recommendation is the
    earliest scheduled point the player has not yet taken. When the player is
    exactly on script this is simply the next entry; when they deviated (say
    they skipped an early W) it catches that missed ability up rather than
    blindly following the index.

    Crucially the sequence also encodes each champion's *real* ability caps and
    unlock timing — Q/W/E appear five times, R three (or six for Udyr), and R
    can appear at level 1 for champions that allow it. So the walk never needs
    the generic 5/6/11/16 rules and works for nonstandard kits. Those generic
    caps are only used as a last-resort fallback for a champion with no scraped
    order at all (`skill_seq` empty), where `skill_max` priority is the best
    available signal. `level` likewise only matters to that fallback; the
    sequence walk is purely points-based, which also handles banked points.
    """
    ranks = {k: int((ranks or {}).get(k) or 0) for k in _ABILITIES}
    seq = [s for s in (skill_seq or []) if s in _ABILITIES]

    if seq:
        # Walk the popular order; the first scheduled point the player is behind
        # on is the pick. On script that's the next entry; off script it catches
        # up the earliest missed ability. Per-ability counts in `seq` are the
        # champion's real caps, so nothing illegal can be returned.
        seen = {k: 0 for k in _ABILITIES}
        for ab in seq:
            seen[ab] += 1
            if seen[ab] > ranks[ab]:
                return ab
        # Player has taken every point the scraped order schedules (typically
        # levels 16-18, beyond the popular 15-point sequence): fall through to
        # the generic-cap fallback for the remaining points.

    level = int(level or 0)
    if ranks["Q"] >= 5 and ranks["W"] >= 5 and ranks["E"] >= 5 and ranks["R"] >= 3:
        return None

    spent = sum(ranks.values())
    # The next point (the spent+1-th) is gained at champion level spent+1; with
    # banked points the player may already be higher, so allow the current level.
    target = max(level, spent + 1)
    ult_cap = _ult_cap(target)
    qwe_cap = min(5, (target + 1) // 2)

    def can(ab: str) -> bool:
        if ab == "R":
            return ranks["R"] < ult_cap
        return ab in ("Q", "W", "E") and ranks[ab] < 5 and ranks[ab] < qwe_cap

    # Rank the ultimate whenever it's available, else the max-priority basic
    # that's still legal to level.
    if can("R"):
        return "R"
    order = [s for s in (skill_max or []) if s in ("Q", "W", "E")]
    for s in ("Q", "W", "E"):
        if s not in order:
            order.append(s)
    for ab in order:
        if can(ab):
            return ab
    # Every basic is at this level's cap: take the highest priority not-yet-maxed.
    for ab in order:
        if ranks[ab] < 5:
            return ab
    if ranks["R"] < 3:
        return "R"
    return None


def build_hud(all_game_data: dict,
              fallback_role: Optional[str] = None,
              gold_fn=None,
              skill_lookup=None) -> Optional[dict]:
    """Build a UI-ready HUD snapshot, or None if the payload is unusable.

    `fallback_role` is the local player's champ-select role (app token like
    "mid"/"bot"); used only when the Live Client Data `position` is blank.
    `gold_fn(item_ids)->int` estimates a player's invested gold from their held
    items (defaults to the item_data catalog); injectable for testing.
    `skill_lookup(champion, role)->{"order":[...],"max":[...]}|None` supplies the
    champion's popular skill order (from the U.GG-sourced bundle) so the HUD can
    show which ability to level next; omitted (no skill block) when unavailable.
    """
    if not isinstance(all_game_data, dict):
        return None
    if gold_fn is None:
        gold_fn = _default_gold_fn
    game_data = all_game_data.get("gameData") or {}
    try:
        game_time = float(game_data.get("gameTime") or 0.0)
    except (TypeError, ValueError):
        game_time = 0.0

    me = _find_active_player(all_game_data)
    if not me:
        return None

    ap = all_game_data.get("activePlayer") or {}
    my_cs = _cs(me)
    my_level = int(me.get("level") or 0)
    my_item_gold = int(gold_fn(_item_ids(me)) or 0)
    try:
        my_bank = int(round(float(ap.get("currentGold") or 0)))
    except (TypeError, ValueError):
        my_bank = 0

    hud = {
        "game_time": int(round(game_time)),
        "me": {
            "champion": me.get("championName") or "",
            "position": str(me.get("position") or "").upper(),
            "cs": my_cs,
            "cs_per_min": _cs_per_min(my_cs, game_time),
            "level": my_level,
            "gold": my_bank,           # real unspent gold (local player only)
            "est_gold": my_item_gold,  # invested item value (comparable metric)
        },
        "opponent": None,
        "delta": None,
        "objectives": _objective_timers(all_game_data, game_time),
        "team_gold": _team_gold(all_game_data, me, gold_fn),
    }

    opp = _lane_opponent(all_game_data, me, fallback_role)
    if opp:
        opp_cs = _cs(opp)
        opp_level = int(opp.get("level") or 0)
        opp_item_gold = int(gold_fn(_item_ids(opp)) or 0)
        hud["opponent"] = {
            "champion": opp.get("championName") or "",
            "position": str(opp.get("position") or "").upper(),
            "cs": opp_cs,
            "cs_per_min": _cs_per_min(opp_cs, game_time),
            "level": opp_level,
            "est_gold": opp_item_gold,
        }
        hud["delta"] = {
            "cs": my_cs - opp_cs,
            "level": my_level - opp_level,
            "gold": my_item_gold - opp_item_gold,
        }

    if skill_lookup is not None:
        try:
            role_token = (_position_to_role(hud["me"]["position"])
                          or (str(fallback_role).strip().lower()
                              if fallback_role else ""))
            info = skill_lookup(hud["me"]["champion"], role_token) or {}
        except Exception:
            info = {}
        seq = info.get("order") or []
        smax = info.get("max") or []
        if seq or smax:
            ranks = _ability_ranks(all_game_data)
            nxt = next_skill(ranks, my_level, seq, smax)
            hud["skill"] = {
                "next": nxt,
                "ranks": ranks,
                "max_order": smax,
                "maxed": nxt is None,
            }
    return hud
