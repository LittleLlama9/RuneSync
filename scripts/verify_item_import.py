"""
Manual smoke test: imports a hardcoded RuneSync item set into the League client.

Needs the League client open — no game required.
Run from the repo root:

    py scripts/verify_item_import.py

Not a pytest test (touches a live LCU); kept here as a maintainer tool.
"""
import os, sys

# Allow running from anywhere by putting the repo root on sys.path.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from lcu import LCUClient

CHAMP     = "Azir"
CHAMP_ID  = 268
ROLE      = "mid"
STARTER   = [1056, 2003]          # Doran's Ring + Health Potion
CORE      = [3165, 3157, 3089]    # Morellonomicon, Zhonya's, Rabadon's

lcu = LCUClient()
try:
    lcu.connect()
    print(f"Connected  (summoner id: {lcu._summoner_id})")
except Exception as e:
    print(f"Failed to connect: {e}")
    raise SystemExit(1)

ok = lcu.import_item_set(CHAMP, CHAMP_ID, ROLE, STARTER, CORE)
print("import_item_set:", "✓ success" if ok else "✗ failed")
