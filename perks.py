"""perks.py — perk-id → rune-name map from CommunityDragon.

The data bundle/LCU only name keystones and trees; the minor runes (Triumph,
Cut Down, Sudden Impact, ...) have no names anywhere in the app. CommunityDragon
publishes the full perk table, so we fetch + cache it once to fill the RUNE PAGE
panel's minor-rune lines. Mirrors ugg_api's time-based cache idiom.
"""
import json, os, time, threading, urllib.request

_URL = "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/global/default/v1/perks.json"
_TTL = 24 * 3600
_lock = threading.Lock()
_name_by_id: dict = {}
_loaded = False


def _cache_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "RuneSync")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return os.path.join(d, "perks_cache.json")


def _parse(data) -> dict:
    out = {}
    for p in data or []:
        try:
            out[int(p["id"])] = p["name"]
        except Exception:
            continue
    return out


def ensure():
    """Populate the id→name map: fresh cache → network → stale cache. Idempotent."""
    global _name_by_id, _loaded
    with _lock:
        if _loaded:
            return
        path = _cache_path()
        try:
            if os.path.exists(path) and time.time() - os.path.getmtime(path) < _TTL:
                _name_by_id = _parse(json.loads(open(path, encoding="utf-8").read()))
                _loaded = True
                return
        except Exception:
            pass
        try:
            req = urllib.request.Request(_URL, headers={"User-Agent": "RuneSync/1.0"})
            raw = urllib.request.urlopen(req, timeout=10).read()
            _name_by_id = _parse(json.loads(raw))
            try:
                with open(path, "wb") as f:
                    f.write(raw)
            except Exception:
                pass
            _loaded = True
            return
        except Exception:
            pass
        # network failed — fall back to stale cache if present
        try:
            if os.path.exists(path):
                _name_by_id = _parse(json.loads(open(path, encoding="utf-8").read()))
        except Exception:
            pass
        _loaded = True


def warm():
    """Kick off the fetch in the background so the first import is instant."""
    threading.Thread(target=ensure, daemon=True).start()


def expand_rune_page(perk_ids) -> dict:
    """perk_ids = [keystone, p1,p2,p3, s1,s2, shard1,shard2,shard3].
    Returns name strings for the keystone + the minor-rune lines."""
    ensure()
    ids = list(perk_ids or [])
    def nm(i):
        try:
            return _name_by_id.get(int(i), "") if i else ""
        except Exception:
            return ""
    return {
        "keystone":       nm(ids[0]) if len(ids) > 0 else "",
        "primaryMinor":   " · ".join(x for x in (nm(i) for i in ids[1:4]) if x),
        "secondaryMinor": " · ".join(x for x in (nm(i) for i in ids[4:6]) if x),
    }
