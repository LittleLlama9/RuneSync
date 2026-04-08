"""
role_updater.py — refresh champion role weight data from the RuneSync server.

The server handles all LoLalytics scraping. This module fetches the result
and writes it to role_weights_cache.json exactly as before, so champion_roles.py
needs no changes.

Public API (unchanged):
  refresh_if_stale(background=True)
  refresh_roles_now() -> bool
  get_cached_weights() -> dict | None
  cache_is_stale() -> bool
"""

import json, os, sys, time, threading, urllib.request, urllib.error, urllib.parse
from pathlib import Path
from typing import Optional

# ── config ─────────────────────────────────────────────────────────────────
from ugg_api import SERVER_URL  # reuse the same server URL

# When compiled by PyInstaller (--onefile), __file__ resolves to the temp
# extraction dir, not the exe's directory. Use sys.executable's dir instead.
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

CACHE_PATH = Path(_BASE) / "role_weights_cache.json"


# ── patch helpers ──────────────────────────────────────────────────────────

def get_latest_patch() -> "str | None":
    try:
        url = "https://ddragon.leagueoflegends.com/api/versions.json"
        with urllib.request.urlopen(url, timeout=5) as r:
            versions = json.loads(r.read())
        return versions[0] if versions else None
    except Exception:
        return None


# ── cache helpers ──────────────────────────────────────────────────────────

def load_cache() -> Optional[dict]:
    if not CACHE_PATH.exists():
        return None
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cache(weights: dict, patch: str = "") -> None:
    data = {
        "updated_at": time.time(),
        "patch": patch or (get_latest_patch() or ""),
        "weights": weights,
    }
    CACHE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  Role weight cache saved to {CACHE_PATH}", flush=True)


def cache_is_stale() -> bool:
    data = load_cache()
    if data is None:
        return True
    cached_patch = data.get("patch", "")
    if cached_patch:
        current = get_latest_patch()
        if current:
            return current != cached_patch
    age_days = (time.time() - data.get("updated_at", 0)) / 86400
    return age_days > 30


def get_cached_weights() -> Optional[dict[str, dict[str, float]]]:
    data = load_cache()
    return data["weights"] if data else None


# ── fetch from server ──────────────────────────────────────────────────────

def _fetch_from_server() -> Optional[dict]:
    """Call the server's /role-weights endpoint. This triggers scraping if not cached."""
    url = f"{SERVER_URL}/role-weights"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "RuneSync/1.0"})
        # Role weights scraping is slow (5 LoLalytics pages), allow generous timeout
        resp = urllib.request.urlopen(req, timeout=300)
        data = json.loads(resp.read())
        if isinstance(data, dict) and len(data) > 10:
            return data
        return None
    except Exception as e:
        print(f"[role_updater] server request failed: {e}", flush=True)
        return None


# ── public functions (same signatures as before) ───────────────────────────

def refresh_roles_now() -> bool:
    """
    Fetch fresh role weights from the server and save to local cache.
    Returns True on success. Safe to call any time — no browser required.
    """
    print("[RuneSync] Fetching role weights from server...", flush=True)
    weights = _fetch_from_server()
    if weights:
        patch = get_latest_patch() or ""
        save_cache(weights, patch)
        print(f"[RuneSync] Role weights updated: {len(weights)} champions.", flush=True)
        return True
    print("[RuneSync] Role weight fetch returned no data.", flush=True)
    return False


def refresh_if_stale(background: bool = True) -> None:
    """
    Called at startup by champion_roles.py.
    If cache is stale, refresh — optionally in a background thread.
    """
    if not cache_is_stale():
        return

    def _do_refresh():
        print("[RuneSync] Role weight cache is stale — refreshing from server...", flush=True)
        try:
            ok = refresh_roles_now()
            if not ok:
                print("[RuneSync] Refresh returned no data — keeping existing cache.", flush=True)
        except Exception as e:
            print(f"[RuneSync] Role weight refresh failed: {e}", flush=True)

    if background:
        threading.Thread(target=_do_refresh, daemon=True).start()
    else:
        _do_refresh()


if __name__ == "__main__":
    print("RuneSync — refreshing champion role weights from server...")
    refresh_if_stale(background=False)
    data = load_cache()
    if data:
        print(f"\nDone. {len(data['weights'])} champions in cache.")
        for champ in ["Tryndamere", "Graves", "Jinx", "Thresh"]:
            print(f"  {champ}: {data['weights'].get(champ, 'NOT FOUND')}")
