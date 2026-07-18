"""
champion_profile.py — unified champion profile for the draft & item recommenders.

Merges two *game-data* sources (never player data):
  * Data Dragon official `tags`/`attackrange` via champion_data.py (async CDN).
  * A curated judgment catalog score_v2/champion_attrs.json authored by a League
    design expert: {damage_type: AD|AP|MIXED, cc: none|soft|hard-single|hard-aoe,
    engage: bool}.

profile(name) returns a dict with both, using safe defaults for unknown/new
champions so the recommenders always degrade gracefully rather than crash.
"""
import json, os, threading
import champion_data

_ATTRS_PATH = os.path.join(os.path.dirname(__file__), "score_v2", "champion_attrs.json")
_ATTRS: dict = {}
_attrs_loaded = False
_lock = threading.Lock()

_DEFAULT = {"damage_type": "AD", "cc": "none", "engage": False}


def _load_attrs():
    global _ATTRS, _attrs_loaded
    with _lock:
        if _attrs_loaded:
            return
        try:
            _ATTRS = json.loads(open(_ATTRS_PATH, encoding="utf-8").read())
        except Exception as e:
            print(f"[champion_profile] attrs load failed: {e}")
            _ATTRS = {}
        _attrs_loaded = True


def is_known(name: str) -> bool:
    """True when `name` has a curated attribute entry (not a default guess)."""
    if not _attrs_loaded:
        _load_attrs()
    return isinstance(_ATTRS.get(name), dict)


def attrs_for(name: str) -> dict:
    """Curated {damage_type, cc, engage, known} for a champion.

    `known` is False for champions missing from the catalog (e.g. a brand-new
    champ); callers should skip them rather than trust the neutral defaults.
    """
    if not _attrs_loaded:
        _load_attrs()
    a = _ATTRS.get(name)
    if not isinstance(a, dict):
        return {**_DEFAULT, "known": False}
    return {
        "damage_type": a.get("damage_type") or _DEFAULT["damage_type"],
        "cc": a.get("cc") or _DEFAULT["cc"],
        "engage": bool(a.get("engage")),
        "known": True,
    }


def profile(name: str) -> dict:
    """Full profile: curated attrs + Data Dragon classes/range for `name`.

    Falls back to sensible neutral values when either source lacks the champion
    (e.g. a brand-new champ before the catalog is updated). Never raises. The
    `known` flag is False when the champion is absent from the curated catalog.
    """
    a = attrs_for(name)
    dd = champion_data.data_for(name) or {}
    return {
        "name": name,
        "damage_type": a["damage_type"],
        "cc": a["cc"],
        "engage": a["engage"],
        "known": a["known"],
        "classes": list(dd.get("classes") or []),
        "range_type": dd.get("range_type") or "",
    }


def is_ready() -> bool:
    """True once the curated catalog has been loaded at least once."""
    return _attrs_loaded


def known_champions() -> set:
    if not _attrs_loaded:
        _load_attrs()
    return set(_ATTRS.keys())
