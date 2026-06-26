"""
Build data client — reads from a hosted data bundle by default, with a
local dev server fallback when the bundle is unavailable.

  - Clients fetch `data_bundle.json` on startup (init_bundle()) and answer
    all queries from the in-memory dict.
  - The localhost:8000 path is a developer-only fallback. End users only
    ever read the hosted bundle; they never install or run a server.
"""

import json, os, sys, threading, time, urllib.request, urllib.error, urllib.parse
from typing import Optional

# ── bundle config ──────────────────────────────────────────────────────────
BUNDLE_URL = (
    "https://github.com/LittleLlama9/RuneSync/releases/download/"
    "data-bundle/data_bundle.json"
)
# Local cache so the client doesn't re-download every launch.
_BUNDLE_CACHE_NAME = "data_bundle_cache.json"
_BUNDLE_CACHE_TTL  = 12 * 3600          # re-fetch every 12 hours

# ── dev server config (fallback path) ──────────────────────────────────────
# Used only when the hosted bundle is unavailable. Developer-only.
SERVER_URL = "http://localhost:8000"

ROLE_MAP = {
    "jungle": "jungle", "support": "support",
    "bot": "adc", "adc": "adc", "top": "top", "mid": "mid",
}

# ── module state ───────────────────────────────────────────────────────────
_bundle: Optional[dict] = None
_bundle_loaded_at: float = 0.0
# Set once init_bundle has returned (success or failure). Lookups block on
# this for a short window so first-launch champ-select doesn't race the
# download. Stays unset in tests / dev usage where init_bundle is never
# invoked — _bundle_init_started gates the wait so we don't deadlock.
_bundle_init_started = False
_bundle_ready_event = threading.Event()
_WINRATE_CACHE: dict = {}   # key -> {"patch": str, "result": dict}

# Read-time winrate sanity backstop. The builder already clamps, but a stale or
# pre-fix cached bundle can carry out-of-range values — the old op.gg path
# shipped counter winrates up to 82% off ~10-game samples. These bounds mirror
# the builder's clamps so such values can never reach the UI, even before a
# client re-downloads a corrected bundle. Counters are one-sided (>50), so a
# tight band; matchups get a wide garbage-only guard so legitimate lopsided
# lanes still show.
_COUNTER_WR_MIN, _COUNTER_WR_MAX = 40.0, 70.0
_MATCHUP_WR_MIN, _MATCHUP_WR_MAX = 25.0, 75.0

_patch_value: str = ""
_patch_fetched_at: float = 0.0
_PATCH_TTL: float = 6 * 3600


def _wait_for_bundle(timeout: float = 8.0) -> None:
    """Block briefly waiting for an in-flight init_bundle() to finish.

    No-op if init_bundle was never called (tests, dev REPL) or has already
    returned. Caps at `timeout` so a hung download can't lock the caller.
    """
    if _bundle_init_started and not _bundle_ready_event.is_set():
        _bundle_ready_event.wait(timeout)


def _cache_path() -> str:
    """Where the bundle is cached on disk.

    Uses %APPDATA%/RuneSync so the location is writable regardless of where
    the exe lives — sitting in Program Files is read-only for non-admin
    users and would silently break the cache. Falls back to the exe dir
    (dev convenience).
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        d = os.path.join(appdata, "RuneSync")
        try:
            os.makedirs(d, exist_ok=True)
            return os.path.join(d, _BUNDLE_CACHE_NAME)
        except Exception:
            pass
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, _BUNDLE_CACHE_NAME)


def _try_load_disk_cache() -> Optional[dict]:
    """Load the most recent bundle from disk if it's not too old.

    Note: staleness is TIME-based (12h TTL), deliberately NOT patch-based. The
    bundle's `patch` legitimately lags ddragon — the bundle is rebuilt and
    published after a patch ships — so comparing to ddragon's latest patch would
    reject the disk cache on every launch during that lag window and re-download
    a bundle that is STILL on the old patch (you can't get fresher data than has
    been published). The TTL already bounds staleness without that churn.
    """
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


def _meta_path() -> str:
    """Sidecar file storing the cached bundle's ETag / Last-Modified."""
    return _cache_path() + ".meta"


def _load_cache_meta() -> dict:
    """Return {'etag':..., 'last_modified':...} for the on-disk cache, or {}."""
    try:
        with open(_meta_path(), "r", encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _save_cache_meta(etag: Optional[str], last_modified: Optional[str]) -> None:
    try:
        with open(_meta_path(), "w", encoding="utf-8") as f:
            json.dump({"etag": etag or "", "last_modified": last_modified or ""}, f)
    except Exception as e:
        print(f"[ugg] could not write bundle meta: {e}", file=sys.stderr)


def _read_cached_bundle() -> Optional[dict]:
    """Parse whatever bundle is currently on disk, regardless of age."""
    try:
        with open(_cache_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _download_bundle() -> Optional[dict]:
    """Refresh the bundle from the GitHub release, using a conditional GET.

    If we already have a cached copy, we send its ETag / Last-Modified so the
    CDN can answer 304 Not Modified (zero-byte body) when the published bundle
    hasn't changed. On 304 we just keep the existing cache and reset its mtime
    so the TTL clock restarts -- no ~370KB re-download for unchanged data. Only
    a genuine change pulls the full file.
    """
    headers = {"User-Agent": "RuneSync/1.0"}
    have_cache = os.path.exists(_cache_path())
    if have_cache:
        meta = _load_cache_meta()
        if meta.get("etag"):
            headers["If-None-Match"] = meta["etag"]
        if meta.get("last_modified"):
            headers["If-Modified-Since"] = meta["last_modified"]
    try:
        req = urllib.request.Request(BUNDLE_URL, headers=headers)
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            etag = r.headers.get("ETag")
            last_modified = r.headers.get("Last-Modified")
        data = json.loads(raw)
        try:
            with open(_cache_path(), "wb") as f:
                f.write(raw)
            _save_cache_meta(etag, last_modified)
        except Exception as e:
            print(f"[ugg] could not write bundle cache: {e}", file=sys.stderr)
        return data
    except urllib.error.HTTPError as e:
        if e.code == 304:
            # Unchanged on the server -- reuse the disk cache and restart its TTL.
            cached = _read_cached_bundle()
            if cached is not None:
                try:
                    os.utime(_cache_path(), None)
                except Exception:
                    pass
                print("[ugg] bundle unchanged (304) -- reusing cache", file=sys.stderr)
                return cached
            # 304 but the cache vanished; fall through to a plain re-fetch.
            print("[ugg] 304 but cache missing; retrying full download",
                  file=sys.stderr)
            return _force_full_download()
        print(f"[ugg] bundle download failed: HTTP {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"[ugg] bundle download failed: {e}", file=sys.stderr)
        return None


def _force_full_download() -> Optional[dict]:
    """Unconditional GET (no validators). Used only to recover from a 304 whose
    cache disappeared underneath us."""
    try:
        req = urllib.request.Request(BUNDLE_URL, headers={"User-Agent": "RuneSync/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            etag = r.headers.get("ETag")
            last_modified = r.headers.get("Last-Modified")
        data = json.loads(raw)
        try:
            with open(_cache_path(), "wb") as f:
                f.write(raw)
            _save_cache_meta(etag, last_modified)
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

    Always sets _bundle_ready_event when done so synchronous callers don't
    block forever on a network failure.
    """
    global _bundle, _bundle_loaded_at, _bundle_init_started
    _bundle_init_started = True
    try:
        if not force_refresh:
            cached = _try_load_disk_cache()
            if cached:
                _bundle = cached
                _bundle_loaded_at = time.time()
                print(f"[ugg] bundle loaded from disk cache "
                      f"(patch {cached.get('patch','?')})", file=sys.stderr)
                return True
        fresh = _download_bundle()
        if fresh:
            _bundle = fresh
            _bundle_loaded_at = time.time()
            print(f"[ugg] bundle downloaded (patch {fresh.get('patch','?')}, "
                  f"{fresh.get('champion_count','?')} champs)", file=sys.stderr)
            return True
        return False
    finally:
        _bundle_ready_event.set()


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


def _derive_counters_from_matchups(enemy_champ: str, role: str,
                                   top_n: int) -> list[dict]:
    """Build a counter list for `enemy_champ` from its matchup table.

    Used when the bundle's curated counter list is empty. The matchup table is
    {opponent: enemy_champ's WR vs that opponent}, so opponents that beat
    `enemy_champ` are those where its WR < 50; the counter's WR is 100 - that.
    Returns the strongest such opponents (highest counter WR), clamped to the
    same sane band as the curated list. Empty if no losing matchups are known.
    """
    if not _bundle:
        return []
    table = (_bundle.get("matchups", {})
                    .get(enemy_champ.lower(), {})
                    .get(role) or {})
    if not isinstance(table, dict):
        return []
    derived = []
    for opp, champ_wr in table.items():
        if not isinstance(opp, str) or not isinstance(champ_wr, (int, float)):
            continue
        opp_wr = round(100 - champ_wr, 2)
        if opp_wr > 50.0 and _COUNTER_WR_MIN <= opp_wr <= _COUNTER_WR_MAX:
            derived.append({"champion": opp, "win_rate": opp_wr})
    derived.sort(key=lambda c: c["win_rate"], reverse=True)
    return derived[:top_n]


class UGGClient:
    def __init__(self):
        pass  # no local state needed

    def get_top_build(self, champion_name: str, role: str = "auto",
                      rank: str = "Platinum+", region: str = "World") -> Optional[dict]:
        # Bundle path. If the bundle download is still in flight (cold first
        # launch + fast champ select), block briefly so we don't fall through
        # to the localhost-server path while a perfectly good bundle is on
        # its way.
        _wait_for_bundle()
        if _bundle:
            champ_builds = (_bundle.get("builds", {})
                                   .get(champion_name.lower(), {}))
            # Try the requested role first.
            entry = champ_builds.get(role) if role and role != "auto" else None
            if entry:
                return entry
            # Fallback: champ-select didn't report a position (autofill, custom
            # game, undocumented queue) OR the requested role isn't bundled —
            # pick the highest-pickrate role for this champ from role_weights.
            # Bundle role_weights are FRACTIONS 0-1 (see role_updater's scale
            # contract); only the ordering matters here, but x100 keeps the log
            # readable as a real percentage.
            weights = _bundle.get("role_weights", {}).get(champion_name) or {}
            preferred = sorted(weights.items(), key=lambda kv: -kv[1])
            for rname, _frac in preferred:
                rkey = {"top": "top", "jungle": "jungle", "mid": "mid",
                        "bot": "bot", "adc": "bot", "support": "support"}.get(rname, rname)
                fallback = champ_builds.get(rkey)
                if fallback:
                    print(f"[ugg] no '{role}' build for {champion_name}; "
                          f"using {rkey} ({_frac * 100:.1f}% pickrate)", file=sys.stderr)
                    return fallback
            # Last resort: ANY role we have a build for.
            if champ_builds:
                rkey, fallback = next(iter(champ_builds.items()))
                print(f"[ugg] no role_weights for {champion_name}; "
                      f"using whatever bundle has ({rkey})", file=sys.stderr)
                return fallback
            raise RuntimeError(
                f"No bundled build for {champion_name} "
                f"(patch {_bundle.get('patch','?')}). "
                f"Data bundle may be incomplete — try restarting RuneSync."
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
        _wait_for_bundle()
        if _bundle:
            entry = (_bundle.get("counters", {})
                            .get(enemy_champ.lower(), {})
                            .get(role) or [])
            if isinstance(entry, list):
                entry = [c for c in entry
                         if isinstance(c, dict)
                         and isinstance(c.get("win_rate"), (int, float))
                         and _COUNTER_WR_MIN <= c["win_rate"] <= _COUNTER_WR_MAX]
                if entry:
                    return entry[:top_n]
            # Fallback: the curated top-5 counter list is empty for this champ
            # (common for lower-popularity picks the bundle builder couldn't fill
            # with 250+ game samples). Derive counters from the full matchup
            # table instead — it's keyed {opponent: ENEMY_champ's WR vs them}, so
            # an opponent that beats this champ is one where the champ's WR sits
            # below 50; the counter's WR is the complement. Real data, just a
            # lower sample floor, so it self-heals champs the curated list skips.
            return _derive_counters_from_matchups(enemy_champ, role, top_n)
        result = _get("/counters", {
            "champion": enemy_champ, "role": role, "top_n": top_n,
        }, timeout=45)
        if result is None:
            return []
        return result if isinstance(result, list) else []

    def get_matchup_winrate(self, my_champ: str, enemy_champ: str,
                            role: str = "auto") -> Optional[dict]:
        # Bundle path. Schema 2+ ships a full per-(champ, role) matchup
        # table, so most lookups hit on the first try.
        _wait_for_bundle()
        if _bundle is not None:
            enemy_lower = enemy_champ.lower()
            my_table = (_bundle.get("matchups", {})
                               .get(my_champ.lower(), {})
                               .get(role) or {})
            if isinstance(my_table, dict):
                # Try case-insensitive enemy lookup (the table is keyed by
                # display name as scraped from u.gg).
                for name, wr in my_table.items():
                    if isinstance(name, str) and name.lower() == enemy_lower \
                            and isinstance(wr, (int, float)) \
                            and _MATCHUP_WR_MIN <= wr <= _MATCHUP_WR_MAX:
                        return {"win_rate": float(wr), "enemy": enemy_champ}
            # Fallback for older bundles or rare combos: derive from the
            # counters list ("best picks vs enemy" — hits when my_champ is a
            # top counter to enemy_champ).
            counters_for_enemy = (_bundle.get("counters", {})
                                         .get(enemy_lower, {})
                                         .get(role) or [])
            if isinstance(counters_for_enemy, list):
                my_lower = my_champ.lower()
                for entry in counters_for_enemy:
                    if isinstance(entry, dict) and \
                            entry.get("champion", "").lower() == my_lower:
                        wr = entry.get("win_rate")
                        if isinstance(wr, (int, float)) \
                                and _MATCHUP_WR_MIN <= wr <= _MATCHUP_WR_MAX:
                            return {"win_rate": float(wr), "enemy": enemy_champ}
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
