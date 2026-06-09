"""
build_data_bundle.py — Pre-compute the full u.gg data bundle for RuneSync.

Uses the stats2.u.gg JSON API directly (no browser/Playwright needed).
All data is fetched via plain HTTP GET from static CDN endpoints.

What the bundle contains:
  - patch:        current patch string (from ddragon)
  - role_weights: { champ_lower: { role: fraction } }
  - builds:       { champ_lower: { role: build_dict } }
  - counters:     { champ_lower: { role: [counter_dict, ...] } }
  - matchups:     { champ_lower: { role: { enemy_name: win_rate } } }

Run locally:
    py scripts/build_data_bundle.py --output data_bundle.json

Run a smoke test (first N champions only):
    py scripts/build_data_bundle.py --output data_bundle.json --limit 3

Designed to be invoked by .github/workflows/build_bundle.yml on a cron.
"""

import argparse
import json
import ssl
import sys
import time
import threading
import urllib.request
import urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── u.gg API constants ───────────────────────────────────────────────────────

REGION_WORLD = "12"
RANK_EMERALD_PLUS = "10"
QUEUE_RANKED = "ranked_solo_5x5"

ROLE_ID_TO_NAME = {1: "jungle", 2: "support", 3: "bot", 4: "top", 5: "mid"}
ROLE_NAME_TO_ID = {v: k for k, v in ROLE_ID_TO_NAME.items()}

ROLES = ["top", "jungle", "mid", "bot", "support"]
ROLE_WEIGHT_THRESHOLD = 0.05

_SSL_CTX = ssl.create_default_context()
_HEADERS = {"User-Agent": "RuneSync/1.0"}

# Global throttle: minimum delay between any two HTTP requests to stats2.u.gg.
_request_lock = threading.Lock()
_last_request_time = 0.0
_REQUEST_SPACING = 3.0  # seconds between requests — u.gg sliding-window limit
_global_backoff_until = 0.0  # when a 429 hits, all threads pause until this time

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def _throttle():
    global _last_request_time, _global_backoff_until
    # If another thread hit a 429 recently, wait for the global cooldown
    now = time.time()
    if now < _global_backoff_until:
        time.sleep(_global_backoff_until - now)
    with _request_lock:
        now = time.time()
        wait = _REQUEST_SPACING - (now - _last_request_time)
        if wait > 0:
            time.sleep(wait)
        _last_request_time = time.time()


def _fetch_json(url: str, retries: int = 4) -> dict | list | None:
    global _global_backoff_until
    _throttle()
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, context=_SSL_CTX, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                backoff = min(2 ** attempt * 2, 30)
                _global_backoff_until = time.time() + backoff
                print(f"[fetch] 429 rate-limited, backing off {backoff}s "
                      f"(attempt {attempt+1}/{retries+1})", flush=True)
                time.sleep(backoff)
                continue
            if attempt < retries:
                time.sleep(1)
            else:
                print(f"[fetch] FAIL {url}: HTTP {e.code}", flush=True)
                return None
        except Exception as e:
            if attempt < retries:
                time.sleep(1)
            else:
                print(f"[fetch] FAIL {url}: {e}", flush=True)
                return None


# ── ddragon ───────────────────────────────────────────────────────────────────

def fetch_ddragon_patch() -> str:
    url = "https://ddragon.leagueoflegends.com/api/versions.json"
    versions = _fetch_json(url)
    return versions[0]


def fetch_champion_map(patch: str) -> dict:
    """Return {champion_id_str: display_name} and {display_name: champion_id_str}."""
    url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json"
    data = _fetch_json(url)
    id_to_name = {}
    name_to_id = {}
    for v in data["data"].values():
        cid = str(v["key"])
        name = v["name"]
        id_to_name[cid] = name
        name_to_id[name] = cid
    return id_to_name, name_to_id


def fetch_perk_metadata(patch: str) -> dict:
    """Return {perk_id: (tree_id, row_index)} from ddragon runesReforged."""
    url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/runesReforged.json"
    data = _fetch_json(url)
    if not data:
        return {}
    perk_meta = {}
    for tree in data:
        tree_id = tree["id"]
        for row_idx, slot in enumerate(tree.get("slots", [])):
            for rune in slot.get("runes", []):
                perk_meta[rune["id"]] = (tree_id, row_idx)
    return perk_meta


# Module-level perk metadata, populated during build_bundle()
_PERK_META: dict = {}


# ── u.gg API version discovery ────────────────────────────────────────────────

def fetch_api_version(patch: str) -> str:
    url = "https://static.bigbrain.gg/assets/lol/riot_patch_update/prod/ugg/ugg-api-versions.json"
    data = _fetch_json(url)
    patch_key = patch.rsplit(".", 1)[0].replace(".", "_")
    if patch_key in data:
        return data[patch_key].get("overview", "1.5.0")
    for k in sorted(data.keys(), reverse=True):
        return data[k].get("overview", "1.5.0")
    return "1.5.0"


def _stats_url(data_type: str, patch: str, api_ver: str, champ_id: str = None) -> str:
    patch_key = patch.rsplit(".", 1)[0].replace(".", "_")
    base = f"https://stats2.u.gg/lol/1.5/{data_type}/{patch_key}"
    if champ_id:
        return f"{base}/{QUEUE_RANKED}/{champ_id}/{api_ver}.json"
    return f"{base}/{api_ver}.json"


# ── role weights from u.gg primary_roles ──────────────────────────────────────

def fetch_role_weights(patch: str, api_ver: str, id_to_name: dict) -> dict:
    url = _stats_url("primary_roles", patch, api_ver)
    data = _fetch_json(url)
    if not data:
        return {}
    weights = {}
    # Assign synthetic weights: primary=1.0, secondary=0.3, rest=0.01
    # The build step's 200-match minimum filters garbage roles regardless.
    WEIGHT_MAP = [1.0, 0.3, 0.1, 0.02, 0.01]
    for cid, role_order in data.items():
        name = id_to_name.get(str(cid))
        if not name or not isinstance(role_order, list):
            continue
        w = {}
        for i, role_id in enumerate(role_order):
            role_name = ROLE_ID_TO_NAME.get(role_id)
            if role_name and i < len(WEIGHT_MAP):
                w[role_name] = WEIGHT_MAP[i]
        weights[name] = w
    return weights


# ── perk sorting ─────────────────────────────────────────────────────────────

def _sort_perk_ids(perk_ids: list, primary_tree: int, secondary_tree: int) -> list:
    """Sort 6 rune perk IDs into LCU positional order.

    Expected order: [keystone, row1, row2, row3, secondary1, secondary2]
    where primary perks come first (sorted by row), then secondary (sorted by row).
    """
    if not _PERK_META or len(perk_ids) < 6:
        return perk_ids  # no metadata available, return as-is

    primary_perks = []
    secondary_perks = []
    for pid in perk_ids:
        tree_id, row = _PERK_META.get(pid, (0, 99))
        if tree_id == primary_tree:
            primary_perks.append((row, pid))
        elif tree_id == secondary_tree:
            secondary_perks.append((row, pid))
        else:
            # Unknown tree — append to whichever has fewer to maintain count
            if len(primary_perks) < 4:
                primary_perks.append((row, pid))
            else:
                secondary_perks.append((row, pid))

    primary_perks.sort()
    secondary_perks.sort()
    return [pid for _, pid in primary_perks] + [pid for _, pid in secondary_perks]


# ── build extraction from overview endpoint ───────────────────────────────────

def fetch_overview_data(patch: str, api_ver: str, champ_id: str) -> dict | None:
    url = _stats_url("overview", patch, api_ver, champ_id)
    return _fetch_json(url)


def extract_build(data: dict, champ_name: str, role: str) -> dict | None:
    if not data:
        return None
    role_id = str(ROLE_NAME_TO_ID.get(role, 4))
    try:
        role_data = data[REGION_WORLD][RANK_EMERALD_PLUS][role_id]
        build_arr = role_data[0]
    except (KeyError, IndexError, TypeError):
        return None

    # [0] = runes: [matches, wins, primary_tree, secondary_tree, [perk_ids]]
    runes = build_arr[0] if len(build_arr) > 0 else []
    # [1] = summoner spells: [matches, wins, [spell_ids]]
    spells = build_arr[1] if len(build_arr) > 1 else []

    # Skip roles with too few games (garbage off-meta like Aatrox support)
    total_matches = spells[0] if isinstance(spells, list) and len(spells) > 0 else 0
    if isinstance(total_matches, (int, float)) and total_matches < 200:
        return None
    # [2] = starter items: [matches, wins, [item_ids]]
    starter = build_arr[2] if len(build_arr) > 2 else []
    # [3] = core items: [matches, wins, [item_ids]]
    core = build_arr[3] if len(build_arr) > 3 else []
    # [8] = shards: [matches, wins, [shard_id_strings]]
    shards = build_arr[8] if len(build_arr) > 8 else []

    if not isinstance(runes, list) or len(runes) < 5:
        return None

    perk_ids = runes[4] if len(runes) > 4 and isinstance(runes[4], list) else []
    shard_ids = []
    if isinstance(shards, list) and len(shards) > 2 and isinstance(shards[2], list):
        shard_ids = [int(s) for s in shards[2] if str(s).isdigit()]

    primary_tree = runes[2] if len(runes) > 2 else 8000
    secondary_tree = runes[3] if len(runes) > 3 else 8100

    # Sort perk IDs into correct LCU order: [keystone, row1, row2, row3, sec1, sec2].
    # The u.gg API returns them in arbitrary (numeric) order, but the LCU
    # interprets selectedPerkIds positionally.
    sorted_perks = _sort_perk_ids(perk_ids[:6], primary_tree, secondary_tree)
    selected_perk_ids = sorted_perks + (shard_ids[:3] if shard_ids else [5008, 5008, 5001])

    if len(selected_perk_ids) < 9:
        print(f"[bundle] build {champ_name}/{role}: only {len(selected_perk_ids)} perks, skipping", flush=True)
        return None

    spell_ids = spells[2] if isinstance(spells, list) and len(spells) > 2 else []
    starter_ids = starter[2] if isinstance(starter, list) and len(starter) > 2 else []
    core_ids = core[2] if isinstance(core, list) and len(core) > 2 else []

    # Items 4th/5th/6th from build_arr[5] if present (nested structure)
    fourth_ids, fifth_ids, sixth_ids = [], [], []
    if len(build_arr) > 5 and isinstance(build_arr[5], list):
        item_options = build_arr[5]
        if isinstance(item_options, list) and len(item_options) >= 3:
            for bucket in item_options:
                if isinstance(bucket, list) and len(bucket) >= 1:
                    item_id = bucket[0] if isinstance(bucket[0], int) else None
                    if item_id:
                        if not fourth_ids or len(fourth_ids) < 3:
                            fourth_ids.append(item_id)

    return {
        "champion": champ_name,
        "role": role,
        "primary_style_id": runes[2] if len(runes) > 2 else 8000,
        "sub_style_id": runes[3] if len(runes) > 3 else 8100,
        "selected_perk_ids": selected_perk_ids,
        "summoners": spell_ids if isinstance(spell_ids, list) else [],
        "items_start": [str(i) for i in starter_ids] if isinstance(starter_ids, list) else [],
        "items_core": [str(i) for i in core_ids] if isinstance(core_ids, list) else [],
        "items_start_ids": starter_ids if isinstance(starter_ids, list) else [],
        "items_core_ids": core_ids if isinstance(core_ids, list) else [],
        "items_fourth_ids": fourth_ids,
        "items_fifth_ids": fifth_ids,
        "items_sixth_ids": sixth_ids,
        "skill_order": [],
    }


# ── matchup + counter extraction ──────────────────────────────────────────────

def fetch_matchups_and_counters(
    patch: str, api_ver: str, champ_id: str, champ_name: str,
    roles: list, id_to_name: dict, role_weights: dict = None
) -> tuple[dict, dict]:
    """Fetch matchup data for all roles at once (one HTTP call per champion).

    Returns (matchups_by_role, counters_by_role).
    """
    url = _stats_url("matchups", patch, api_ver, champ_id)
    data = _fetch_json(url)
    if not data:
        return {}, {}

    matchups_by_role = {}
    counters_by_role = {}

    for role in roles:
        role_id = str(ROLE_NAME_TO_ID.get(role, 4))
        try:
            role_data = data[REGION_WORLD][RANK_EMERALD_PLUS][role_id]
            entries = role_data[0]
        except (KeyError, IndexError, TypeError):
            continue

        if not isinstance(entries, list):
            continue

        matchup_table = {}
        counter_list = []
        for m in entries:
            if not isinstance(m, list) or len(m) < 3:
                continue
            enemy_id = str(m[0])
            wins = m[1]
            matches = m[2]
            if matches < 50:
                continue
            enemy_name = id_to_name.get(enemy_id)
            if not enemy_name:
                continue
            my_wr = round(wins / matches * 100, 2) if matches > 0 else 50.0
            if 30.0 <= my_wr <= 70.0:
                matchup_table[enemy_name] = my_wr
            enemy_wr = round(100 - my_wr, 2)
            # Only include as counter if enemy is actually played in this role
            enemy_plays_role = True
            if role_weights:
                ew = role_weights.get(enemy_name, {})
                enemy_plays_role = ew.get(role, 0) >= 1.0
            if 40.0 <= enemy_wr <= 70.0 and enemy_plays_role:
                counter_list.append({"champion": enemy_name, "win_rate": enemy_wr})

        if matchup_table:
            matchups_by_role[role] = matchup_table
        if counter_list:
            counter_list.sort(key=lambda x: x["win_rate"], reverse=True)
            counters_by_role[role] = counter_list[:5]

    return matchups_by_role, counters_by_role


# ── bundle orchestrator ───────────────────────────────────────────────────────

def relevant_roles(champ: str, role_weights: dict, threshold: float) -> list[str]:
    w = role_weights.get(champ) or role_weights.get(champ.lower()) or {}
    out = []
    for role in ROLES:
        v = w.get(role, 0)
        if isinstance(v, (int, float)) and v >= threshold:
            out.append(role)
    if not out:
        out = ["mid"]
    return out


def build_bundle(limit: int | None, threshold: float, output_path: Path) -> dict:
    global _PERK_META
    started_at = time.time()
    print(f"[bundle] starting at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    patch = fetch_ddragon_patch()
    print(f"[bundle] patch: {patch}", flush=True)

    api_ver = fetch_api_version(patch)
    print(f"[bundle] API version: {api_ver}", flush=True)

    _PERK_META = fetch_perk_metadata(patch)
    print(f"[bundle] perk metadata: {len(_PERK_META)} perks loaded", flush=True)

    id_to_name, name_to_id = fetch_champion_map(patch)
    champions = sorted(id_to_name.values())
    if limit:
        champions = champions[:limit]
        print(f"[bundle] LIMITED to first {limit} champions: {champions}", flush=True)
    print(f"[bundle] {len(champions)} champions to process", flush=True)

    role_weights = fetch_role_weights(patch, api_ver, id_to_name)
    print(f"[bundle] role_weights: {len(role_weights)} champions", flush=True)

    builds = {}
    counters = {}
    matchups = {}
    failures = []

    # u.gg overview endpoint returns ALL roles in one response per champion,
    # but builds are role-specific in the nested structure. Matchups are also
    # per-champion (one HTTP call covers all roles). So we parallelize by
    # champion, not by (champion, role).
    done_count = [0]

    def _process_champ(champ: str) -> tuple:
        champ_id = name_to_id.get(champ)
        if not champ_id:
            return champ, {}, {}, {}, []

        roles_for_champ = relevant_roles(champ, role_weights, threshold)
        ckey = champ.lower()
        local_builds = {}
        local_failures = []

        done_count[0] += 1
        print(f"[bundle] [{done_count[0]}/{len(champions)}] "
              f"{champ} -> {roles_for_champ}", flush=True)

        # Builds — fetch the overview endpoint ONCE per champion, then
        # extract each role from the cached response. This halves the
        # total HTTP calls vs the old per-role fetch.
        try:
            overview_data = fetch_overview_data(patch, api_ver, champ_id)
        except Exception as e:
            overview_data = None
            local_failures.append(f"build:{champ}:overview_fetch:{e}")
            print(f"[bundle] FAIL overview {champ}: {e}", flush=True)
        for role in roles_for_champ:
            try:
                b = extract_build(overview_data, champ, role)
                if b:
                    local_builds[role] = b
            except Exception as e:
                local_failures.append(f"build:{champ}:{role}:{e}")
                print(f"[bundle] FAIL build {champ}/{role}: {e}", flush=True)
        if not local_builds:
            local_failures.append(f"build:{champ}:all_roles_empty")

        # Matchups + counters — one HTTP call per champion
        try:
            local_matchups, local_counters = fetch_matchups_and_counters(
                patch, api_ver, champ_id, champ, roles_for_champ, id_to_name,
                role_weights)
        except Exception as e:
            local_matchups, local_counters = {}, {}
            local_failures.append(f"matchups:{champ}:{e}")
            print(f"[bundle] FAIL matchups {champ}: {e}", flush=True)

        return champ, local_builds, local_counters, local_matchups, local_failures

    # Sequential with throttle — stats2.u.gg has an aggressive sliding-window
    # rate limit. 1 worker + 3.0s spacing, 2 requests per champion (overview
    # + matchups). ~18 min for 172 champions.
    empty_champs = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(_process_champ, c): c for c in champions}
        for future in as_completed(futures):
            champ, b, c, m, f = future.result()
            ckey = champ.lower()
            builds[ckey] = b
            counters[ckey] = c
            matchups[ckey] = m
            failures.extend(f)
            if not b:
                empty_champs.append(champ)

    if empty_champs:
        print(f"[bundle] WARNING: {len(empty_champs)} champions got no build data "
              f"(likely rate-limited): {empty_champs[:10]}{'...' if len(empty_champs) > 10 else ''}",
              flush=True)

    bundle = {
        "schema_version": 2,
        "generated_at": int(time.time()),
        "patch": patch,
        "champion_count": len(champions),
        "role_weights": role_weights,
        "builds": builds,
        "counters": counters,
        "matchups": matchups,
        "failures": failures,
    }

    output_path.write_text(json.dumps(bundle, separators=(",", ":")), encoding="utf-8")
    elapsed = int(time.time() - started_at)
    print(
        f"[bundle] done in {elapsed}s — {len(builds)} champs, "
        f"{sum(len(r) for r in builds.values())} builds, "
        f"{sum(len(r) for r in counters.values())} counter sets, "
        f"{sum(len(r) for r in matchups.values())} matchup tables, "
        f"{len(failures)} failures. "
        f"Wrote {output_path} ({output_path.stat().st_size // 1024} KB)",
        flush=True,
    )
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data_bundle.json"),
                        help="Output JSON path (default: data_bundle.json)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Smoke-test: process only the first N champions")
    parser.add_argument("--threshold", type=float, default=ROLE_WEIGHT_THRESHOLD,
                        help=f"Min role-weight fraction (default: {ROLE_WEIGHT_THRESHOLD})")
    args = parser.parse_args()
    build_bundle(args.limit, args.threshold, args.output)


if __name__ == "__main__":
    main()
