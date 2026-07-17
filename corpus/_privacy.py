"""Shared privacy scanning helpers used across the corpus package.

Centralized here so ``manifest.py``, ``adversarial_cases.py``, and
``review.py`` all reject the same things the same way instead of drifting.
This is defense-in-depth: callers should still build entries only from
already-sanitized fields (see ``build_from_history.py``), but every entry,
case, and label is re-scanned before it is persisted.
"""

from __future__ import annotations

import re
from typing import Any

# Substrings that must never appear as a key anywhere in a corpus/review
# record. Case-insensitive. This catches accidental credential leakage
# (a stray "api_key" or "riot_token" field) regardless of where it is
# nested.
FORBIDDEN_KEY_SUBSTRINGS = (
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "credential",
    "riot_key",
    "puuid",
    "summoner_name",
    "account_id",
)

# Riot personal/production developer keys look like RGAPI-<uuid>.
_RIOT_KEY_PATTERN = re.compile(r"RGAPI-[0-9a-fA-F-]{20,}")

# Riot PUUIDs (and most other Riot opaque identifiers) are long base64url-ish
# strings, exactly 78 characters with no separators. The threshold here is
# set well above readable case/entry ids (which top out in the 50s) and
# above a sha256 hex digest (64 chars, explicitly excluded below since those
# are this package's own legitimate content hashes) so it only ever fires on
# something that was never meant to leave ``history_store.py`` in the first
# place.
_RAW_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_-]{70,}$")
_PURE_HEX_PATTERN = re.compile(r"^[0-9a-f]+$")


def looks_like_riot_api_key(value: Any) -> bool:
    return isinstance(value, str) and bool(_RIOT_KEY_PATTERN.search(value))


def looks_like_raw_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not _RAW_IDENTIFIER_PATTERN.match(value):
        return False
    if _PURE_HEX_PATTERN.match(value):
        # A pure lowercase-hex string of this length is one of this
        # package's own sha256 content hashes, not a Riot identifier.
        return False
    return True


def scan_for_forbidden(obj: Any, *, _path: str = "$") -> list[str]:
    """Recursively scan ``obj`` for credential- or raw-identifier-shaped data.

    Returns a list of human-readable problem descriptions (empty if clean).
    Never raises -- callers decide whether to treat findings as fatal.
    """
    problems: list[str] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_str = str(key)
            lowered = key_str.lower()
            for bad in FORBIDDEN_KEY_SUBSTRINGS:
                if bad in lowered:
                    problems.append(f"{_path}.{key_str}: forbidden key pattern '{bad}'")
                    break
            problems.extend(scan_for_forbidden(value, _path=f"{_path}.{key_str}"))
    elif isinstance(obj, (list, tuple)):
        for index, value in enumerate(obj):
            problems.extend(scan_for_forbidden(value, _path=f"{_path}[{index}]"))
    elif isinstance(obj, str):
        if looks_like_riot_api_key(obj):
            problems.append(f"{_path}: value looks like a Riot developer API key")
        elif looks_like_raw_identifier(obj):
            problems.append(
                f"{_path}: value looks like a raw unhashed identifier "
                "(too long to be a local hashed id)"
            )
    return problems
