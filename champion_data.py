"""
champion_data.py — Riot Data Dragon champion catalog: class tags + attack range.

Mirrors item_data.py's async-load/cache pattern. Provides the *objective*
champion fields (official Data Dragon `tags`, `partype`, base `attackrange`) that
the draft/item recommenders combine with the curated judgment catalog in
score_v2/champion_attrs.json (damage_type / cc / engage).

Cached in %APPDATA%/RuneSync/champions/. Uses only the official Data Dragon CDN,
which RuneSync already relies on for items and rune icons.
"""
import json, os, threading, urllib.request

_FALLBACK_PATCH = "16.14.1"
_PATCH = _FALLBACK_PATCH
# display_name -> {"classes": [str], "partype": str, "range_type": "melee"|"ranged", "attackrange": int}
_CHAMPS: dict = {}
_loaded = threading.Event()
_init_started = False

# Base attack range at or below this is a melee champion; ranged champions start
# around 500. Nothing legitimate falls in the 200-450 gap.
_MELEE_MAX_RANGE = 350


def _cache_dir() -> str:
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    d = os.path.join(base, "RuneSync", "champions")
    os.makedirs(d, exist_ok=True)
    return d


def _fetch_latest_patch() -> str:
    try:
        req = urllib.request.Request(
            "https://ddragon.leagueoflegends.com/api/versions.json",
            headers={"User-Agent": "RuneSync/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=6).read())
        return data[0]
    except Exception:
        return _FALLBACK_PATCH


def _load_blocking():
    global _PATCH, _CHAMPS
    patch = _fetch_latest_patch()
    _PATCH = patch

    cache_path = os.path.join(_cache_dir(), f"_champ_{patch}.json")
    if os.path.exists(cache_path):
        try:
            _CHAMPS = json.loads(open(cache_path, encoding="utf-8").read())
            _loaded.set()
            return
        except Exception:
            pass

    try:
        url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json"
        req = urllib.request.Request(url, headers={"User-Agent": "RuneSync/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        champs = {}
        for cdata in data.get("data", {}).values():
            name = cdata.get("name")
            if not name:
                continue
            rng = int((cdata.get("stats") or {}).get("attackrange") or 0)
            champs[name] = {
                "classes": list(cdata.get("tags") or []),
                "partype": cdata.get("partype") or "",
                "attackrange": rng,
                "range_type": "melee" if 0 < rng <= _MELEE_MAX_RANGE else "ranged",
            }
        _CHAMPS = champs
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(champs, f)
    except Exception as e:
        print(f"[champions] catalog load failed: {e}")

    _loaded.set()


def init():
    """Start champion catalog loading in the background. Idempotent."""
    global _init_started
    if _init_started:
        return
    _init_started = True
    threading.Thread(target=_load_blocking, daemon=True).start()


def is_ready() -> bool:
    return _loaded.is_set()


def wait_ready(timeout: float = 4.0) -> bool:
    return _loaded.wait(timeout)


def data_for(name: str) -> dict:
    """Data Dragon-derived fields for a champion display name ({} if unknown)."""
    return _CHAMPS.get(name, {})


def patch() -> str:
    return _PATCH
