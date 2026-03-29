"""
u.gg build data client — calls the RuneSync scraping server.

The server (RuneSyncServer/) runs headless Chromium and handles all scraping.
This module is a thin HTTP wrapper that keeps the same public interface so
monitor.py needs no changes.
"""

import json, sys, time, urllib.request, urllib.error, urllib.parse
from typing import Optional

# ── server config ──────────────────────────────────────────────────────────
# Update SERVER_URL after deploying. For local testing use localhost.
SERVER_URL = "http://localhost:8000"

ROLE_MAP = {
    "jungle": "jungle", "support": "support",
    "bot": "adc", "adc": "adc", "top": "top", "mid": "mid",
}

# ── in-memory winrate cache ─────────────────────────────────────────────────
# Keyed by (my_champ_lower, enemy_lower, role_lower).
# Each entry stores the patch version alongside the result so the cache is
# automatically invalidated when a new patch ships.
_WINRATE_CACHE: dict = {}   # key -> {"patch": str, "result": dict}
_patch_value: str = ""
_patch_fetched_at: float = 0.0
_PATCH_TTL: float = 6 * 3600  # re-check at most every 6 hours (matches server TTL)


def _current_patch() -> str:
    """Return the current patch, re-fetching from server at most every 6 hours."""
    global _patch_value, _patch_fetched_at
    now = time.time()
    if _patch_value and now - _patch_fetched_at < _PATCH_TTL:
        return _patch_value
    result = _get("/patch", {}, timeout=10)
    new_patch = result.get("patch", "") if result else ""
    if new_patch and new_patch != _patch_value:
        if _patch_value:
            print(f"[ugg] patch {_patch_value} → {new_patch}: clearing winrate cache", file=sys.stderr)
            _WINRATE_CACHE.clear()
        _patch_value = new_patch
        _patch_fetched_at = now
    return _patch_value


def _get(path: str, params: dict, timeout: int = 35) -> Optional[dict | list]:
    """Make a GET request to the server and return parsed JSON, or None on error."""
    query = urllib.parse.urlencode(params)
    url = f"{SERVER_URL}{path}?{query}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RuneSync/1.0"})
        resp = urllib.request.urlopen(req, timeout=timeout)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        print(f"[ugg] server error {e.code} for {path}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ugg] request failed for {path}: {e}", file=sys.stderr)
        return None


class UGGClient:
    def __init__(self):
        pass  # no local state needed

    def get_top_build(self, champion_name: str, role: str = "auto",
                      rank: str = "Platinum+", region: str = "World") -> Optional[dict]:
        result = _get("/build", {
            "champion": champion_name,
            "role": role,
            "rank": rank,
            "region": region,
        }, timeout=45)
        if result is None:
            raise RuntimeError(f"Failed to fetch build for {champion_name} from server.")
        return result

    def get_counters(self, enemy_champ: str, role: str = "auto",
                     top_n: int = 5) -> list[dict]:
        result = _get("/counters", {
            "champion": enemy_champ,
            "role": role,
            "top_n": top_n,
        }, timeout=45)
        if result is None:
            return []
        return result if isinstance(result, list) else []

    def get_matchup_winrate(self, my_champ: str, enemy_champ: str,
                            role: str = "auto") -> Optional[dict]:
        patch = _current_patch()
        key = (my_champ.lower(), enemy_champ.lower(), role.lower())
        entry = _WINRATE_CACHE.get(key)
        if entry and entry.get("patch") == patch:
            return entry["result"]
        result = _get("/matchup", {
            "my_champ": my_champ,
            "enemy_champ": enemy_champ,
            "role": role,
        }, timeout=45)
        if result is not None:
            _WINRATE_CACHE[key] = {"patch": patch, "result": result}
        return result  # None on miss is fine — monitor.py already handles it

    def get_current_patch(self) -> str:
        result = _get("/patch", {}, timeout=10)
        if result and isinstance(result, dict):
            return result.get("patch", "latest")
        return "latest"

    def get_champion_id(self, champion_name: str) -> Optional[int]:
        return None  # not needed
