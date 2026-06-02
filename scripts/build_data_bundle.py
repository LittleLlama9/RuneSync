"""
build_data_bundle.py — Pre-compute the full u.gg data bundle for RuneSync.

Replaces the "run a server on every user's PC" model with "scrape once
centrally, ship a static JSON, every client reads from there."

What the bundle contains:
  - patch:        current patch string (from ddragon)
  - role_weights: scraped once globally (from lolalytics)
  - builds:       { champion_lower: { role: build_dict } }
  - counters:     { champion_lower: { role: [counter_dict, ...] } }

What it does NOT contain (intentional, too large to pre-bundle):
  - per-(my_champ, enemy_champ, role) matchup winrates. Those stay on the
    optional server, or are computed client-side from the counters list.

Run locally (slow — full scrape of ~150 champions × ~3 roles each):
    py scripts/build_data_bundle.py --output data_bundle.json

Run a smoke test (first N champions only):
    py scripts/build_data_bundle.py --output data_bundle.json --limit 3

Designed to be invoked by .github/workflows/build_bundle.yml on a cron.
"""

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
import urllib.request
from pathlib import Path

# Add repo root + server/ to sys.path so we can import the server's scraper.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "server"))

from playwright.async_api import async_playwright  # noqa: E402
import scraper                                      # noqa: E402

# Roles every champ will be attempted in. Champions with low role-weight in a
# role will be skipped to avoid scraping garbage builds.
ROLES = ["top", "jungle", "mid", "bot", "support"]

# Skip a (champ, role) if their lolalytics role-weight is below this fraction.
# Default 0.05 = 5% of games. Tunable via CLI.
ROLE_WEIGHT_THRESHOLD = 0.05


def fetch_ddragon_patch() -> str:
    """Latest LoL patch string, e.g. '15.6.1'."""
    url = "https://ddragon.leagueoflegends.com/api/versions.json"
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(url, context=ctx, timeout=10) as r:
        versions = json.loads(r.read())
    return versions[0]


def fetch_ddragon_champions(patch: str) -> list[str]:
    """Full list of champion display names for the given patch."""
    url = f"https://ddragon.leagueoflegends.com/cdn/{patch}/data/en_US/champion.json"
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(url, context=ctx, timeout=10) as r:
        data = json.loads(r.read())
    # data["data"] keys are slug-y ("Aatrox","MonkeyKing"); use the display name.
    return sorted(v["name"] for v in data["data"].values())


def relevant_roles(champ: str, role_weights: dict, threshold: float) -> list[str]:
    """Return the roles where this champion has >= threshold play rate."""
    w = role_weights.get(champ) or role_weights.get(champ.lower()) or {}
    out = []
    for role in ROLES:
        v = w.get(role, 0)
        if isinstance(v, (int, float)) and v >= threshold:
            out.append(role)
    # Always include at least one role so we don't skip a champion entirely.
    if not out:
        out = ["mid"]  # safe default; usually has a build
    return out


async def build_bundle(limit: int | None, threshold: float, output_path: Path) -> dict:
    """Drive the scraper across all (champ, role) combos and write JSON."""
    started_at = time.time()
    print(f"[bundle] starting at {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)

    patch = fetch_ddragon_patch()
    print(f"[bundle] patch: {patch}", flush=True)

    champions = fetch_ddragon_champions(patch)
    if limit:
        champions = champions[:limit]
        print(f"[bundle] LIMITED to first {limit} champions: {champions}", flush=True)
    print(f"[bundle] {len(champions)} champions to process", flush=True)

    pw = await async_playwright().start()
    await scraper.init(pw)

    try:
        print("[bundle] scraping role_weights (global)...", flush=True)
        role_weights = await scraper.scrape_role_weights()
        print(f"[bundle] role_weights: {len(role_weights)} champions", flush=True)

        builds: dict[str, dict] = {}
        counters: dict[str, dict] = {}
        failures: list[str] = []

        for i, champ in enumerate(champions, 1):
            roles_for_champ = relevant_roles(champ, role_weights, threshold)
            print(f"[bundle] [{i}/{len(champions)}] {champ} -> {roles_for_champ}", flush=True)

            ckey = champ.lower()
            builds[ckey] = {}
            counters[ckey] = {}

            for role in roles_for_champ:
                # Build
                try:
                    b = await scraper.scrape_build(champ, role)
                    if b:
                        builds[ckey][role] = b
                except Exception as e:
                    failures.append(f"build:{champ}:{role}:{e}")
                    print(f"[bundle] FAIL build {champ}/{role}: {e}", flush=True)
                # Counters
                try:
                    c = await scraper.scrape_counters(champ, role, top_n=5)
                    if c:
                        counters[ckey][role] = c
                except Exception as e:
                    failures.append(f"counters:{champ}:{role}:{e}")
                    print(f"[bundle] FAIL counters {champ}/{role}: {e}", flush=True)

        bundle = {
            "schema_version": 1,
            "generated_at": int(time.time()),
            "patch": patch,
            "champion_count": len(champions),
            "role_weights": role_weights,
            "builds": builds,
            "counters": counters,
            "failures": failures,
        }
    finally:
        await scraper.shutdown()
        await pw.stop()

    output_path.write_text(json.dumps(bundle, separators=(",", ":")), encoding="utf-8")
    elapsed = int(time.time() - started_at)
    print(
        f"[bundle] done in {elapsed}s — {len(builds)} champs, "
        f"{sum(len(r) for r in builds.values())} builds, "
        f"{sum(len(r) for r in counters.values())} counter sets, "
        f"{len(failures)} failures. Wrote {output_path} ({output_path.stat().st_size//1024} KB)",
        flush=True,
    )
    return bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data_bundle.json"),
                        help="Output JSON path (default: data_bundle.json)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Smoke-test: process only the first N champions")
    parser.add_argument("--threshold", type=float, default=ROLE_WEIGHT_THRESHOLD,
                        help=f"Min role-weight fraction (default: {ROLE_WEIGHT_THRESHOLD})")
    args = parser.parse_args()
    asyncio.run(build_bundle(args.limit, args.threshold, args.output))


if __name__ == "__main__":
    main()
