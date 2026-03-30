"""
Simple JSON file cache for scraped data.

Cache keys include the current patch version so entries automatically
become misses when a new patch ships — no explicit invalidation needed.
"""

import json, threading
from pathlib import Path

CACHE_FILE = Path(__file__).parent / "cache.json"
_lock = threading.Lock()
_memory: dict = {}
_loaded: bool = False

# Sentinel stored as a JSON string for cache-miss entries.
# Allows cache.get() to distinguish "key not found" (returns None)
# from "key found but scrape returned nothing" (returns MISS).
MISS = "__miss__"


def _load() -> dict:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(data: dict) -> None:
    CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _ensure_loaded() -> None:
    global _loaded
    if not _loaded:
        _memory.update(_load())
        _loaded = True


def get(key: str):
    """Return the cached value, MISS sentinel, or None if key is absent."""
    with _lock:
        _ensure_loaded()
        if key not in _memory:
            return None
        return _memory[key]  # may be MISS or a real result


def set(key: str, value) -> None:
    with _lock:
        _ensure_loaded()
        _memory[key] = value
        _save(_memory)


def has(key: str) -> bool:
    with _lock:
        _ensure_loaded()
        return key in _memory


def purge_patch(patch: str) -> int:
    """Delete all cache entries whose key ends with _{patch}. Returns count removed."""
    with _lock:
        _ensure_loaded()
        stale = [k for k in _memory if k.endswith(f"_{patch}")]
        for k in stale:
            del _memory[k]
        if stale:
            _save(_memory)
        return len(stale)
