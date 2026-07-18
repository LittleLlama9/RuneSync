"""
item_data.py — Riot Data Dragon item catalog: names + icon URLs.
Catalog cached in %APPDATA%/RuneSync/items/. No Tkinter/PIL — icons are served
to the webview UI as ddragon CDN URLs (icon_url), loaded by the browser.
"""
import json, os, threading, urllib.request

_FALLBACK_PATCH = "15.6.1"
_PATCH = _FALLBACK_PATCH
_ITEM_CATALOG: list = []           # [{id, name, image}]
_catalog_loaded = threading.Event()
_init_started = False


def _cache_dir() -> str:
    base = os.environ.get("APPDATA", os.path.expanduser("~"))
    d = os.path.join(base, "RuneSync", "items")
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


def _load_catalog_blocking():
    global _PATCH, _ITEM_CATALOG
    patch = _fetch_latest_patch()
    _PATCH = patch

    cache_path = os.path.join(_cache_dir(), f"_catalog5_{patch}.json")
    if os.path.exists(cache_path):
        try:
            _ITEM_CATALOG = json.loads(open(cache_path, encoding="utf-8").read())
            _catalog_loaded.set()
            return
        except Exception:
            pass

    try:
        url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/item.json"
        req = urllib.request.Request(url, headers={"User-Agent": "RuneSync/1.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=15).read())
        items = []
        for iid, idata in data.get("data", {}).items():
            if not idata.get("gold", {}).get("purchasable", True):
                continue
            stats = idata.get("stats") or {}
            tags = list(idata.get("tags") or [])
            maps = idata.get("maps") or {}
            items.append({
                "id": int(iid),
                "name": idata["name"],
                "image": idata["image"]["full"],
                "gold": int((idata.get("gold") or {}).get("total") or 0),
                "tags": tags,
                # Compact defensive-stat subset the item recommender filters on.
                "armor": int(stats.get("FlatArmorMod") or 0),
                "mr": int(stats.get("FlatSpellBlockMod") or 0),
                "hp": int(stats.get("FlatHPPoolMod") or 0),
                "depth": len(idata.get("from") or []),
                # Summoner's Rift availability. Map 11 is SR, but some Arena /
                # special-mode items (6-digit IDs like 663058) are still flagged
                # on map 11 by Data Dragon, so also require a base-game item ID
                # (< 100000) to exclude them from resist suggestions.
                "sr": bool(maps.get("11")) and int(iid) < 100000,
            })
        items.sort(key=lambda x: x["name"])
        _ITEM_CATALOG = items
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(items, f)
    except Exception as e:
        print(f"[items] catalog load failed: {e}")

    _catalog_loaded.set()


def init():
    """Start catalog + patch loading in background. Safe to call multiple times."""
    global _init_started
    if _init_started:
        return
    _init_started = True
    threading.Thread(target=_load_catalog_blocking, daemon=True).start()


def is_ready() -> bool:
    """True once the item catalog has finished loading (or failed to)."""
    return _catalog_loaded.is_set()


def wait_ready(timeout: float = 4.0) -> bool:
    """Block up to `timeout`s for the catalog (so name_for resolves real names
    even if an import fires before the async load finishes). Safe off the UI thread."""
    return _catalog_loaded.wait(timeout)


def name_for(item_id) -> str:
    """Item display name from the catalog (or 'Item <id>' if unknown/not ready)."""
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return str(item_id)
    for i in _ITEM_CATALOG:
        if i["id"] == iid:
            return i["name"]
    return f"Item {iid}"


def icon_url(item_id) -> str:
    """ddragon CDN icon URL for an item id (empty string if unknown)."""
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return ""
    for i in _ITEM_CATALOG:
        if i["id"] == iid:
            return (f"https://ddragon.leagueoflegends.com/cdn/{_PATCH}"
                    f"/img/item/{i['image']}")
    return ""


def gold_value(item_id) -> int:
    """Total gold value of an item id (0 if unknown/consumable/not ready).

    Uses Data Dragon's `gold.total`, i.e. the full build cost of the item, so a
    sum over a player's held items approximates their invested gold.
    """
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return 0
    for i in _ITEM_CATALOG:
        if i["id"] == iid:
            return int(i.get("gold") or 0)
    return 0


def estimate_gold_from_items(item_ids) -> int:
    """Approximate a player's invested gold by summing held-item gold values.

    This is the same visible-inventory proxy other companion tools use to show a
    gold lead: the Live Client Data API exposes every player's item IDs but not
    enemy/teammate unspent gold, so summed item value is the closest local
    estimate. Trinkets/consumables contribute their (small/zero) catalog value.
    Returns 0 until the catalog has loaded.
    """
    total = 0
    for item_id in item_ids or []:
        total += gold_value(item_id)
    return total


def defensive_items(kind: str, min_gold: int = 800) -> list:
    """Purchasable items granting a defensive stat, richest first.

    `kind` is 'armor', 'mr', or 'hp'. Used by the item recommender to surface
    concrete resist buys that counter the enemy's damage profile. Filters to
    meaningfully-sized items (>= `min_gold`) so components aren't suggested.
    Returns [{id, name, image, gold, value}] where value is the stat amount.
    """
    key = {"armor": "armor", "mr": "mr", "hp": "hp"}.get(kind)
    if not key or not _catalog_loaded.is_set():
        return []
    out = []
    for i in _ITEM_CATALOG:
        v = int(i.get(key) or 0)
        if v <= 0 or int(i.get("gold") or 0) < min_gold:
            continue
        if not i.get("sr", True):        # skip Arena / special-mode-only items
            continue
        if "Consumable" in (i.get("tags") or []):
            continue
        out.append({"id": i["id"], "name": i["name"], "image": i["image"],
                    "gold": int(i.get("gold") or 0), "value": v})
    out.sort(key=lambda x: x["value"], reverse=True)
    return out


def item_tags(item_id) -> list:
    """Data Dragon tags for an item id ([] if unknown/not ready)."""
    try:
        iid = int(item_id)
    except (TypeError, ValueError):
        return []
    for i in _ITEM_CATALOG:
        if i["id"] == iid:
            return list(i.get("tags") or [])
    return []


def search(query: str, max_results: int = 12) -> list:
    """Return items whose name contains query (case-insensitive)."""
    if not query or not _catalog_loaded.is_set():
        return []
    q = query.lower()
    return [i for i in _ITEM_CATALOG if q in i["name"].lower()][:max_results]
