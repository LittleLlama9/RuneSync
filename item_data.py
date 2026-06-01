"""
item_data.py — Riot Data Dragon item catalog + async icon loading.
Icons and catalog are cached in %APPDATA%/RuneSync/items/.
"""

import json, os, threading, urllib.request
from PIL import Image, ImageTk

_FALLBACK_PATCH = "15.6.1"
_PATCH = _FALLBACK_PATCH
_ITEM_CATALOG: list = []           # [{id, name, image}]
_ICON_TK_CACHE: dict = {}          # item_id -> PhotoImage | None
_ICON_LOAD_LOCK = threading.Lock()
_catalog_loaded = threading.Event()
_init_started = False
_ICON_SIZE = (32, 32)


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

    cache_path = os.path.join(_cache_dir(), f"_catalog_{patch}.json")
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
            items.append({
                "id": int(iid),
                "name": idata["name"],
                "image": idata["image"]["full"],
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


def search(query: str, max_results: int = 12) -> list:
    """Return items whose name contains query (case-insensitive)."""
    if not query or not _catalog_loaded.is_set():
        return []
    q = query.lower()
    return [i for i in _ITEM_CATALOG if q in i["name"].lower()][:max_results]


def get_icon_async(item_id: int, callback) -> None:
    """
    Fetch icon for item_id. Calls callback(ImageTk.PhotoImage | None).
    If already cached, callback fires immediately in the calling thread.
    Otherwise downloads in a background thread.
    """
    if item_id in _ICON_TK_CACHE:
        callback(_ICON_TK_CACHE[item_id])
        return

    def _work():
        photo = _load_icon_blocking(item_id)
        callback(photo)

    threading.Thread(target=_work, daemon=True).start()


def _load_icon_blocking(item_id: int):
    with _ICON_LOAD_LOCK:
        if item_id in _ICON_TK_CACHE:
            return _ICON_TK_CACHE[item_id]

        path = os.path.join(_cache_dir(), f"{item_id}.png")
        if not os.path.exists(path):
            try:
                item = next((i for i in _ITEM_CATALOG if i["id"] == item_id), None)
                if not item:
                    _ICON_TK_CACHE[item_id] = None
                    return None
                url = (f"https://ddragon.leagueoflegends.com/cdn/{_PATCH}"
                       f"/img/item/{item['image']}")
                req = urllib.request.Request(url, headers={"User-Agent": "RuneSync/1.0"})
                raw = urllib.request.urlopen(req, timeout=8).read()
                with open(path, "wb") as f:
                    f.write(raw)
            except Exception:
                _ICON_TK_CACHE[item_id] = None
                return None

        try:
            img = Image.open(path).resize(_ICON_SIZE, Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            _ICON_TK_CACHE[item_id] = photo
            return photo
        except Exception:
            _ICON_TK_CACHE[item_id] = None
            return None
