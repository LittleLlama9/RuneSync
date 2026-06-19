"""
role_updater.py — refresh champion role weight data.

Reads from the GitHub-hosted data bundle when available (post v1.0 model);
falls back to the legacy localhost server otherwise. Writes to
role_weights_cache.json exactly as before, so champion_roles.py needs
no changes.

SCALE CONTRACT (the subtle bit): the cache — and therefore everything in
champion_roles — is PERCENT scale (0-100), matching the hardcoded
ROLE_WEIGHTS fallback table and champion_roles._is_plausible_dist()'s
50-130 plausibility band. But the two upstream sources disagree:
  - the data bundle ships FRACTIONS 0-1 (op.gg role_rate / u.gg synthetic
    weights), because the bundle builder's role filters work on that scale;
  - the legacy localhost scraper already returns PERCENT.
So every fetched dict is run through _normalize_to_percent() at the single
boundary (_fetch_role_weights) before it touches the cache. Before this was
added the bundle's fraction weights were copied verbatim, _is_plausible_dist
rejected all of them (total ~1.0 < 50), and the role cache silently fell back
to the stale hardcoded table forever.

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
import ugg_api
from ugg_api import SERVER_URL  # legacy server fallback

# Cache path: %APPDATA%/RuneSync — writable on every install location,
# including Program Files where the exe directory is read-only for non-admin
# users. Falls back to the exe / repo dir if APPDATA isn't set (dev or odd
# environments).
def _resolve_cache_dir() -> str:
    appdata = os.environ.get("APPDATA")
    if appdata:
        d = os.path.join(appdata, "RuneSync")
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


CACHE_PATH = Path(_resolve_cache_dir()) / "role_weights_cache.json"

# Bump when the SHAPE or SCALE of cached weights changes. A cache written by a
# build that predates the fraction->percent normalization stores fraction-scale
# weights that champion_roles rejects; treating any non-current format as stale
# forces a one-time refresh so existing installs self-heal without waiting for a
# patch rollover (which is otherwise the only thing that invalidates the cache).
ROLE_CACHE_FORMAT = 2


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
        "format_version": ROLE_CACHE_FORMAT,
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
    # A pre-normalization cache holds fraction-scale weights that the runtime
    # rejects; force a refresh by treating any non-current format as stale.
    if data.get("format_version") != ROLE_CACHE_FORMAT:
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

def _fetch_from_bundle() -> Optional[dict]:
    """Pull role weights from the in-memory data bundle if loaded."""
    if not ugg_api.bundle_loaded():
        return None
    weights = ugg_api._bundle.get("role_weights") if ugg_api._bundle else None
    if isinstance(weights, dict) and len(weights) > 10:
        return weights
    return None


def _fetch_from_server() -> Optional[dict]:
    """Legacy fallback: call /role-weights on the localhost server."""
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


def _normalize_to_percent(weights: dict) -> dict:
    """Coerce a role-weight dict to PERCENT scale (0-100), per the module's
    scale contract.

    Detection is per-champion and scale-aware: a real percent distribution sums
    to roughly 40-150 (one dominant role plus secondaries), while a fraction one
    sums to ~1 (op.gg role_rate) or up to ~1.43 (u.gg synthetic). Those bands are
    far apart, so any champ whose weights sum at or below the cutoff is treated as
    fractions and scaled up. Idempotent: already-percent data (e.g. from the
    legacy scraper) passes through untouched, so this is safe to run on any
    source's output.
    """
    if not isinstance(weights, dict):
        return weights
    FRACTION_CUTOFF = 5.0  # well above the ~1.43 max fraction total, well below
                           # the ~40 min percent total — nothing lands between.
    out = {}
    for champ, roles in weights.items():
        if isinstance(roles, dict) and roles:
            nums = [v for v in roles.values() if isinstance(v, (int, float))]
            total = sum(nums)
            if 0 < total <= FRACTION_CUTOFF:
                roles = {r: round(v * 100, 2)
                         for r, v in roles.items()
                         if isinstance(v, (int, float))}
            else:
                roles = dict(roles)  # copy: never alias the live _bundle's dicts
        out[champ] = roles
    return out


def _fetch_role_weights() -> Optional[dict]:
    """Prefer the data bundle; fall back to the localhost server.

    Always returns PERCENT-scale weights regardless of source (the bundle ships
    fractions, the server ships percent) — see _normalize_to_percent.
    """
    raw = _fetch_from_bundle() or _fetch_from_server()
    return _normalize_to_percent(raw) if raw else None


# ── public functions (same signatures as before) ───────────────────────────

def refresh_roles_now() -> bool:
    """
    Fetch fresh role weights from the server and save to local cache.
    Returns True on success. Safe to call any time — no browser required.
    """
    print("[RuneSync] Fetching role weights (bundle preferred)...", flush=True)
    weights = _fetch_role_weights()
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
