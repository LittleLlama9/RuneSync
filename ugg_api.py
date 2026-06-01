"""
u.gg build data client — reads from a GitHub-hosted data bundle by default,
falls back to a local scraping server when the bundle is unavailable.

Architecture (post v1.0 public release):
  - `.github/workflows/build_bundle.yml` runs the server scraper on a cron
    and uploads `data_bundle.json` as a GitHub Release asset.
  - Clients fetch the bundle on startup (init_bundle()) and answer all
    queries from the in-memory dict.
  - The legacy localhost:8000 server path is kept for developers and as
    a fallback when the bundle is unreachable. End users do not need
    to install or run the server.
"""

import json, os, sys, time, urllib.request, urllib.error, urllib.parse
from typing import Optional

# ── bundle config ──────────────────────────────────────────────────────────
BUNDLE_URL = (
    "https://github.com/Ninjayeti/RuneSync/releases/download/"
    "data-bundle/data_bundle.json"
)
# Local cache so the client doesn't re-download every launch.
_BUNDLE_CACHE_NAME = "data_bundle_cache.json"
_BUNDLE_CACHE_TTL  = 12 * 3600          # re-fetch every 12 hours

# ── legacy server config (fallback path) ───────────────────────────────────
# Used only when the bundle is unavailable. Default to localhost so the
# old "run the FastAPI server" workflow still works for devs.
SERVER_URL = "http://localhost:8000"

ROLE_MAP = {
    "jungle": "jungle", "support": "support",
    "bot": "adc", "adc": "adc", "top": "top", "mid": "mid",
}

# ── module state ───────────────────────────────────────────────────────────
_bundle: Optional[dict] = None
_bundle_loaded_at: float = 0.0
_WINRATE_CACHE: dict = {}   # key -> {"patch": str, "result": dict}
_patch_value: str = ""
_patch_fetched_at: float = 0.0
_PATCH_TTL: float = 6 * 3600


def _cache_path() -> str:
    """Where the bundle is cached on disk (next to the exe / repo root)."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, _BUNDLE_CACHE_NAME)


def _try_load_disk_cache() -> Optional[dict]:
    """Load the most recent bundle from disk if it's not too old."""
    path = _cache_path()
    if not os.path.exists(path):
        return None
    try:
        age = time.time() - os.path.getmtime(path)
        if age > _BUNDLE_CACHE_TTL:
            return None
        return json.loads(open(path, "r", encoding="utf-8").read())
    except Exception:
        return None


def _download_bundle() -> Optional[dict]:
    """Pull a fresh bundle from the GitHub release and cache to disk."""
    try:
        req = urllib.request.Request(BUNDLE_URL, headers={"User-Agent": "RuneSync/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        data = json.loads(raw)
        # Persist to disk so subsequent launches are instant.
        try:
            with open(_cache_path(), "wb") as f:
                f.write(raw)
        except Exception as e:
            print(f"[ugg] could not write bundle cache: {e}", file=sys.stderr)
        return data
    except Exception as e:
        print(f"[ugg] bundle download failed: {e}", file=sys.stderr)
        return None


def init_bundle(force_refresh: bool = False) -> bool:
    """Load the data bundle into memory. Returns True if successful.

    Order of attempts:
      1. (unless force_refresh) recent on-disk cache
      2. fresh download from GitHub release
      3. give up — callers will fall back to the localhost server
    """
    global _bundle, _bundle_loaded_at
    if not force_refresh:
        cached = _try_load_disk_cache()
        if cached:
            _bundle = cached
            _bundle_loaded_at = time.time()
            print(f"[ugg] bundle loaded from disk cache (patch {cached.get('patch','?')})",
                  file=sys.stderr)
            return True
    fresh = _download_bundle()
    if fresh:
        _bundle = fresh
        _bundle_loaded_at = time.time()
        print(f"[ugg] bundle downloaded (patch {fresh.get('patch','?')}, "
              f"{fresh.get('champion_count','?')} champs)", file=sys.stderr)
        return True
    return False


def bundle_loaded() -> bool:
    return _bundle is not None


# ── legacy HTTP helpers (used only when bundle is unavailable) ─────────────

def _current_patch() -> str:
    """Return the current patch. Bundle wins; else server; else best effort."""
    global _patch_value, _patch_fetched_at
    if _bundle and _bundle.get("patch"):
        return _bundle["patch"]
    now = time.time()
    if _patch_value and now - _patch_fetched_at < _PATCH_TTL:
        return _patch_value
    result = _get("/patch", {}, timeout=10)
    new_patch = result.get("patch", "") if result else ""
    if new_patch and new_patch != _patch_value:
        if _patch_value:
            print(f"[ugg] patch {_patch_value} → {new_patch}: clearing winrate cache",
                  file=sys.stderr)
            _WINRATE_CACHE.clear()
        _patch_value = new_patch
        _patch_fetched_at = now
    return _patch_value


def _get(path: str, params: dict, timeout: int = 35) -> Optional[dict | list]:
    """GET to the localhost server. Returns parsed JSON or None on error."""
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


# ── public client ──────────────────────────────────────────────────────────

class UGGClient:
    def __init__(self):
        pass  # no local state needed

    def get_top_build(self, champion_name: str, role: str = "auto",
                      rank: str = "Platinum+", region: str = "World") -> Optional[dict]:
        # Bundle path
        if _bundle:
            entry = (_bundle.get("builds", {})
                            .get(champion_name.lower(), {})
                            .get(role))
            if entry:
                return entry
            # Bundle loaded but no entry — caller can decide what to do.
            # We deliberately don't fall through to server here; the bundle
            # is authoritative for what builds exist.
            raise RuntimeError(
                f"No bundled build for {champion_name}/{role} "
                f"(patch {_bundle.get('patch','?')})."
            )
        # Legacy server path
        result = _get("/build", {
            "champion": champion_name, "role": role,
            "rank": rank, "region": region,
        }, timeout=45)
        if result is None:
            raise RuntimeError(f"Failed to fetch build for {champion_name} from server.")
        return result

    def get_counters(self, enemy_champ: str, role: str = "auto",
                     top_n: int = 5) -> list[dict]:
        if _bundle:
            entry = (_bundle.get("counters", {})
                            .get(enemy_champ.lower(), {})
                            .get(role) or [])
            return entry[:top_n] if isinstance(entry, list) else []
        result = _get("/counters", {
            "champion": enemy_champ, "role": role, "top_n": top_n,
        }, timeout=45)
        if result is None:
            return []
        return result if isinstance(result, list) else []

    def get_matchup_winrate(self, my_champ: str, enemy_champ: str,
                            role: str = "auto") -> Optional[dict]:
        # NOT in the bundle (too many combos). Try server; on failure return
        # None, which monitor.py already handles gracefully.
        if _bundle is not None:
            # Bundle mode and no server — skip silently.
            return None
        patch = _current_patch()
        key = (my_champ.lower(), enemy_champ.lower(), role.lower())
        entry = _WINRATE_CACHE.get(key)
        if entry and entry.get("patch") == patch:
            return entry["result"]
        result = _get("/matchup", {
            "my_champ": my_champ, "enemy_champ": enemy_champ, "role": role,
        }, timeout=45)
        if result is not None:
            _WINRATE_CACHE[key] = {"patch": patch, "result": result}
        return result

    def get_current_patch(self) -> str:
        if _bundle and _bundle.get("patch"):
            return _bundle["patch"]
        result = _get("/patch", {}, timeout=10)
        if result and isinstance(result, dict):
            return result.get("patch", "latest")
        return "latest"

    def get_champion_id(self, champion_name: str) -> Optional[int]:
        return None  # not needed
